from datetime import timedelta

import pytest

from survival.clock import FakeClock, iso
from survival.config import Paths
from survival.waker import Waker
from survival.watchdog import state_store


@pytest.fixture
def env(tmp_path):
    clock = FakeClock()
    paths = Paths.for_mode("paper", tmp_path / "data")
    state = state_store(paths, "paper")
    state.set(phase="survival", rules_given=True)
    woke, running, prices = [], [False], {"BTC/USD": 100_000.0}
    w = Waker(state, paths, clock, woke.append, lambda: running[0],
              price=lambda s: prices[s], price_every=0, min_spawn_gap=0)
    return w, state, paths, clock, woke, running, prices


def test_nothing_wakes_it_without_conditions(env):
    w, *_ , woke, _, _ = env
    assert w.check() is None and woke == []


def test_time_condition(env):
    w, state, _, clock, woke, _, _ = env
    state.set(wake_conditions={"at": iso(clock.now() + timedelta(minutes=5)), "price": []})
    assert w.check() is None
    clock.advance(minutes=5)
    assert w.check().startswith("time")


def test_price_move_and_levels(env):
    w, state, _, clock, woke, _, prices = env
    state.set(wake_conditions={"set_at": "x", "price": [
        {"symbol": "BTC/USD", "move_pct": 3, "ref_price": None}]})
    assert w.check() is None                       # records the reference price
    assert state.read()["wake_conditions"]["price"][0]["ref_price"] == 100_000.0
    prices["BTC/USD"] = 102_000.0
    assert w.check() is None
    prices["BTC/USD"] = 96_900.0
    assert "moved -3.10%" in w.check()
    state.set(wake_conditions={"price": [{"symbol": "BTC/USD", "below": 97_000, "ref_price": 1}]})
    assert "at or below" in w.check()


def test_no_wake_while_paused_running_or_dead(env):
    w, state, paths, clock, woke, running, _ = env
    state.set(wake_conditions={"at": iso(clock.now()), "price": []})
    paths.paused_flag.write_text("x")
    assert w.check() is None
    paths.paused_flag.unlink()
    running[0] = True
    assert w.check() is None
    running[0] = False
    paths.dead_marker.write_text("x")
    assert w.check() is None


def test_genesis_and_interrupted_wake(env):
    w, state, *_ = env
    state.set(phase="genesis", rules_given=False)
    assert w.check() == "Genesis"
    state.set(phase="survival", awake=True)
    assert w.check().startswith("restart")
    assert state.read()["awake"] is False


def test_spawn_gap_prevents_loops(env):
    w, state, _, clock, woke, _, _ = env
    w.min_spawn_gap = 30
    state.set(wake_conditions={"at": iso(clock.now()), "price": []})
    assert w.check()
    assert w.check() is None          # brain refused and exited: wait before retrying
    clock.advance(seconds=31)
    assert w.check()
