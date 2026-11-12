from datetime import timedelta

from survival.broker import Holding
from survival.supervisor import is_finally_dead


def test_paper_funding_sets_treasury_to_chosen_amount(world):
    world.survive(trading=30.0)
    st = world.st
    assert st["paper_offset"] == 100_000.0 - 30.0
    world.wd.tick()
    b = world.st["balances"]
    assert b["trading"] == 30.0 and b["wallet"] == 20.0 and b["net_worth"] == 50.0
    assert world.st["death_floor"] == 12.50
    assert world.st["distance_usd"] == 37.5


def test_live_funding_waits_for_the_deposit(world):
    world.wd.mode = "live"
    world.broker.cash = 0.0
    world.genesis()
    world.state.set(phase="funding", split={"trading_usd": 30.0, "wallet_usd": 20.0})
    world.wd.tick()
    assert world.st["phase"] == "funding"
    world.broker.cash = 30.0  # the owner's transfer arrives
    world.wd.tick()
    assert world.st["phase"] == "survival" and world.st["paper_offset"] == 0.0


def test_forced_low_balance_kills_and_liquidates(world):
    world.survive(trading=45.0)  # wallet $5
    world.broker.cash -= 40.0
    world.broker.holdings["BTCUSD"] = Holding("BTCUSD", "crypto", 0.0002, 15.0, 1.0)
    world.broker.open_orders = 2
    world.wd.tick()
    assert world.st["phase"] == "survival"
    world.broker.holdings["BTCUSD"] = Holding("BTCUSD", "crypto", 0.0002, 1.0, -13.0)  # crash
    assert world.wd.tick() == "dead"
    st = world.st
    assert st["phase"] == "dead" and st["alive"] is False
    assert world.paths.dead_marker.exists()
    assert world.stopped == 1                       # brain + scripts stopped
    assert world.broker.open_orders == 0            # orders cancelled
    assert world.broker.closed == ["BTCUSD"]        # crypto sold at once
    assert st["death"]["liquidation"]["done"] is True
    assert st["death"]["liquidation"]["cancelled_count"] == 2
    assert is_finally_dead(world.paths)             # restart refused
    kinds = [r["kind"] for r in world.ledger.rows()]
    assert "death" in kinds


def test_stocks_wait_for_market_open(world):
    world.survive(trading=45.0)  # wallet $5
    world.broker.cash -= 45.0
    world.broker.holdings["AAPL"] = Holding("AAPL", "us_equity", 0.05, 5.0, -40.0)
    world.broker.market_open = False
    open_at = world.clock.now() + timedelta(hours=10)
    world.broker.next_open = open_at

    real_sleep = world.clock.sleep

    def sleep(s):  # the market opens while the watchdog waits
        real_sleep(s)
        if world.clock.now() >= open_at:
            world.broker.market_open = True
    world.clock.sleep = sleep

    assert world.wd.tick() == "dead"
    assert world.broker.closed == ["AAPL"]
    assert world.clock.now() >= open_at
    assert world.st["death"]["liquidation"]["done"]


def test_paper_force_networth_can_trigger_death(world):
    world.survive(trading=30.0)
    world.wd.tick()
    world.ledger.append("paper_adjust", amount_usd=10.0 - 50.0, actor="lea")
    assert world.wd.tick() == "dead"


def test_alpaca_outage_makes_no_death_decision(world):
    world.survive(trading=30.0)
    world.broker.cash = 0.0  # would be death...
    world.broker.fail = 3    # ...but Alpaca isn't answering
    for _ in range(3):
        assert world.wd.tick() == "error"
    assert world.st["phase"] == "survival"
    assert world.st["watchdog"]["consecutive_errors"] == 3
    assert world.wd.tick() == "dead"  # real data arrives


def test_stagnation_raises_floor_and_eventually_kills(world):
    world.survive(trading=30.0)
    world.broker.cash = 100_000.0 - 30.0 + 30.0 - 36.0  # NW = 14.00, flat
    for day in range(1, 30):
        world.clock.advance(days=1)
        r = world.wd.tick()
        if r == "dead":
            break
    # floor 12.50 + 0.25*(day-7) >= 14.00  ->  day 13
    assert day == 13
    assert world.st["death"]["death_floor"] == 14.00


def test_new_high_restarts_clock_and_paused_time_counts(world):
    world.survive(trading=30.0)
    world.paths.paused_flag.write_text("x")  # the owner pauses; the clock keeps running
    world.clock.advance(days=10)
    world.wd.tick()
    assert world.st["death_floor"] == 13.25
    world.broker.cash += 2.0  # NW 52 -> new high
    world.wd.tick()
    assert world.st["ath_usd"] == 52.0 and world.st["days_since_high"] == 0
    world.clock.advance(days=3)
    world.broker.cash -= 2.0
    world.wd.tick()
    assert world.st["death_floor"] == 13.25  # never goes back down, no rise yet


def test_rent_is_charged_in_euros_and_warned(world):
    world.survive(trading=45.0)  # wallet $5
    due = (world.clock.now() + timedelta(days=2)).date().isoformat()
    world.state.set(rent={"amount_eur": 4.51, "next_due": due, "period_months": 1})
    world.wd.tick()
    assert world.st["alerts"] == []  # $5 covers 4.51 EUR * 1.10 = 4.961
    world.ledger.append("card_payment", amount_usd=1.0)
    world.wd.tick()
    assert world.st["alerts"][0]["kind"] == "rent_uncovered"
    world.clock.advance(days=2)
    world.wd.tick()
    rents = [r for r in world.ledger.rows() if r["kind"] == "rent"]
    assert len(rents) == 1 and float(rents[0]["amount_usd"]) == round(4.51 * 1.10, 4)
    assert world.st["rent"]["next_due"] > due
