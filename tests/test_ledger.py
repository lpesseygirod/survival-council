from survival.ledger import Ledger


def test_pots_follow_money_events(world):
    world.genesis()
    L = world.ledger
    L.append("llm_call", service="xai", model="grok", amount_usd=0.62)  # Genesis credit
    p = L.pots()
    assert p.genesis_credit == 0.38 or abs(p.genesis_credit - 0.38) < 1e-9
    assert p.net_worth(0) == 50.0          # Genesis credit isn't its money
    L.append("llm_call", service="xai", amount_usd=0.50)  # 0.38 genesis + 0.12 own
    p = L.pots()
    assert p.genesis_credit == 0
    assert p.credit.get("xai", 0) == 0     # had none bought yet: clamps at 0
    L.append("credit_purchase", service="xai", amount_usd=5.0, amount_eur=4.55)
    p = L.pots()
    assert p.wallet == 45.0 and p.credit["xai"] == 5.0
    assert p.net_worth(0) == 50.0          # buying credit moves money, doesn't lose it
    L.append("llm_call", service="xai", amount_usd=0.25)
    assert L.pots().net_worth(0) == 49.75
    L.append("credit_zeroed", service="xai")  # out-of-credit error from the API
    assert L.pots().net_worth(0) == 45.0
    L.append("card_payment", amount_usd=2.0, what="data feed", where="example.com", reason="its reason")
    assert L.pots().wallet == 43.0


def test_ledger_is_shared_across_processes(world):
    world.genesis()
    other = Ledger(world.paths, world.clock)  # e.g. the brain process
    other.append("card_payment", amount_usd=3.0)
    assert world.ledger.pots().wallet == 47.0


def test_newlines_cannot_break_rows(world):
    world.genesis()
    world.ledger.append("card_payment", amount_usd=1.0, reason="line1\nline2,with,commas")
    rows = world.ledger.rows()
    assert rows[-1]["reason"] == "line1 line2,with,commas"
    assert world.ledger.pots().wallet == 49.0
