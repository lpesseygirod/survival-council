import json
import re

import httpx
import pytest

from survival.brain import Mind, start_genesis
from survival.clock import FakeClock, iso
from survival.vault import Vault
from survival.world import World

TICKS = 10_000_000_000


class FakeAPI:
    """Scripted OpenAI-compatible endpoint. Each entry answers one request."""

    def __init__(self):
        self.script = []
        self.requests = []

    def add(self, content="", tools=(), cost=0.01, status=200, body=None, usage=None):
        calls = [{"id": f"c{len(self.script)}_{i}", "type": "function",
                  "function": {"name": n, "arguments": json.dumps(a)}} for i, (n, a) in enumerate(tools)]
        msg = {"role": "assistant", "content": content}
        if calls:
            msg["tool_calls"] = calls
        self.script.append((status, body or {
            "choices": [{"message": msg}],
            "usage": usage or {"prompt_tokens": 1000, "completion_tokens": 100,
                               "completion_tokens_details": {"reasoning_tokens": 50},
                               "cost_in_usd_ticks": int(cost * TICKS)}}))

    def __call__(self, url, headers, body):
        self.requests.append({"url": url, "headers": headers, "body": json.loads(json.dumps(body))})
        status, data = self.script.pop(0)
        return httpx.Response(status, json=data, request=httpx.Request("POST", url))


def _no_network(url, headers):
    raise ConnectionError("tests never touch the network")


@pytest.fixture
def api():
    return FakeAPI()


@pytest.fixture
def world(tmp_path, api):
    v = Vault(tmp_path / "secrets")
    v.put("xai_api_key", "xai-TESTKEY-not-real-123")
    w = World.build("paper", data_dir=tmp_path / "data", clock=FakeClock(), vault=v,
                    post=api, sleep=lambda s: None, get=_no_network)
    w.state.set(phase="setup")
    start_genesis(w)
    w.state.set(watchdog={"last_ok": iso(w.clock.now())},
                balances={"trading": 0.0, "net_worth": 50.0})
    return w


def test_rules_are_facts_only(world):
    text = world.paths.rules.read_text()
    assert "$12.50" in text and "$0.25" in text and "grok-4.7" in text
    assert "$1.00" in text and "disconnected forever" in text
    for nudge in ("careful", "conservative", "cautious", "should", "recommend", "wise",
                  "risky", "step by step", "bold", "prudent", "avoid"):
        assert not re.search(rf"\b{re.escape(nudge)}", text, re.I), nudge


def test_free_server_means_no_rent_in_rules(tmp_path):
    from survival.config import load_config
    from survival.prices import seed as price_seed
    from survival.rules import render
    cfg = load_config()
    free = render(cfg, price_seed(cfg), {}, "paper")
    assert not re.search(r"(?i)\brent\b|\bserver\b", free)
    paid = render(cfg, price_seed(cfg), {"rent": {"amount_eur": 4.51}}, "paper")
    assert "server rent with your card" in paid and "Server rent: €4.51" in paid


def test_genesis_first_wake_gets_only_the_rules(world, api):
    api.add(tools=[("self_update", {"name": "Kestrel", "reason": "a name"}),
                   ("agent_create", {"name": "Skeptic", "job": "Argue against every idea.",
                                     "model": "grok-4.3", "reason": "second opinion"})], cost=0.40)
    api.add(tools=[("consult", {"agent": "Skeptic", "message": "Split 30/20?"})], cost=0.30)
    api.add(content="No volume behind it.", cost=0.02)  # Skeptic's answer
    api.add(tools=[("memory_write", {"path": "rules.md", "content": "floor 12.50"}),
                   ("set_wake", {"in_minutes": 60}),
                   ("genesis_ready", {"trading_usd": 30, "reason": "mine"})], cost=0.30)
    api.add(content="Done.", cost=0.10)

    rec = Mind(world).wake("Genesis")

    first = api.requests[0]["body"]["messages"]
    assert len(first) == 1 and first[0]["role"] == "user"
    assert first[0]["content"] == world.paths.rules.read_text()
    assert api.requests[0]["body"]["model"] == "grok-4.7"           # the owner's choice of first model
    assert "Authorization" in api.requests[0]["headers"]
    # Skeptic ran on its own model with its own job, and no tools it wasn't given
    sk = api.requests[2]["body"]
    assert sk["model"] == "grok-4.3" and sk["messages"][0] == {"role": "system", "content": "Argue against every idea."}
    assert "tools" not in sk

    st = world.state.read()
    assert st["rules_given"] and st["phase"] == "funding" and st["split"]["trading_usd"] == 30
    assert st["awake"] is False and st["wake_conditions"]["at"]
    arch = world.arch.read()
    assert arch["self"]["name"] == "Kestrel" and "Skeptic" in arch["agents"]
    assert [h["change"] for h in arch["history"]] == ["self_update", "agent_create"]

    pots = world.ledger.pots()
    assert pots.genesis_credit == 0                    # $1.12 spent: $1 from Genesis budget...
    assert round(pots.spent_total, 2) == 1.12          # ...the extra $0.12 from its own money
    llm_rows = [r for r in world.ledger.rows() if r["kind"] == "llm_call"]
    assert [r["actor"] for r in llm_rows] == ["self", "Kestrel", "Skeptic", "Kestrel", "Kestrel"]
    assert rec["cost_usd"] == pytest.approx(1.12)
    assert "consult" in rec["tools_used"]
    assert any(s["type"] == "reply" and s["actor"] == "Skeptic" for s in rec["steps"])
    assert world.memory.read("rules.md") == "floor 12.50"

    # nothing anywhere leaks the key
    for f in (world.paths.decisions, world.paths.events, world.paths.ledger):
        assert "TESTKEY" not in f.read_text()


def test_later_wake_gets_status_line_instructions_and_autoload(world, api):
    world.state.set(rules_given=True)
    world.memory.write("me.md", "I am Kestrel.")
    world.state.set(memory_autoload=["me.md"])
    world.arch.change("self_update", {}, "", lambda d: d["self"].update(instructions="My own words."))
    api.add(content="ok")
    Mind(world).wake("time 21:40")
    msgs = api.requests[0]["body"]["messages"]
    assert msgs[0] == {"role": "system", "content": "My own words."}
    assert msgs[1]["content"].startswith("Genesis | Wallet $50.00")
    assert "Woken by: time 21:40" in msgs[1]["content"]
    assert "I am Kestrel." in msgs[1]["content"]
    assert "Rules of the Game" not in msgs[1]["content"]   # given once only


def test_out_of_credit_zeroes_credit_and_uses_fallback(world, api):
    world.state.set(rules_given=True)
    world.ledger.append("credit_purchase", service="xai", amount_usd=5.0)
    world.arch.change("x", {}, "", lambda d: d["self"].update(
        fallbacks=[{"provider": "xai", "model": "grok-4.3"}]))
    api.add(status=403, body={"error": "Your team has used all available credits"})
    api.add(content="from fallback")
    rec = Mind(world).wake("t")
    assert world.ledger.pots().credit["xai"] == 0
    assert api.requests[1]["body"]["model"] == "grok-4.3"
    assert rec["final_text"] == "from fallback"


def test_no_credit_anywhere_ends_wake_without_crash(world, api):
    world.state.set(rules_given=True)
    api.add(status=402, body={"error": "payment required"})
    rec = Mind(world).wake("t")
    assert rec["final_text"] == "" and world.state.read()["awake"] is False


def test_cost_from_price_table_when_no_ticks(world, api):
    world.state.set(rules_given=True)
    api.add(content="x", usage={"prompt_tokens": 300_000, "completion_tokens": 1000,
                                "prompt_tokens_details": {"cached_tokens": 100_000}})
    Mind(world).wake("t")
    row = [r for r in world.ledger.rows() if r["kind"] == "llm_call"][-1]
    # grok-4.7 long context (>=200k): 200k*$4 + 100k*$1 + 1k*$12 per 1M
    assert float(row["amount_usd"]) == pytest.approx(0.8 + 0.1 + 0.012)


def test_memory_cannot_escape_its_folder(world):
    for bad in ("../state.json", "a/../../ledger.csv", "../../secrets/vault.key", ""):
        with pytest.raises(ValueError):
            world.memory.write(bad, "x")
    world.memory.write("/etc/passwd", "x")  # absolute paths are taken inside its memory
    assert (world.paths.memory / "etc" / "passwd").exists()


def test_watchdog_must_confirm_before_waking(world, api):
    world.state.set(watchdog={"last_ok": "2020-01-01T00:00:00+00:00"})
    assert Mind(world).wake("t") is None
    assert api.requests == []


def test_consult_depth_limit(world, api):
    world.state.set(rules_given=True)
    world.arch.change("x", {}, "", lambda d: d["agents"].__setitem__("Echo", {
        "job": "", "provider": "xai", "model": "grok-4.3", "tools": ["consult"], "options": {}}))
    depth = world.cfg.max_consult_depth
    api.add(tools=[("consult", {"agent": "Echo", "message": "go"})])
    for _ in range(depth):
        api.add(tools=[("consult", {"agent": "Echo", "message": "go"})])
    for _ in range(depth + 1):
        api.add(content="end")
    Mind(world).wake("t")
    assert any("depth limit" in r["body"]["messages"][-1].get("content", "")
               for r in api.requests if r["body"]["messages"][-1]["role"] == "tool")


def test_xai_price_sync_converts_units(tmp_path):
    from survival.config import Paths, load_config
    from survival.prices import Prices

    class R:
        def raise_for_status(self): pass
        def json(self):
            return {"models": [{"id": "grok-9", "prompt_text_token_price": 20000,
                                "cached_prompt_text_token_price": 5000,
                                "completion_text_token_price": 60000,
                                "long_context_threshold": 200000,
                                "prompt_text_token_price_long_context": 40000,
                                "completion_text_token_price_long_context": 120000}]}
    seen = {}
    def get(url, headers):
        seen.update(url=url, auth=headers["Authorization"])
        return R()
    p = Prices(load_config(), Paths.for_mode("paper", tmp_path), get=get)
    assert p.sync_xai("k") == ["grok-9"]
    m = p.model("xai", "grok-9")
    assert (m["input"], m["cached_input"], m["output"]) == (2.0, 0.5, 6.0)
    assert (m["input_long"], m["output_long"]) == (4.0, 12.0)
    assert seen["url"].endswith("/v1/language-models")
