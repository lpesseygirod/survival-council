from __future__ import annotations

import pytest

from survival.broker import FakeBroker
from survival.clock import FakeClock
from survival.config import Paths, load_config
from survival.fx import Fx
from survival.ledger import Ledger
from survival.watchdog import Watchdog, state_store


class World:
    """A paper run in a temp dir with a fake clock and a fake Alpaca."""

    def __init__(self, tmp_path):
        self.cfg = load_config()
        self.paths = Paths.for_mode("paper", tmp_path / "data")
        self.clock = FakeClock()
        self.ledger = Ledger(self.paths, self.clock)
        self.fx = Fx(self.paths, "unused", self.clock, fetch=lambda url: 1.10)
        self.broker = FakeBroker(cash=100_000.0)
        self.stopped = 0
        self.wd = Watchdog(self.cfg, self.paths, "paper", self.ledger, lambda: self.broker,
                           self.fx, self.clock, stop_ai=self._stop)
        self.state = state_store(self.paths, "paper")

    def _stop(self):
        self.stopped += 1

    def genesis(self):
        self.ledger.append("wallet_start", amount_usd=50.0, actor="watchdog")
        self.ledger.append("genesis_credit", amount_usd=1.0, service="xai", actor="watchdog")
        self.state.set(phase="genesis")

    def survive(self, trading=30.0):
        """Genesis -> split -> paper funding -> survival."""
        self.genesis()
        self.state.set(phase="funding", split={"trading_usd": trading, "wallet_usd": 50 - trading})
        assert self.wd.tick() == "ok"
        assert self.state.read()["phase"] == "survival"

    @property
    def st(self) -> dict:
        return self.state.read()


@pytest.fixture
def world(tmp_path):
    return World(tmp_path)
