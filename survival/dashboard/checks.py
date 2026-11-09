"""Setup wizard connection tests. Free endpoints only: no AI generation, no paid search."""

from __future__ import annotations

from typing import Any

import httpx

from ..desk import TRADING_BASE
from ..mail import Mail, MailError
from ..redact import Redactor
from ..vault import Vault


def _get(client: httpx.Client, url: str, headers: dict) -> httpx.Response | None:
    try:
        return client.get(url, headers=headers, timeout=20)
    except httpx.HTTPError:
        return None


def alpaca(vault: Vault, mode: str, client: httpx.Client | None = None) -> dict[str, Any]:
    c = client or httpx.Client()
    h = {"APCA-API-KEY-ID": vault.get(f"alpaca_{mode}_key_id"),
         "APCA-API-SECRET-KEY": vault.get(f"alpaca_{mode}_secret")}
    r = _get(c, f"{TRADING_BASE[mode]}/v2/account", h)
    if r is None:
        return {"ok": False, "error": "Alpaca didn't answer"}
    if r.status_code in (401, 403):
        return {"ok": False, "error": f"Alpaca rejected the key. Is it a {mode} key?"}
    if r.status_code >= 400:
        return {"ok": False, "error": f"Alpaca answered {r.status_code}"}
    a = r.json()
    trading = a.get("status") == "ACTIVE" and not a.get("trading_blocked") and not a.get("account_blocked")
    crypto = a.get("crypto_status") == "ACTIVE"
    return {
        "ok": bool(trading and crypto),
        "trading": trading, "crypto": crypto,
        # The trading API has no endpoint that moves money out of the account;
        # withdrawals exist only in Alpaca's own app.
        "withdrawals_off": True,
        "currency": a.get("currency"), "status": a.get("status"),
        "error": None if trading and crypto else (
            "trading is blocked on this account" if not trading else "crypto trading is not enabled yet"),
    }


def xai(vault: Vault, client: httpx.Client | None = None) -> dict[str, Any]:
    c = client or httpx.Client()
    h = {"Authorization": f"Bearer {vault.get('xai_api_key')}"}
    r = _get(c, "https://api.x.ai/v1/api-key", h)
    if r is not None and r.status_code == 200:
        k = r.json()
        blocked = k.get("api_key_blocked") or k.get("team_blocked") or k.get("api_key_disabled")
        return {"ok": not blocked, "error": "this key is blocked or disabled" if blocked else None,
                "note": "Connected. Its credit balance isn't readable with an API key: check you added $1 in xAI's console."}
    r = _get(c, "https://api.x.ai/v1/models", h)
    if r is None:
        return {"ok": False, "error": "xAI didn't answer"}
    if r.status_code >= 400:
        return {"ok": False, "error": f"xAI rejected the key ({r.status_code})"}
    return {"ok": True, "models": len(r.json().get("data", [])),
            "note": "Connected. Its credit balance isn't readable with an API key: check you added $1 in xAI's console."}


def tavily(vault: Vault, client: httpx.Client | None = None) -> dict[str, Any]:
    c = client or httpx.Client()
    r = _get(c, "https://api.tavily.com/usage", {"Authorization": f"Bearer {vault.get('tavily_api_key')}"})
    if r is None:
        return {"ok": False, "error": "Tavily didn't answer"}
    if r.status_code in (401, 403):
        return {"ok": False, "error": "Tavily rejected the key"}
    if r.status_code >= 400:
        return {"ok": False, "error": f"Tavily answered {r.status_code}"}
    data = r.json()
    key = data.get("key") or {}
    return {"ok": True, "usage": key.get("usage"), "limit": key.get("limit")}


def email(vault: Vault) -> dict[str, Any]:
    try:
        r = Mail(vault, Redactor(vault)).test()
        return {"ok": bool(r.get("ok")), "messages": r.get("messages")}
    except MailError as e:
        return {"ok": False, "error": str(e)}
