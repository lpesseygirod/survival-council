"""Fills a throwaway data dir with a believable run, to look at the dashboard.

    python -m tests.demo_seed <data_dir> <secrets_dir> [setup|genesis|funding|survival|dead]
"""

from __future__ import annotations

import json
import random
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from survival.clock import FakeClock, iso
from survival.config import Paths, load_config
from survival.ledger import Ledger
from survival.mind import Architecture, Memory
from survival.store import JsonStore, append_jsonl
from survival.vault import Vault
from survival.watchdog import state_store


def seed(data_dir: Path, secrets: Path, phase: str) -> None:
    cfg = load_config()
    random.seed(7)
    now = datetime.now(timezone.utc).replace(microsecond=0)
    start = now - timedelta(days=22, hours=5)
    clock = FakeClock(start)
    (data_dir).mkdir(parents=True, exist_ok=True)
    (data_dir / "current_mode").write_text("paper")
    paths = Paths.for_mode("paper", data_dir)
    v = Vault(secrets)
    st = state_store(paths, "paper")
    setup = JsonStore(paths.setup, paths.lock)
    if phase == "setup":
        setup.set(accounts={"alpaca": True, "xai": True})
        st.set(phase="setup")
        return
    v.put_card("Kestrel Council", "4242424242424417", 4, 2031, "321")
    for k in ("alpaca_paper_key_id", "alpaca_paper_secret", "xai_api_key", "tavily_api_key", "email_password"):
        v.put(k, "demo-" + k)
    v.put("email_address", "kestrel.council@fastmail.com")
    ok = {"ok": True, "at": iso(start)}
    setup.set(accounts={k: True for k in ("card", "alpaca", "xai", "tavily", "email", "server")},
              card_limit_eur=46.0, server_paid_by_card=True,
              tests={"alpaca": {**ok, "trading": True, "crypto": True, "withdrawals_off": True},
                     "xai": ok, "tavily": {**ok, "usage": 12, "limit": 1000}, "email": ok})
    led = Ledger(paths, clock)
    arch = Architecture(cfg, paths, clock)
    mem = Memory(paths, clock)
    ev = lambda t, **f: append_jsonl(paths.events, paths.lock, {"at": iso(clock.now()), "type": t, **f})  # noqa: E731
    paths.fx_cache.write_text(json.dumps({"date": now.date().isoformat(), "rate": 1.087}))
    st.set(phase="genesis", genesis_started_at=iso(start), rules_given=True,
           rent={"amount_eur": 4.51, "next_due": (now + timedelta(days=30)).date().isoformat(), "period_months": 1})
    led.append("wallet_start", actor="lea", amount_usd=50.0, what="its card (Wallet)")
    led.append("genesis_credit", actor="lea", service="xai", amount_usd=1.0, what="Genesis budget")
    ev("wake_start", actor="self", woken_by="Genesis")
    clock.advance(minutes=2)
    mem.write("rules.md", "Floor $12.50. +$0.25/day after 7 days without a new high. Never goes down.\nNet worth = trading + wallet + credit.")
    ev("memory", actor="self", action="added", path="rules.md", preview="Floor $12.50. +$0.25/day after 7 days")
    for i, (who, cost) in enumerate([("self", 0.08), ("self", 0.11), ("Kestrel", 0.06)]):
        clock.advance(minutes=2)
        led.append("llm_call", actor=who, service="xai", model="grok-4.7", tokens_in=9000, tokens_out=800,
                   amount_usd=cost, detail={"wake": "g1"})
    ev("tool", actor="self", tool="market_data", args=json.dumps({"path": "v1beta3/crypto/us/bars"}))
    arch.change("self_update", {"name": "Kestrel"}, "a name to sign with", lambda d: d["self"].update(name="Kestrel"))
    ev("architecture", actor="Kestrel", change="self_update", reason="a name to sign with")
    for name, job, model in [("Scout", "Reads news and market data, reports facts only.", "grok-4.3"),
                             ("Skeptic", "Argues against every trade idea.", "grok-4.7"),
                             ("Quant", "Writes and runs code, backtests.", "grok-build-0.1"),
                             ("Momentum", "Looks for trends.", "grok-4.3")]:
        clock.advance(minutes=1)
        arch.change("agent_create", {"name": name, "model": model, "job": job}, "council",
                    lambda d, n=name, j=job, m=model: d["agents"].__setitem__(n, {"job": j, "provider": "xai", "model": m, "tools": []}))
        ev("architecture", actor="Kestrel", change="agent_create", agent=name, model=model, reason="council")
    arch.change("structure", {"description": "council of 4 + itself"}, "different views",
                lambda d: d.__setitem__("structure", "council of 4 + itself"))
    if phase == "genesis":
        st.set(watchdog={"last_ok": iso(now)}, awake=True)
        ev("say", actor="Kestrel", text="Reading 90 days of BTC and ETH candles before choosing a split.")
        return
    clock.advance(minutes=4)
    split = {"trading_usd": 30.0, "wallet_usd": 19.75, "reason": "Most of it works on Alpaca; the card keeps enough for credit and rent.", "decided_at": iso(clock.now())}
    st.set(phase="funding", split=split)
    ev("genesis_ready", actor="Kestrel", trading_usd=30.0, wallet_usd=19.75, reason=split["reason"])
    if phase == "funding":
        return
    clock.advance(minutes=1)
    led.append("split", actor="watchdog", amount_usd=30.0, what="Trading Treasury funded on Alpaca", trading=30.0)
    surv = clock.now()
    st.set(phase="survival", survival_started_at=iso(surv), paper_offset=99970.0)
    # 22 days of history
    nw, ath, ath_at, floor = 50.0, 50.0, surv, 12.5
    t = surv
    while t < now:
        t += timedelta(hours=1)
        nw = max(14.5, nw + random.gauss(-0.01, 0.35) + (0.12 if t < surv + timedelta(days=11) else -0.02))
        if nw > ath:
            ath, ath_at = nw, t
        days = int((t - ath_at).total_seconds() // 86400)
        floor = max(floor, 12.5 + 0.25 * max(0, days - 7))
        append_jsonl(paths.history, paths.lock, {"t": iso(t), "nw": round(nw, 2), "floor": floor, "ath": round(ath, 2)})
    clock._now = now - timedelta(hours=4)
    mem.write("lessons.md", "Grid bot works in sideways ETH, fails in trends.")
    mem.write("costs.md", "Waking every hour cost $1.90 in 2 days for nothing.")
    mem.write("momentum.md", "Momentum on SOL is reliable.")
    mem.delete("momentum.md")
    arch.change("agent_delete", {"name": "Momentum"}, "three losing calls in a row", lambda d: d["agents"].pop("Momentum"))
    ev("architecture", actor="Kestrel", change="agent_delete", agent="Momentum", reason="three losing calls in a row")
    paths.code.mkdir(parents=True, exist_ok=True)
    (paths.code / "dip-watch.py").write_text("import council\n")
    (paths.code / "grid-bot-eth.py").write_text("import council\n")
    (paths.code / "backtest_rsi.py").write_text("print('rsi')\n")
    st.set(scripts={"dip-watch.py": {"path": "dip-watch.py", "status": "running", "desired": "running", "started_at": iso(now - timedelta(days=6)), "exits": 0},
                    "grid-bot-eth.py": {"path": "grid-bot-eth.py", "status": "running", "desired": "running", "started_at": iso(now - timedelta(days=9)), "exits": 1}})
    clock._now = now - timedelta(minutes=20)
    wake = "w-demo"
    ev("wake_start", actor="Kestrel", woken_by="BTC/USD moved +3.10% (from 96200 to 99182)")
    steps = []
    for who, model, cost, text in [("Scout", "grok-4.3", 0.01, "Pulled 4h candles and 3 news pages. Volume on ETH is below its 20-day average."),
                                   ("Skeptic", "grok-4.7", 0.04, "The ETH bounce has no volume behind it. If we buy, set a stop at 3% below."),
                                   ("Kestrel", "grok-4.7", 0.06, "Hold ETH. Add $3 to BTC with a limit at $97,850. Wake at 21:40 or on ±3%.")]:
        clock.advance(minutes=2)
        led.append("llm_call", actor=who, service="xai", model=model, tokens_in=6000, tokens_out=400, amount_usd=cost, detail={"wake": wake}, trading=28.4)
        if who != "Kestrel":
            ev("consult", actor="Kestrel", agent=who, message="Your view on ETH?")
            ev("reply", actor=who, to="Kestrel", text=text, cost=cost)
            steps += [{"actor": "Kestrel", "type": "consult", "agent": who, "message": "Your view on ETH?"},
                      {"actor": who, "type": "reply", "text": text, "cost": cost}]
        else:
            ev("say", actor=who, text=text, model=model, cost=cost)
            steps.append({"actor": who, "type": "say", "text": text, "model": model, "cost": cost})
    led.append("api_call", actor="Scout", service="tavily", amount_usd=0.0, what="tavily search", detail={"wake": wake})
    led.append("credit_purchase", actor="Kestrel", service="xai", amount_usd=5.0, amount_eur=4.6, what="xAI credit", where="console.x.ai", reason="more thinking")
    led.append("order", actor="Kestrel", symbol="BTC/USD", side="buy", qty="", price="97850", amount_usd=3.0, what="order placed", reason="dip", trading=28.4)
    led.append("fill", actor="Kestrel", symbol="BTC/USD", side="buy", qty="0.0000307", price="97850", amount_usd=3.0, what="fill", trading=28.4)
    led.append("fill", actor="grid-bot-eth.py", symbol="ETH/USD", side="sell", qty="0.0011", price="3412", amount_usd=3.75, what="fill", trading=28.4)
    steps.append({"actor": "Kestrel", "type": "tool", "tool": "place_order", "args": '{"order":{"symbol":"BTC/USD","side":"buy","notional":"3"}}'})
    append_jsonl(paths.decisions, paths.lock, {"wake_id": wake, "started": iso(now - timedelta(minutes=20)), "ended": iso(now - timedelta(minutes=8)),
                                               "woken_by": "BTC/USD moved +3.10%", "steps": steps, "cost_usd": 0.11,
                                               "tools_used": ["consult", "place_order", "memory_write"], "final_text": "",
                                               "next_wake": {"at": iso(now + timedelta(hours=3)), "price": [{"symbol": "BTC/USD", "move_pct": 3}]}})
    st.set(payments={"p1": {"id": "p1", "status": "waiting_for_bank", "amount": 5.0, "currency": "USD", "where": "xAI", "what": "xAI credit"}},
           alerts=[{"kind": "bank_confirmation", "payment_id": "p1", "amount": 5.0, "currency": "USD", "where": "xAI"}])
    nw = 47.80
    st.set(balances={"trading": 28.40, "cash": 6.10, "positions_value": 22.30, "wallet": 14.20,
                     "credit": {"xai": 4.10, "tavily": 1.10}, "credit_total": 5.20, "genesis_credit": 0.0,
                     "net_worth": nw, "spent_today": 0.31, "spent_total": 9.14, "as_of": iso(now)},
           death_floor=13.25, ath_usd=52.10, days_since_high=10, distance_usd=34.55, distance_pct=72.28,
           positions=[{"symbol": "BTCUSD", "asset_class": "crypto", "qty": 0.000149, "market_value": 14.60, "unrealized_pl": 0.82},
                      {"symbol": "ETHUSD", "asset_class": "crypto", "qty": 0.00226, "market_value": 7.70, "unrealized_pl": -0.31}],
           watchdog={"last_ok": iso(now)}, awake=True, speaking="Skeptic",
           wake={"id": wake, "woken_by": "BTC/USD moved +3.10%"},
           wake_conditions={"at": iso(now + timedelta(hours=3)), "price": [{"symbol": "BTC/USD", "move_pct": 3, "ref_price": 99182}]})
    if phase == "dead":
        mem.write("last.md", "If this is the last entry: the grid bot was right more often than I was.")
        paths.dead_marker.write_text("{}")
        st.set(phase="dead", alive=False, awake=False, speaking=None, death={
            "at": iso(now), "net_worth": 14.02, "death_floor": 14.25, "ath_usd": 52.10, "spent_total": 17.40,
            "liquidation": {"done": True, "cancelled_count": 2, "closed": [{"symbol": "BTCUSD", "value": 5.1}, {"symbol": "ETHUSD", "value": 4.21}], "cash_left": 9.31}})


if __name__ == "__main__":
    seed(Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3] if len(sys.argv) > 3 else "survival")
