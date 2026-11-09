"""Death floor and stagnation. Pure functions, no I/O.

- The floor starts at base_floor_usd.
- "Growing" = Net Worth reaches a new all-time high. The start ($50) is the first high.
- After grace_days without a new high, the floor rises daily_rise_usd for every
  further full day. Day 10 without a high -> 3 rises.
- A new high freezes the floor where it is and restarts the clock.
- The floor never goes back down. All time counts, paused or not.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime

from .clock import iso, parse
from .config import DeathRules

DAY = 86400.0


@dataclass(frozen=True)
class FloorState:
    floor_at_high: float  # floor value at the moment of the last all-time high
    ath_usd: float
    ath_at: datetime

    @classmethod
    def start(cls, rules: DeathRules, start_usd: float, at: datetime) -> "FloorState":
        return cls(rules.base_floor_usd, start_usd, at)

    def to_json(self) -> dict:
        d = asdict(self)
        d["ath_at"] = iso(self.ath_at)
        return d

    @classmethod
    def from_json(cls, d: dict) -> "FloorState":
        return cls(float(d["floor_at_high"]), float(d["ath_usd"]), parse(d["ath_at"]))


def days_since_high(st: FloorState, now: datetime) -> int:
    return max(0, int((now - st.ath_at).total_seconds() // DAY))


def floor_at(st: FloorState, now: datetime, rules: DeathRules) -> float:
    rises = max(0, days_since_high(st, now) - rules.grace_days)
    return round(st.floor_at_high + rises * rules.daily_rise_usd, 2)


def observe(st: FloorState, net_worth: float, now: datetime, rules: DeathRules) -> FloorState:
    """Record a Net Worth reading; a new all-time high freezes the floor and restarts the clock."""
    if round(net_worth, 2) > round(st.ath_usd, 2):
        return FloorState(floor_at(st, now, rules), net_worth, now)
    return st


def is_dead(net_worth: float, floor: float) -> bool:
    return round(net_worth, 2) <= round(floor, 2)
