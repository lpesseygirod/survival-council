import json
import re
import threading
from email.message import EmailMessage
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from survival import tools_browser  # noqa: F401
from survival.browser import Browser
from survival.clock import FakeClock
from survival.mail import Mail
from survival.tools import TOOLS, Ctx, run_tool
from survival.vault import Vault
from survival.world import World

CARD_NO = "4242424242424242"
KEY = "sk-live-abcDEF1234567890XYZabc"

STRIPE_FRAME = """<html><body>
<input autocomplete="cc-number" name="cardnumber" placeholder="Card number">
<input autocomplete="cc-exp" name="exp-date" placeholder="MM / YY">
<input autocomplete="cc-csc" name="cvc" placeholder="CVC">
</body></html>"""

PAGES = {
    "/checkout": """<html><head><title>Buy credit</title></head><body>
<h1>Buy $5 of credit</h1>
<label>Email <input name="email"></label>
<iframe src="https://js.stripe.com/v3/fake-card.html" width="400" height="200"></iframe>
<label>Name on card <input name="cardholder"></label>
<div id="echo"></div>
<button onclick="document.getElementById('echo').innerText =
  'Paying with 4242 4242 4242 4242'">Pay</button>
</body></html>""",
    "/key": f"""<html><head><title>API keys</title></head><body>
<p>Your new API key: <code id="k">{KEY}</code></p>
<input id="kin" readonly value="{KEY}">
<a href="/docs">Docs</a></body></html>""",
    "/3ds": """<html><body><h2>Confirm</h2>
<iframe src="https://hooks.stripe.com/3d_secure_2/challenge/abc"></iframe></body></html>""",
}


@pytest.fixture(scope="module")
def site():
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a): pass
        def do_GET(self):
            body = PAGES.get(self.path.split("?")[0], "<html>404</html>").encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(body)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()


class FakeIMAP:
    boxes = {}

    def __init__(self, host, port):
        self.host = host

    def login(self, user, pw):
        assert pw == "app-password-xyz"

    def select(self, box, readonly=False):
        return "OK", [str(len(self.boxes)).encode()]

    def list(self):
        return "OK", [b'(\\HasNoChildren) "/" "INBOX"']

    def uid(self, cmd, *args):
        if cmd == "SEARCH":
            return "OK", [" ".join(self.boxes).encode()]
        if cmd == "FETCH":
            raw = self.boxes[args[0].decode() if isinstance(args[0], bytes) else args[0]]
            return "OK", [(b"1 (FLAGS () BODY[] {1}", raw), b")"]
        return "OK", [b""]

    def logout(self):
        pass


class FakeSMTP:
    sent = []

    def __init__(self, host, port): pass
    def login(self, u, p): pass
    def send_message(self, m): FakeSMTP.sent.append(m)
    def quit(self): pass


@pytest.fixture
def world(tmp_path):
    v = Vault(tmp_path / "secrets")
    v.put_card("Kestrel Council", CARD_NO, 4, 2031, "321")
    v.put("email_address", "kestrel@fastmail.com")
    v.put("email_password", "app-password-xyz")
    holder = {}

    def bf():
        b = Browser(tmp_path / "profile", holder["w"].redactor, allow_private=True)
        b.start()
        b._ctx.route("https://js.stripe.com/**", lambda r: r.fulfill(body=STRIPE_FRAME, content_type="text/html"))
        b._ctx.route("https://hooks.stripe.com/**", lambda r: r.fulfill(body="<html>bank</html>", content_type="text/html"))
        return b

    w = World.build("paper", data_dir=tmp_path / "data", clock=FakeClock(), vault=v,
                    get=lambda u, h: 1 / 0, browser_factory=bf)
    w.mail = Mail(v, w.redactor, imap_factory=FakeIMAP, smtp_factory=FakeSMTP)
    w.fx._fetch = lambda url: 1.10
    holder["w"] = w
    w.state.set(phase="genesis")
    w.ledger.append("wallet_start", amount_usd=50.0)
    yield w
    w.close_browser()


def call(c, tool_name, **args):
    return json.loads(run_tool(c, tool_name, json.dumps(args), set(TOOLS)))


def dump(x) -> str:
    return json.dumps(x)


def test_card_never_visible_and_payment_logged(world, site):
    c = Ctx(world, "w1", "Kestrel")
    v = call(c, "browser_open", url=f"{site}/checkout")
    assert v["title"] == "Buy credit"
    card_refs = [e for e in v["elements"] if e.get("card_field")]
    assert {e["name"] for e in card_refs} >= {"cardnumber", "exp-date", "cvc", "cardholder"}
    # the AI can't type into card fields itself
    num_ref = next(e["ref"] for e in card_refs if e["name"] == "cardnumber")
    assert "pay_card" in call(c, "browser_type", ref=num_ref, text="4111")["error"]

    r = call(c, "pay_card", amount=5, currency="USD", what="xAI credit", where="console.x.ai",
             reason="more thinking", credit_service="xai")
    assert set(r["filled"]) >= {"number", "expiry", "cvc", "name"}
    pid = r["payment_id"]
    # the card really is in the page...
    frame = [f for f in world.get_browser().page.frames if "stripe" in f.url][0]
    assert frame.locator("input[name=cardnumber]").input_value().replace(" ", "") == CARD_NO
    # ...but nothing the AI can get shows it
    pay_ref = next(e["ref"] for e in call(c, "browser_view")["elements"] if e.get("label") == "Pay")
    after = call(c, "browser_click", ref=pay_ref)
    out = dump(after) + dump(call(c, "browser_html")) + dump(call(c, "browser_view"))
    for leak in (CARD_NO, "4242 4242 4242 4242", "321", "04/31", "04 / 31", "0431"):
        assert leak not in out, leak
    assert "[card]" in out
    # screenshot works, and the echoed card text is marked for blacking out
    shot = call(c, "browser_screenshot")
    assert shot["screenshot"] == "attached" and c.pending_images
    assert world.get_browser().page.locator("[data-cmask]").count() >= 1

    pots = world.ledger.pots()
    assert pots.wallet == 45.0 and pots.credit["xai"] == 5.0
    call(c, "payment_update", payment_id=pid, status="waiting_for_bank")
    assert world.state.read()["alerts"][0]["kind"] == "bank_confirmation"
    call(c, "payment_update", payment_id=pid, status="failed")
    pots = world.ledger.pots()
    assert pots.wallet == 50.0 and pots.credit["xai"] == 0
    assert world.state.read()["alerts"] == []
    for f in (world.paths.events, world.paths.ledger, world.paths.state):
        assert CARD_NO not in f.read_text()


def test_bank_confirmation_page_raises_alert(world, site):
    c = Ctx(world, "w1", "Kestrel")
    call(c, "browser_open", url=f"{site}/checkout")
    pid = call(c, "pay_card", amount=4.5, currency="EUR", what="server", where="hetzner",
               reason="rent")["payment_id"]
    assert world.ledger.pots().wallet == pytest.approx(50 - 4.95)
    v = call(c, "browser_open", url=f"{site}/3ds")
    assert v["bank_confirmation_frame"] is True
    assert world.state.read()["payments"][pid]["status"] == "waiting_for_bank"
    assert world.state.read()["alerts"][0]["where"] == "hetzner"


def test_new_api_key_goes_to_vault_unseen(world, site):
    c = Ctx(world, "w1", "Kestrel")
    v = call(c, "browser_open", url=f"{site}/key")
    assert KEY not in dump(v)
    handle = re.search(r"Your new API key: \[(hidden#\d+)\]", v["text"]).group(1)
    assert handle.startswith("hidden#")
    assert call(c, "vault_save", name="openai_key", hidden=handle) == {"ok": True, "saved": "openai_key"}
    assert world.vault.get("openai_key") == KEY
    ref = next(e["ref"] for e in v["elements"] if e.get("tag") == "input")
    assert call(c, "vault_save", name="openai_key2", ref=ref)["ok"]
    assert world.vault.get("openai_key2") == KEY
    # Lea's keys are off limits; its own secrets can be typed
    assert "reserved" in call(c, "vault_save", name="xai_api_key", hidden=handle)["error"]
    assert "not a secret you saved" in call(c, "browser_type_secret", ref=ref, name="email_password")["error"]
    assert call(c, "vault_names") == {"yours": ["openai_key", "openai_key2"]}
    assert KEY not in world.paths.events.read_text()


def test_browser_blocks_local_addresses(world, site, tmp_path):
    b = Browser(tmp_path / "p2", world.redactor)  # default: private addresses blocked
    try:
        with pytest.raises(Exception, match="could not open"):
            b.open(f"{site}/checkout")
    finally:
        b.close()


def test_email_read_hides_keys_and_send(world):
    m = EmailMessage()
    m["From"], m["To"], m["Subject"] = "noreply@x.ai", "kestrel@fastmail.com", "Your API key"
    m.set_content(f"Welcome. Your key is {KEY}\nVerification code: 482913")
    FakeIMAP.boxes = {"7": m.as_bytes()}
    c = Ctx(world, "w1", "Kestrel")
    lst = call(c, "email_list")
    assert lst[0]["uid"] == "7" and lst[0]["subject"] == "Your API key"
    msg = call(c, "email_read", uid="7")
    assert KEY not in dump(msg) and "482913" in msg["text"]
    handle = re.search(r"Your key is \[(hidden#\d+)\]", msg["text"]).group(1)
    assert call(c, "vault_save", name="xai_second_key", hidden=handle)["ok"]
    r = call(c, "email_send", to="support@example.com", subject="Hi", body="Question")
    assert r["ok"] and FakeSMTP.sent[-1]["From"] == "kestrel@fastmail.com"
