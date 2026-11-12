"""What the protected core needs from Alpaca: balances, cancel, close, market clock.

Trading for the AI itself (any order type, market data) goes through the
gateway; this module is the watchdog's own narrow view.

Paper mode: Alpaca's paper account usually holds far more than the amount the
AI chose. The Trading Treasury is therefore `paper equity - offset`, where the
offset is fixed at the moment Survival starts so that the treasury equals the
AI's chosen amount. (If the owner resets the paper account to exactly that amount,
the offset is simply 0.)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

from .vault import Vault


@dataclass(frozen=True)
class Holding:
    symbol: str
    asset_class: str  # "crypto" or "us_equity"
    qty: float
    market_value: float
    unrealized_pl: float


@dataclass(frozen=True)
class Snapshot:
    cash: float
    holdings: tuple[Holding, ...]
    pending_transfer_in: float = 0.0

    @property
    def equity(self) -> float:
        return round(self.cash + sum(h.market_value for h in self.holdings), 6)


class Broker(Protocol):
    def snapshot(self) -> Snapshot: ...
    def cancel_all_orders(self) -> int: ...
    def close_position(self, symbol: str) -> None: ...
    def market_clock(self) -> tuple[bool, datetime]: ...


def key_names(mode: str) -> tuple[str, str]:
    return f"alpaca_{mode}_key_id", f"alpaca_{mode}_secret"


class AlpacaBroker:
    def __init__(self, vault: Vault, mode: str):
        from alpaca.trading.client import TradingClient

        kid, sec = key_names(mode)
        self._c = TradingClient(vault.get(kid), vault.get(sec), paper=(mode == "paper"))

    def snapshot(self) -> Snapshot:
        acct = self._c.get_account()
        holdings = tuple(
            Holding(
                symbol=p.symbol,
                asset_class=str(getattr(p.asset_class, "value", p.asset_class)),
                qty=float(p.qty),
                market_value=float(p.market_value or 0),
                unrealized_pl=float(p.unrealized_pl or 0),
            )
            for p in self._c.get_all_positions()
        )
        return Snapshot(float(acct.cash), holdings, float(acct.pending_transfer_in or 0))

    def cancel_all_orders(self) -> int:
        return len(self._c.cancel_orders() or [])

    def close_position(self, symbol: str) -> None:
        self._c.close_position(symbol)

    def market_clock(self) -> tuple[bool, datetime]:
        c = self._c.get_clock()
        return bool(c.is_open), c.next_open


@dataclass
class FakeBroker:
    """In-memory broker for tests."""

    cash: float = 0.0
    holdings: dict[str, Holding] = field(default_factory=dict)
    open_orders: int = 0
    market_open: bool = True
    next_open: datetime | None = None
    fail: int = 0  # number of upcoming calls that raise, to simulate an outage
    closed: list[str] = field(default_factory=list)

    def _maybe_fail(self) -> None:
        if self.fail > 0:
            self.fail -= 1
            raise ConnectionError("simulated Alpaca outage")

    def snapshot(self) -> Snapshot:
        self._maybe_fail()
        return Snapshot(self.cash, tuple(self.holdings.values()))

    def cancel_all_orders(self) -> int:
        self._maybe_fail()
        n, self.open_orders = self.open_orders, 0
        return n

    def close_position(self, symbol: str) -> None:
        self._maybe_fail()
        h = self.holdings.pop(symbol)
        self.cash += h.market_value
        self.closed.append(symbol)

    def market_clock(self) -> tuple[bool, datetime]:
        return self.market_open, self.next_open or datetime.max
