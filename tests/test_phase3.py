import json
import socket
import subprocess
import sys
import time

import httpx
import pytest

from survival.clock import FakeClock, iso
from survival.desk import DeskError
from survival.gateway import Gateway
from survival.net import NetError
from survival.sandbox import Tokens
from survival.scripts import ScriptManager
from survival.supervisor import AIProcesses
from survival.tools import Ctx, run_tool, TOOLS
from survival import tools_world  # noqa: F401
from survival.vault import Vault
from survival.waker import Waker
from survival.world import World
from tests.fake_alpaca import FakeAlpaca


def public_resolve(host, port):
    ips = {"localhost": "127.0.0.1", "metadata": "169.254.169.254", "intranet": "10.0.0.5"}
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ips.get(host, "93.184.216.34"), 0))]


@pytest.fixture
def alpaca():
    return FakeAlpaca()


@pytest.fixture
def web():
    pages = {}

    def handle(req: httpx.Request):
        if req.url.host == "api.tavily.com":
            body = json.loads(req.content)
            if pages.get("tavily_broke"):
                return httpx.Response(432, json={"detail": "plan limit"})
            return httpx.Response(200, json={"results": [{"title": body["query"]}],
                                             "usage": {"credits": 2 if body.get("search_depth") == "advanced" else 1}})
        if req.url.host == "api.github.com":
            return httpx.Response(200, json={"full_name": req.url.path})
        page = pages.get(str(req.url))
        if page is None:
            return httpx.Response(404, text="no")
        if page.startswith("REDIRECT "):
            return httpx.Response(302, headers={"location": page[9:]})
        return httpx.Response(200, text=page, headers={"content-type": "text/html"})
    return pages, httpx.Client(transport=httpx.MockTransport(handle))


@pytest.fixture
def world(tmp_path, alpaca, web):
    v = Vault(tmp_path / "secrets")
    for k, val in {"alpaca_paper_key_id": "kid", "alpaca_paper_secret": "sec",
                   "tavily_api_key": "tvly-SECRETKEY123"}.items():
        v.put(k, val)
    w = World.build("paper", data_dir=tmp_path / "data", clock=FakeClock(), vault=v,
                    alpaca_client=httpx.Client(transport=alpaca.transport()),
                    net_client=web[1], resolve=public_resolve, get=lambda u, h: 1 / 0)
    w.state.set(phase="survival", paper_offset=100_000.0 - 30.0, rules_given=True,
                balances={"trading": 30.0})
    return w


def ctx(world, actor="Kestrel"):
    return Ctx(world, "wake-1", actor)


def call(c, tool_name, **args):
    return json.loads(run_tool(c, tool_name, json.dumps(args), set(TOOLS)))


# ================================================================== trading
def test_paper_account_is_its_own_treasury(world):
    a = call(ctx(world), "trading_account")
    assert a["cash"] == 30.0 and a["buying_power"] == 30.0


def test_buys_capped_at_its_cash_and_logged_under_its_name(world, alpaca):
    c = ctx(world)
    r = call(c, "place_order", order={"symbol": "BTC/USD", "side": "buy", "type": "market",
                                      "notional": "40", "time_in_force": "gtc"})
    assert "insufficient buying power" in r["error"]
    r = call(c, "place_order", order={"symbol": "BTC/USD", "side": "buy", "type": "limit",
                                      "qty": "0.0002", "limit_price": "97850", "time_in_force": "gtc"},
             reason="dip")
    assert r["status"] == "accepted"
    # the open order reserves its cash
    assert call(c, "trading_account")["buying_power"] == pytest.approx(30 - 19.57)
    row = [x for x in world.ledger.rows() if x["kind"] == "order"][-1]
    assert row["actor"] == "Kestrel" and row["symbol"] == "BTC/USD" and row["reason"] == "dip"
    assert world.desk.placed_by(r["id"]) == "Kestrel"
    assert world.paths.check_request.exists()  # watchdog asked to check now


def test_no_short_selling_in_paper(world, alpaca):
    r = call(ctx(world), "place_order", order={"symbol": "AAPL", "side": "sell", "type": "market",
                                               "qty": "1", "time_in_force": "day"})
    assert "no short selling" in r["error"]
    alpaca.positions["AAPL"] = {"symbol": "AAPL", "qty": "2", "qty_available": "2", "market_value": "400"}
    r = call(ctx(world), "place_order", order={"symbol": "AAPL", "side": "sell", "type": "market",
                                               "qty": "1", "time_in_force": "day"})
    assert r["status"] == "accepted"


def test_no_trading_before_survival_or_after_death(world):
    world.state.set(phase="genesis")
    r = call(ctx(world), "place_order", order={"symbol": "AAPL", "side": "buy", "notional": "1"})
    assert "not available" in r["error"]  # the tool isn't offered outside Survival
    with pytest.raises(DeskError, match="Survival"):
        world.desk.place_order("x", {"symbol": "AAPL", "side": "buy", "notional": "1"})
    world.state.set(phase="survival")
    world.paths.dead_marker.write_text("x")
    with pytest.raises(DeskError, match="dead"):
        world.desk.place_order("x", {"symbol": "AAPL", "side": "buy", "notional": "1"})


def test_market_data_passthrough_and_paths(world, alpaca):
    r = call(ctx(world), "market_data", path="v1beta3/crypto/us/bars",
             params={"symbols": ["BTC/USD", "ETH/USD"], "timeframe": "1Hour"})
    assert r["echo"] == "/v1beta3/crypto/us/bars" and r["params"]["symbols"] == "BTC/USD,ETH/USD"
    for bad in ("../v2/account", "https://evil.com/x", "v2/../../x"):
        assert "error" in call(ctx(world), "market_data", path=bad)


def test_portfolio_history_is_virtualized(world):
    h = call(ctx(world), "trading_history", kind="portfolio")
    assert h["equity"] == [30.0, 31.5]


def test_fills_logged_once_with_who_placed_them(world, alpaca):
    o = world.desk.place_order("grid-bot", {"symbol": "BTC/USD", "side": "buy", "type": "market",
                                            "notional": "5", "time_in_force": "gtc"})
    alpaca.fills = [{"id": "a1", "order_id": o["id"], "symbol": "BTC/USD", "side": "buy",
                     "qty": "0.00005", "price": "100000", "transaction_time": "2026-01-01T00:00:01Z"}]
    assert world.desk.sync_fills() == 1
    assert world.desk.sync_fills() == 0
    fill = [r for r in world.ledger.rows() if r["kind"] == "fill"]
    assert len(fill) == 1 and fill[0]["actor"] == "grid-bot" and float(fill[0]["amount_usd"]) == 5.0


# ================================================================= internet
def test_read_url_text_links_and_redirects(world, web):
    pages, _ = web
    pages["https://example.com/a"] = "REDIRECT https://example.com/b"
    pages["https://example.com/b"] = ("<html><title>B</title><script>evil()</script>"
                                      "<p>Hello <a href='/c'>there</a></p></html>")
    r = call(ctx(world), "read_url", url="https://example.com/a", links=True)
    assert r["title"] == "B" and "Hello" in r["text"] and "evil" not in r["text"]
    assert r["links"][0]["url"] == "https://example.com/c"


def test_internet_tools_cannot_reach_the_machine(world, web):
    pages, _ = web
    for url in ("http://localhost:8765/call", "http://metadata/latest", "http://intranet/",
                "file:///etc/passwd"):
        assert "error" in call(ctx(world), "read_url", url=url)
    pages["https://example.com/r"] = "REDIRECT http://metadata/latest"
    assert "non-public" in call(ctx(world), "read_url", url="https://example.com/r")["error"]


def test_tavily_free_tier_then_paid_then_out_of_credit(world, web):
    world.ledger.append("credit_purchase", service="tavily", amount_usd=1.0)
    world.state.set(tavily_usage={"month": world.clock.now().strftime("%Y-%m"), "credits": 999})
    call(ctx(world), "web_search", query="btc", options={"search_depth": "advanced"})
    row = [r for r in world.ledger.rows() if r["kind"] == "api_call"][-1]
    assert float(row["amount_usd"]) == pytest.approx(0.008)  # 1 free + 1 paid credit
    assert world.ledger.pots().credit["tavily"] == pytest.approx(0.992)
    web[0]["tavily_broke"] = True
    assert "out of credit" in call(ctx(world), "web_search", query="x")["error"]
    assert world.ledger.pots().credit["tavily"] == 0
    assert "SECRETKEY" not in world.paths.events.read_text()


# ===================================================================== code
def test_code_run_in_sandbox_and_paths(world):
    c = ctx(world)
    call(c, "code_write", path="hello.py", content="import os,sys\nprint('hi', sys.argv[1:])\n"
         "print('secrets dir visible:', 'COUNCIL_SECRETS_DIR' in os.environ)\n")
    r = call(c, "code_run", path="hello.py", args=["x"])
    assert r["exit_code"] == 0 and "hi ['x']" in r["stdout"]
    assert "secrets dir visible: False" in r["stdout"]
    for bad in ("../state.json", "../../secrets/vault.key", ".logs/x.log"):
        assert "error" in call(c, "code_write", path=bad, content="x")
    call(c, "code_write", path="slow.py", content="import time\ntime.sleep(30)\n")
    r = call(c, "code_run", path="slow.py", timeout_s=1)
    assert r["exit_code"] is None and "stopped after" in r["stderr"]


# ============================================== gateway + scripts end to end
def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def test_script_trades_and_wakes_through_gateway(world, alpaca):
    port = free_port()
    object.__setattr__(world.cfg, "gateway_port", port)
    gw = Gateway(lambda: world.desk, world.paths, world.state, world.tokens, port, world.clock)
    gw.start()
    try:
        assert gw.handle("bad-token", "account", {})[0] == 401
        call(ctx(world), "code_write", path="bot.py", content=(
            "import council\n"
            "print(council.account()['cash'])\n"
            "o = council.place_order(symbol='BTC/USD', side='buy', type='market', notional='2',"
            " time_in_force='gtc', reason='test')\n"
            "council.wake('bought ' + o['symbol'])\n"
            "try:\n    council.place_order(symbol='BTC/USD', side='buy', notional='500')\n"
            "except council.CouncilError as e:\n    print('refused:', e)\n"))
        call(ctx(world), "script_start", name="bot", path="bot.py", restart=False)
        ai = AIProcesses()
        sm = ScriptManager(world.paths, world.state, ai, world.sandbox, world.tokens, world.clock)
        sm.reconcile()
        assert ai.is_alive("script:bot")
        for _ in range(100):
            if not ai.is_alive("script:bot"):
                break
            time.sleep(0.1)
        sm.reconcile()
        log = (world.paths.script_logs / "bot.log").read_text()
        assert "30.0" in log and "refused: insufficient buying power" in log
        st = world.state.read()["scripts"]["bot"]
        assert st["status"] == "exited" and st["desired"] == "stopped" and st["last_exit_code"] == 0
        orders = [r for r in world.ledger.rows() if r["kind"] == "order"]
        assert orders[-1]["actor"] == "bot"
        woke = []
        Waker(world.state, world.paths, world.clock, woke.append, lambda: False).check()
        assert woke == ["script bot: bought BTC/USD"]
        assert world.tokens.actor("anything") is None
    finally:
        gw.stop()


def test_script_restart_backoff_and_stop(world):
    call(ctx(world), "code_write", path="crash.py", content="raise SystemExit(3)\n")
    call(ctx(world), "code_write", path="loop.py", content="import time\nwhile True: time.sleep(0.1)\n")
    call(ctx(world), "script_start", name="crash", path="crash.py", restart=True)
    call(ctx(world), "script_start", name="loop", path="loop.py")
    ai = AIProcesses()
    sm = ScriptManager(world.paths, world.state, ai, world.sandbox, world.tokens, world.clock)
    sm.reconcile()
    time.sleep(1.0)
    sm.reconcile()
    s = world.state.read()["scripts"]
    assert s["crash"]["status"] == "exited" and s["crash"]["last_exit_code"] == 3
    assert s["crash"]["next_start_at"] and s["loop"]["status"] == "running"
    sm.reconcile()  # backoff not elapsed: not restarted yet
    assert not ai.is_alive("script:crash")
    world.clock.advance(seconds=6)
    sm.reconcile()
    assert ai.is_alive("script:crash") or world.state.read()["scripts"]["crash"]["status"] == "running"
    call(ctx(world), "script_stop", name="loop")
    sm.reconcile()
    assert not ai.is_alive("script:loop")
    assert world.state.read()["scripts"]["loop"]["status"] == "stopped"
    # after a reboot, running scripts start again without counting an exit
    call(ctx(world), "script_start", name="loop", path="loop.py")
    sm.reconcile()
    ai.stop_all(grace=1)
    sm2 = ScriptManager(world.paths, world.state, AIProcesses(), world.sandbox, world.tokens, world.clock)
    sm2.on_boot()
    sm2.reconcile()
    assert sm2.ai.is_alive("script:loop")
    assert world.state.read()["scripts"]["loop"]["exits"] == 0
    sm2.ai.stop_all(grace=1)
