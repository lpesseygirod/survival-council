"""Wakes the brain when one of the conditions it set is met. Nothing else wakes it,
except: the very first Genesis wake-up, and resuming a wake-up a crash or
reboot interrupted. Scripts' wake calls are added in phase 3."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Callable

from .clock import Clock, iso, parse
from .config import Paths
from .store import JsonStore

log = logging.getLogger("council.waker")


class Waker:
    def __init__(self, state: JsonStore, paths: Paths, clock: Clock,
                 spawn_brain: Callable[[str], None], brain_running: Callable[[], bool],
                 price: Callable[[str], float] | None = None, price_every: float = 15.0,
                 min_spawn_gap: float = 30.0):
        self.state, self.paths, self.clock = state, paths, clock
        self.spawn_brain, self.brain_running = spawn_brain, brain_running
        self.price, self.price_every = price, price_every
        self._last_price_check: datetime | None = None
        self._last_spawn: datetime | None = None
        self.min_spawn_gap = min_spawn_gap

    def check(self) -> str | None:
        """Returns the wake reason if it woke the brain."""
        if self.paths.dead_marker.exists() or self.paths.paused_flag.exists():
            return None
        st = self.state.read()
        if st.get("phase") not in ("genesis", "funding", "survival"):
            return None
        if self.brain_running():
            return None
        now = self.clock.now()
        if self._last_spawn and (now - self._last_spawn).total_seconds() < self.min_spawn_gap:
            return None  # a wake-up that just ended or was refused: don't respawn in a loop
        if st.get("awake"):
            self.state.set(awake=False, wake=None, speaking=None)
            return self._wake("restart: the previous wake-up was interrupted")
        if st.get("phase") == "genesis" and not st.get("rules_given"):
            return self._wake("Genesis")
        for req in self._script_requests():
            return self._wake(req)
        cond = st.get("wake_conditions") or {}
        if cond.get("at") and now >= parse(cond["at"]):
            return self._wake(f"time {cond['at']}")
        if cond.get("price") and self.price and self._price_due(now):
            return self._check_prices(cond)
        return None

    def _price_due(self, now: datetime) -> bool:
        if self._last_price_check and (now - self._last_price_check).total_seconds() < self.price_every:
            return False
        self._last_price_check = now
        return True

    def _check_prices(self, cond: dict) -> str | None:
        changed = False
        for t in cond["price"]:
            sym = t["symbol"]
            try:
                p = self.price(sym)
            except Exception as e:
                log.warning("price for %s unavailable: %s", sym, e)
                continue
            if t.get("ref_price") is None:
                t["ref_price"], changed = p, True
                continue
            ref = float(t["ref_price"])
            if t.get("move_pct") is not None and ref > 0:
                move = (p / ref - 1) * 100
                if abs(move) >= float(t["move_pct"]):
                    return self._wake(f"{sym} moved {move:+.2f}% (from {ref:g} to {p:g})")
            if t.get("above") is not None and p >= float(t["above"]):
                return self._wake(f"{sym} at {p:g}, at or above {t['above']:g}")
            if t.get("below") is not None and p <= float(t["below"]):
                return self._wake(f"{sym} at {p:g}, at or below {t['below']:g}")
        if changed:
            set_at = cond.get("set_at")
            def f(d: dict) -> None:
                cur = d.get("wake_conditions") or {}
                if cur.get("set_at") == set_at:  # not replaced meanwhile
                    cur["price"] = cond["price"]
            self.state.update(f)
        return None

    def _script_requests(self) -> list[str]:
        return []  # phase 3: its scripts can ask to wake it

    def _wake(self, reason: str) -> str:
        log.info("waking: %s", reason)
        self._last_spawn = self.clock.now()
        self.state.set(last_wake_request={"at": iso(self._last_spawn), "reason": reason})
        self.spawn_brain(reason)
        return reason
