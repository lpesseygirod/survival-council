"""The protected watchdog: Net Worth, death floor, rent, funding, death.

Runs continuously in the protected supervisor, also while the brain sleeps or
the owner has paused it. The AI's code can neither reach nor change it.
"""

from __future__ import annotations

import calendar
import json
import logging
from datetime import date, datetime
from typing import Callable

from . import floor as fl
from .broker import Broker, Snapshot
from .clock import Clock, iso, parse
from .config import Config, Paths
from .fx import Fx
from .ledger import Ledger
from .store import JsonStore, append_jsonl

log = logging.getLogger("council.watchdog")

PHASES = ("setup", "genesis", "funding", "survival", "dead")


def new_state(mode: str) -> dict:
    return {"mode": mode, "phase": "setup", "alive": True, "awake": False,
            "paused": False, "alerts": []}


def state_store(paths: Paths, mode: str) -> JsonStore:
    return JsonStore(paths.state, paths.lock, default=lambda: new_state(mode))


def add_months(d: date, months: int) -> date:
    m = d.month - 1 + months
    y, m = d.year + m // 12, m % 12 + 1
    return date(y, m, min(d.day, calendar.monthrange(y, m)[1]))


def status_line(st: dict) -> str:
    """The one-line factual status shown to the brain when it wakes."""
    b = st["balances"]
    return (f"Net Worth ${b['net_worth']:.2f} | Trading ${b['trading']:.2f} | "
            f"Wallet ${b['wallet']:.2f} | Credit ${b['credit_total']:.2f} | "
            f"Death floor ${st['death_floor']:.2f} | "
            f"${st['distance_usd']:.2f} ({st['distance_pct']:.0f}%) from death | "
            f"Days since last high: {st['days_since_high']}")


class Watchdog:
    def __init__(self, cfg: Config, paths: Paths, mode: str, ledger: Ledger,
                 broker_factory: Callable[[], Broker], fx: Fx,
                 clock: Clock | None = None, stop_ai: Callable[[], None] = lambda: None,
                 fill_sync: Callable[[], int] | None = None):
        self.cfg, self.paths, self.mode = cfg, paths, mode
        self.ledger, self.fx = ledger, fx
        self.clock = clock or Clock()
        self.state = state_store(paths, mode)
        self.stop_ai = stop_ai
        self.fill_sync = fill_sync
        self._broker_factory = broker_factory
        self._broker: Broker | None = None

    @property
    def broker(self) -> Broker:
        if self._broker is None:
            self._broker = self._broker_factory()
        return self._broker

    # ------------------------------------------------------------------ loop
    def run_forever(self, should_stop: Callable[[], bool] = lambda: False) -> None:
        log.info("watchdog started (%s)", self.mode)
        while not should_stop():
            result = self.tick()
            if result == "dead" and self.state.read().get("death", {}).get("liquidation", {}).get("done"):
                log.info("death handled; watchdog stops")
                return
            wait = self.cfg.alpaca_retry_seconds if result == "error" else self.cfg.poll_seconds
            self._wait(wait, should_stop)

    def _wait(self, seconds: float, should_stop: Callable[[], bool]) -> None:
        """Sleep, but wake early when an order or charge asks for a check."""
        step = 0.5
        waited = 0.0
        while waited < seconds and not should_stop():
            if self.paths.check_request.exists():
                self.paths.check_request.unlink(missing_ok=True)
                return
            self.clock.sleep(step)
            waited += step

    # ------------------------------------------------------------------ tick
    def tick(self) -> str:
        st = self.state.read()
        phase = st.get("phase", "setup")
        if phase == "dead" or self.paths.dead_marker.exists():
            if not st.get("death", {}).get("liquidation", {}).get("done"):
                self.liquidate()
            return "dead"
        if phase == "setup":
            return "idle"

        self._charge_due_rent(st)

        snap: Snapshot | None = None
        if phase in ("funding", "survival"):
            try:
                snap = self.broker.snapshot()
            except Exception as e:
                self._record_error(e)
                return "error"

        if phase == "funding":
            self._check_funding(st, snap)
            st = self.state.read()
            phase = st["phase"]

        if phase == "survival" and self.fill_sync:
            try:
                self.fill_sync()
            except Exception as e:
                log.warning("fill sync failed: %s", e)

        now = self.clock.now()
        pots = self.ledger.pots()
        trading = self._trading_value(st, snap) if snap else 0.0
        nw = pots.net_worth(trading)

        update: dict = {
            "balances": {
                "trading": round(trading, 4),
                "cash": round(snap.cash - st.get("paper_offset", 0.0), 4) if snap else 0.0,
                "positions_value": round(sum(h.market_value for h in snap.holdings), 4) if snap else 0.0,
                "wallet": round(pots.wallet, 4),
                "credit": {k: round(v, 4) for k, v in pots.credit.items()},
                "credit_total": round(pots.credit_total, 4),
                "genesis_credit": round(pots.genesis_credit, 4),
                "net_worth": round(nw, 4),
                "spent_total": round(pots.spent_total, 4),
                "spent_today": round(pots.spent_by_day.get(now.date().isoformat(), 0.0), 4),
                "as_of": iso(now),
            },
            "positions": [h.__dict__ for h in snap.holdings] if snap else [],
            "watchdog": {"last_ok": iso(now), "last_error": None, "consecutive_errors": 0},
            "alerts": self._alerts(st, pots.wallet, now),
        }

        if phase != "survival":
            self.state.set(**update)
            self._history(now, nw, None, None)
            return "ok"

        fs = fl.observe(fl.FloorState.from_json(st["floor"]), nw, now, self.cfg.death)
        floor_now = fl.floor_at(fs, now, self.cfg.death)
        distance = nw - floor_now
        update.update({
            "floor": fs.to_json(),
            "death_floor": floor_now,
            "ath_usd": round(fs.ath_usd, 4),
            "days_since_high": fl.days_since_high(fs, now),
            "distance_usd": round(distance, 4),
            "distance_pct": round(100 * distance / nw, 2) if nw > 0 else 0.0,
        })
        self.state.set(**update)
        self._history(now, nw, floor_now, fs.ath_usd)

        if fl.is_dead(nw, floor_now):
            self.die(nw, floor_now, snap)
            return "dead"
        return "ok"

    # ------------------------------------------------------------ helpers
    _last_hist: tuple | None = None

    def _history(self, now: datetime, nw: float, floor_now: float | None, ath: float | None) -> None:
        """Points for the dashboard chart: on change, at most once a minute, else every 30 min."""
        key = (round(nw, 2), floor_now, None if ath is None else round(ath, 2))
        last = self._last_hist
        if last:
            age = (now - last[0]).total_seconds()
            if age < 60 or (key == last[1] and age < 1800):
                return
        self._last_hist = (now, key)
        append_jsonl(self.paths.history, self.paths.lock,
                     {"t": iso(now), "nw": round(nw, 4), "floor": floor_now, "ath": key[2]})
    def _trading_value(self, st: dict, snap: Snapshot) -> float:
        return snap.equity - float(st.get("paper_offset", 0.0))

    def _record_error(self, e: Exception) -> None:
        log.warning("Alpaca unreachable, no decision this round: %s", e)
        def f(d: dict) -> None:
            w = d.setdefault("watchdog", {})
            w["last_error"] = f"{iso(self.clock.now())} {type(e).__name__}: {e}"
            w["consecutive_errors"] = int(w.get("consecutive_errors", 0)) + 1
        self.state.update(f)

    def _check_funding(self, st: dict, snap: Snapshot) -> None:
        """Survival starts when the watchdog sees the AI's chosen amount on Alpaca."""
        x = float(st["split"]["trading_usd"])
        if self.mode == "paper":
            offset = round(snap.equity - x, 6)
        elif snap.equity + 0.005 >= x:
            offset = 0.0
        else:
            return
        self.start_survival(x, offset)

    def start_survival(self, trading_usd: float, paper_offset: float) -> None:
        now = self.clock.now()
        self.ledger.append("split", amount_usd=round(trading_usd, 4), actor="watchdog",
                           what="Trading Treasury funded on Alpaca", trading=trading_usd)
        fs = fl.FloorState.start(self.cfg.death, self.cfg.start_usd, now)
        self.ledger.append("phase", actor="watchdog", what="survival started", trading=trading_usd)
        self.state.set(phase="survival", paper_offset=paper_offset,
                       survival_started_at=iso(now), floor=fs.to_json(),
                       death_floor=self.cfg.death.base_floor_usd)
        log.info("survival started: trading $%.2f, paper offset %.2f", trading_usd, paper_offset)

    def _charge_due_rent(self, st: dict) -> None:
        rent = st.get("rent")
        if not rent or not rent.get("next_due"):
            return
        today = self.clock.now().date()
        due = date.fromisoformat(rent["next_due"])
        while due <= today:
            try:
                usd = self.fx.to_usd(float(rent["amount_eur"]))
            except Exception as e:
                log.warning("rent due but no FX rate yet: %s", e)
                return
            self.ledger.append("rent", actor="hosting", service="server",
                               amount_eur=rent["amount_eur"], amount_usd=usd,
                               what="server rent", where=rent.get("provider", "Hetzner"),
                               detail={"due": due.isoformat()})
            due = add_months(due, int(rent.get("period_months", 1)))
            nd = due.isoformat()
            self.state.update(lambda d: d["rent"].update(next_due=nd))

    def _alerts(self, st: dict, wallet_usd: float, now: datetime) -> list[dict]:
        # keep alerts other components raise (e.g. a payment waiting on the bank)
        alerts = [a for a in st.get("alerts", []) if a.get("kind") != "rent_uncovered"]
        rent = st.get("rent")
        if rent and rent.get("next_due"):
            due = date.fromisoformat(rent["next_due"])
            if (due - now.date()).days <= self.cfg.rent_warn_days:
                try:
                    rent_usd = self.fx.to_usd(float(rent["amount_eur"]))
                except Exception:
                    rent_usd = None
                if rent_usd is not None and wallet_usd < rent_usd:
                    alerts.append({"kind": "rent_uncovered", "due": due.isoformat(),
                                   "rent_eur": rent["amount_eur"], "rent_usd": rent_usd,
                                   "wallet_usd": round(wallet_usd, 2)})
        return alerts

    # ------------------------------------------------------------------ death
    def die(self, nw: float, floor_now: float, snap: Snapshot | None) -> None:
        now = self.clock.now()
        st = self.state.read()
        pots = self.ledger.pots()
        record = {
            "at": iso(now), "net_worth": round(nw, 4), "death_floor": floor_now,
            "ath_usd": st.get("ath_usd"), "spent_total": round(pots.spent_total, 4),
            "balances": st.get("balances"),
            "positions": [h.__dict__ for h in snap.holdings] if snap else [],
            "liquidation": {"done": False},
        }
        log.critical("DEATH: net worth $%.2f <= floor $%.2f", nw, floor_now)
        # 1. log the death with final balances
        self.ledger.append("death", actor="watchdog", amount_usd=round(nw, 4),
                           what="net worth reached the death floor",
                           detail={"floor": floor_now}, trading=st["balances"]["trading"])
        # DEAD marker first: no start, no gateway order, from this instant on
        self.paths.dead_marker.write_text(json.dumps({"died_at": iso(now)}))
        self.state.set(phase="dead", alive=False, awake=False, death=record)
        # stop the brain and every script so nothing trades during liquidation
        try:
            self.stop_ai()
        except Exception as e:
            log.error("stopping AI processes failed: %s", e)
        self._freeze_memory()
        # 2. cancel orders, sell everything
        self.liquidate()

    def _freeze_memory(self) -> None:
        """At death its memory and code stay on disk, read-only, for the owner."""
        for root in (self.paths.memory, self.paths.code):
            for p in [*sorted(root.rglob("*"), reverse=True), root]:
                try:
                    p.chmod(0o555 if p.is_dir() else 0o444)
                except OSError:
                    pass  # files the sandbox user owns: its processes are already stopped

    def liquidate(self) -> None:
        """Cancel all orders, sell all positions. Crypto now; stocks at the next open."""
        requested: dict[str, datetime] = {}  # symbol -> when we last asked to close it
        while True:
            st = self.state.read()
            liq = st.get("death", {}).get("liquidation", {})
            try:
                if not liq.get("orders_cancelled"):
                    n = self.broker.cancel_all_orders()
                    self._liq(orders_cancelled=True, cancelled_count=n)
                snap = self.broker.snapshot()
                if not snap.holdings:
                    self._liq(done=True, finished_at=iso(self.clock.now()),
                              cash_left=round(snap.cash - st.get("paper_offset", 0.0), 4))
                    self.ledger.append("phase", actor="watchdog", what="liquidation finished",
                                       trading=snap.cash - st.get("paper_offset", 0.0))
                    log.info("liquidation finished")
                    return
                market_open, next_open = self.broker.market_clock()
                waiting = []
                for h in snap.holdings:
                    if h.asset_class == "crypto" or market_open:
                        last = requested.get(h.symbol)
                        if last and (self.clock.now() - last).total_seconds() < 60:
                            continue  # close already sent; waiting for the fill
                        self.broker.close_position(h.symbol)
                        requested[h.symbol] = self.clock.now()
                        self._liq_closed(h)
                    else:
                        waiting.append(h.symbol)
                if waiting:
                    self._liq(waiting_for_open=waiting, next_open=iso(next_open))
                    secs = (next_open - self.clock.now()).total_seconds()
                    self.clock.sleep(max(5.0, min(secs, 300.0)))
                else:
                    self.clock.sleep(2.0)  # let fills settle, then re-check
            except Exception as e:
                self._record_error(e)
                self.clock.sleep(self.cfg.alpaca_retry_seconds)

    def _liq(self, **fields) -> None:
        self.state.update(lambda d: d["death"]["liquidation"].update(fields))

    def _liq_closed(self, h) -> None:
        def f(d: dict) -> None:
            lq = d["death"]["liquidation"]
            lq.setdefault("closed", []).append(
                {"symbol": h.symbol, "qty": h.qty, "value": h.market_value, "at": iso(self.clock.now())})
            if h.symbol in lq.get("waiting_for_open", []):
                lq["waiting_for_open"].remove(h.symbol)
        self.state.update(f)
        self.ledger.append("order", actor="watchdog", symbol=h.symbol, side="sell",
                           qty=h.qty, amount_usd=round(h.market_value, 4),
                           what="close position at death")
