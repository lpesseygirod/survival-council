import json
import threading

import httpx
import pytest

from survival.config import load_config
from survival.dashboard.server import Dashboard, set_password
from survival.vault import Vault
from tests.demo_seed import seed

PW = "correct horse battery"


@pytest.fixture
def dash(tmp_path):
    data, secrets = tmp_path / "data", tmp_path / "secrets"
    seed(data, secrets, "survival")
    vault = Vault(secrets)
    set_password(secrets / "dashboard.json", PW)
    calls = {"genesis": []}
    checkers = {k: (lambda mode, k=k: {"ok": True, "trading": True, "crypto": True, "withdrawals_off": True})
                for k in ("alpaca", "xai", "tavily", "email")}
    d = Dashboard(load_config(), data, vault, secrets / "dashboard.json", secure_cookie=False,
                  checkers=checkers, start_genesis=lambda mode: calls["genesis"].append(mode))
    srv = d.serve("127.0.0.1", 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_port}"
    yield d, base, vault, data, calls
    srv.shutdown()


def login(base) -> httpx.Client:
    c = httpx.Client(base_url=base)
    r = c.post("/api/login", json={"password": PW}, headers={"X-Council": "1"})
    assert r.status_code == 200
    assert "HttpOnly" in r.headers["set-cookie"] and "SameSite=Strict" in r.headers["set-cookie"]
    return c


def post(c, path, body=None):
    return c.post(path, json=body or {}, headers={"X-Council": "1"})


def test_login_required_and_wrong_password_throttled(dash):
    d, base, *_ = dash
    c = httpx.Client(base_url=base)
    assert c.get("/live").status_code == 200          # the page shell is public
    assert c.get("/api/snapshot").status_code == 401  # the data is not
    for _ in range(5):
        assert post(c, "/api/login", {"password": "nope"}).status_code == 401
    assert post(c, "/api/login", {"password": PW}).status_code == 401  # locked for a minute
    r = c.get("/live")
    assert "script-src 'self'" in r.headers["content-security-policy"]
    assert r.headers["x-frame-options"] == "DENY"


def test_post_needs_csrf_header(dash):
    d, base, *_ = dash
    c = login(base)
    assert c.post("/api/pause", json={}).status_code == 403


def test_snapshot_has_everything_and_no_secrets(dash):
    d, base, vault, data, _ = dash
    c = login(base)
    s = c.get("/api/snapshot").json()
    assert s["phase"] == "survival" and s["header"]["name"] == "Kestrel"
    assert s["money"]["net_worth"] == 47.80 and s["money"]["card_last4"] == "4417"
    assert {a["name"] for a in s["mind"]["agents"]} == {"Scout", "Skeptic", "Quant"}
    assert s["alerts"][0]["kind"] == "bank_confirmation"
    assert any(p["opened_by"] == "Kestrel" for p in s["positions"])
    assert s["activity"]["decisions"][0]["chips"][0].startswith("Order placed: BUY BTC/USD")
    assert any(m["action"] == "forgotten" for m in s["activity"]["memory_timeline"])
    assert len(s["chart"]) > 10
    text = json.dumps(s)
    for name in vault.names():
        value = vault.get(name)
        if name == "card":
            value = json.loads(value)["number"]
        if name != "email_address":
            assert value not in text, name


def test_pause_resume(dash):
    d, base, _, data, _ = dash
    c = login(base)
    post(c, "/api/pause")
    assert (data / "paper" / "PAUSED").exists()
    assert c.get("/api/snapshot").json()["header"]["paused"] is True
    post(c, "/api/resume")
    assert not (data / "paper" / "PAUSED").exists()


def test_setup_wizard_flow(tmp_path):
    data, secrets = tmp_path / "data", tmp_path / "secrets"
    seed(data, secrets, "setup")
    vault = Vault(secrets)
    set_password(secrets / "dashboard.json", PW)
    started = []
    seen = []
    checkers = {k: (lambda mode, k=k: seen.append(k) or {"ok": True}) for k in ("alpaca", "xai", "tavily", "email")}
    d = Dashboard(load_config(), data, vault, secrets / "dashboard.json", secure_cookie=False,
                  checkers=checkers, start_genesis=lambda m: started.append(m))
    srv = d.serve("127.0.0.1", 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    c = login(f"http://127.0.0.1:{srv.server_port}")
    try:
        assert post(c, "/api/setup/start-genesis").status_code == 409  # nothing ready
        assert post(c, "/api/setup/card", {"name": "K", "number": "4242 4242 4242 4241", "exp_month": 4,
                                           "exp_year": 31, "cvv": "321"}).status_code == 400  # bad Luhn
        r = post(c, "/api/setup/card", {"name": "K", "number": "4242 4242 4242 4242", "exp_month": 4,
                                        "exp_year": 31, "cvv": "321", "limit_eur": 46})
        assert r.json() == {"ok": True, "last4": "4242"}
        assert post(c, "/api/setup/alpaca", {"key_id": "PKTEST"}).status_code == 400  # secret missing
        assert post(c, "/api/setup/alpaca", {"key_id": "PKTEST", "secret": "s3cr3t"}).json()["ok"]
        assert vault.get("alpaca_paper_secret") == "s3cr3t"
        post(c, "/api/setup/xai", {"key": "xai-k"})
        post(c, "/api/setup/tavily", {"key": "tvly-k"})
        post(c, "/api/setup/email", {"address": "k@fastmail.com", "password": "app"})
        assert seen == ["alpaca", "xai", "tavily", "email"]
        assert post(c, "/api/setup/start-genesis").status_code == 409  # server still missing
        assert post(c, "/api/setup/server", {"rent_eur": "4.51", "next_due": "2026-12-09",
                                             "paid_by_card": True}).json()["ok"]
        s = c.get("/api/snapshot").json()
        assert s["setup"]["ready"] and s["setup"]["card_last4"] == "4242"
        assert "s3cr3t" not in json.dumps(s)
        assert post(c, "/api/setup/start-genesis").json()["ok"] and started == ["paper"]
        post(c, "/api/setup/mode", {"mode": "live"})
        assert (data / "current_mode").read_text() == "live"
    finally:
        srv.shutdown()


def test_funding_confirmation(tmp_path):
    data, secrets = tmp_path / "data", tmp_path / "secrets"
    seed(data, secrets, "funding")
    set_password(secrets / "dashboard.json", PW)
    d = Dashboard(load_config(), data, Vault(secrets), secrets / "dashboard.json", secure_cookie=False)
    srv = d.serve("127.0.0.1", 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    c = login(f"http://127.0.0.1:{srv.server_port}")
    try:
        s = c.get("/api/snapshot").json()
        g = s["genesis"]
        assert g["split"]["trading_usd"] == 30.0
        assert g["instructions"]["card_limit_eur"] == round(19.75 / 1.087, 2)
        assert post(c, "/api/funding/done").json()["ok"]
        assert c.get("/api/snapshot").json()["genesis"]["instructions"]["confirmed"]
    finally:
        srv.shutdown()
