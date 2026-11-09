from datetime import timedelta

from survival import floor as fl
from survival.clock import FakeClock
from survival.config import load_config

R = load_config().death


def test_floor_matches_design_examples():
    c = FakeClock()
    st = fl.FloorState.start(R, 50.0, c.now())
    assert fl.floor_at(st, c.now(), R) == 12.50
    assert fl.floor_at(st, c.now() + timedelta(days=7), R) == 12.50   # grace still running
    assert fl.floor_at(st, c.now() + timedelta(days=8), R) == 12.75   # first rise
    assert fl.floor_at(st, c.now() + timedelta(days=10), R) == 13.25  # dashboard design
    assert fl.floor_at(st, c.now() + timedelta(days=14), R) == 14.25  # death screen design


def test_new_high_stops_rise_restarts_clock_and_floor_never_goes_down():
    c = FakeClock()
    st = fl.FloorState.start(R, 50.0, c.now())
    c.advance(days=10)
    assert fl.floor_at(st, c.now(), R) == 13.25
    st = fl.observe(st, 50.01, c.now(), R)  # new all-time high
    assert st.ath_usd == 50.01
    assert fl.floor_at(st, c.now(), R) == 13.25        # frozen, not reset to 12.50
    c.advance(days=7)
    assert fl.floor_at(st, c.now(), R) == 13.25        # new grace period
    c.advance(days=1)
    assert fl.floor_at(st, c.now(), R) == 13.50        # rising again from where it was


def test_equal_to_high_is_not_a_new_high():
    c = FakeClock()
    st = fl.FloorState.start(R, 50.0, c.now())
    c.advance(days=9)
    assert fl.observe(st, 50.0, c.now(), R) is st


def test_dies_at_or_below_floor():
    assert fl.is_dead(12.50, 12.50)
    assert fl.is_dead(12.49, 12.50)
    assert not fl.is_dead(12.51, 12.50)
