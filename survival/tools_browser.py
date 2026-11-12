"""Tools: the browser, the vault (by name only), card payments, the email inbox."""

from __future__ import annotations

import base64
import json
import re
import secrets
import string
import uuid
from typing import Any

from .clock import iso
from .tools import ALL_PHASES, Ctx, tool
from .vault import CARD

# secrets the owner set up: the AI can use them through tools but never write or type them
RESERVED = re.compile(r"^(card|xai_api_key|tavily_api_key|alpaca_.*|email_.*)$")
SECRET_NAME = re.compile(r"^[a-z0-9_.\-]{1,64}$")


def _browser(ctx: Ctx):
    return ctx.world.get_browser()


def _after_browser(ctx: Ctx, view: dict) -> dict:
    """A bank's confirmation page during a payment: tell the owner's dashboard."""
    if isinstance(view, dict) and view.get("bank_confirmation_frame"):
        pays = ctx.world.state.read().get("payments") or {}
        pending = [p for p in pays.values() if p.get("status") == "pending"]
        if pending:
            _set_status(ctx, pending[-1]["id"], "waiting_for_bank")
    return view


# =================================================================== browser
@tool("browser_open", "Open a URL in your browser (new_tab=true for a new tab). Returns the page: "
      "url, title, text, and its interactive elements with refs (e12, f2e3 inside frames).",
      {"url": {"type": "string"}, "new_tab": {"type": "boolean"}}, ("url",))
def _b_open(ctx: Ctx, url: str, new_tab: bool = False) -> Any:
    return _after_browser(ctx, _browser(ctx).open(url, new_tab))


@tool("browser_view", "The current page again (refs refreshed). text=false leaves out the page text; "
      "max_chars cuts it.", {"text": {"type": "boolean"}, "max_chars": {"type": "integer"}})
def _b_view(ctx: Ctx, text: bool = True, max_chars: int | None = None) -> Any:
    return _after_browser(ctx, _browser(ctx).view(max_chars, text))


@tool("browser_click", "Click an element by ref.", {"ref": {"type": "string"}}, ("ref",))
def _b_click(ctx: Ctx, ref: str) -> Any:
    return _after_browser(ctx, _browser(ctx).click(ref))


@tool("browser_type", "Type text into a field (clears it first unless clear=false; submit=true presses "
      "Enter). Card fields are filled only by pay_card.",
      {"ref": {"type": "string"}, "text": {"type": "string"}, "submit": {"type": "boolean"},
       "clear": {"type": "boolean"}}, ("ref", "text"))
def _b_type(ctx: Ctx, ref: str, text: str, submit: bool = False, clear: bool = True) -> Any:
    return _after_browser(ctx, _browser(ctx).type(ref, text, submit, clear))


@tool("browser_select", "Choose an option in a dropdown (by its text or value).",
      {"ref": {"type": "string"}, "option": {"type": "string"}}, ("ref", "option"))
def _b_select(ctx: Ctx, ref: str, option: str) -> Any:
    return _browser(ctx).select(ref, option)


@tool("browser_check", "Tick or untick a checkbox or radio button.",
      {"ref": {"type": "string"}, "checked": {"type": "boolean"}}, ("ref",))
def _b_check(ctx: Ctx, ref: str, checked: bool = True) -> Any:
    return _browser(ctx).check(ref, checked)


@tool("browser_press", "Press a key on the current page (Enter, Tab, Escape, ArrowDown, ...).",
      {"key": {"type": "string"}}, ("key",))
def _b_press(ctx: Ctx, key: str) -> Any:
    return _after_browser(ctx, _browser(ctx).press(key))


@tool("browser_scroll", "Scroll the page by pixels (negative scrolls up).", {"pixels": {"type": "integer"}})
def _b_scroll(ctx: Ctx, pixels: int = 800) -> Any:
    return _browser(ctx).scroll(pixels)


@tool("browser_back", "Go back one page.")
def _b_back(ctx: Ctx) -> Any:
    return _browser(ctx).back()


@tool("browser_wait", "Wait up to `seconds` (max 60), or until `text` appears.",
      {"seconds": {"type": "number"}, "text": {"type": "string"}})
def _b_wait(ctx: Ctx, seconds: float = 2, text: str | None = None) -> Any:
    return _after_browser(ctx, _browser(ctx).wait(seconds, text))


@tool("browser_tabs", "List tabs, switch to one (switch=index) or close one (close=index).",
      {"switch": {"type": "integer"}, "close": {"type": "integer"}})
def _b_tabs(ctx: Ctx, switch: int | None = None, close: int | None = None) -> Any:
    b = _browser(ctx)
    if switch is not None:
        return b.switch_tab(switch)
    if close is not None:
        return b.close_tab(close)
    return b.tabs()


@tool("browser_html", "HTML of the page or of one element (ref).", {"ref": {"type": "string"}})
def _b_html(ctx: Ctx, ref: str | None = None) -> Any:
    return {"html": _browser(ctx).html(ref)}


@tool("browser_screenshot",
      "Take a screenshot of the current page (full_page for the whole page). The image is added to "
      "this conversation for models that accept images. Card fields, payment frames and passwords "
      "appear blacked out.", {"full_page": {"type": "boolean"}})
def _b_shot(ctx: Ctx, full_page: bool = False) -> Any:
    png = _browser(ctx).screenshot(full_page)
    ctx.pending_images.append(base64.b64encode(png).decode())
    return {"screenshot": "attached", "bytes": len(png)}


# ===================================================================== vault
def _own(ctx: Ctx, name: str) -> None:
    def f(d: dict) -> None:
        names = d.setdefault("ai_secrets", [])
        if name not in names:
            names.append(name)
    ctx.world.state.update(f)


def _check_new_name(name: str) -> str | None:
    if not SECRET_NAME.match(name):
        return "name: lowercase letters, digits, _ . - (max 64)"
    if RESERVED.match(name):
        return f"{name!r} is reserved for the keys Lea set up"
    return None


@tool("vault_save",
      "Store a value in the vault under a name without seeing it: either a [hidden#N] handle from "
      "a page or email you were shown, or a browser element (ref) whose value or text is the secret. "
      "Use the name with add_llm_provider, browser_type_secret, etc.",
      {"name": {"type": "string"}, "hidden": {"type": "string"}, "ref": {"type": "string"}}, ("name",))
def _v_save(ctx: Ctx, name: str, hidden: str | None = None, ref: str | None = None) -> Any:
    if err := _check_new_name(name):
        return {"error": err}
    if hidden:
        value = ctx.world.redactor.take(hidden)
    elif ref:
        value = _browser(ctx).raw_value(ref)
    else:
        return {"error": "give hidden or ref"}
    if not value:
        return {"error": "that value is empty"}
    ctx.world.vault.put(name, value)
    _own(ctx, name)
    ctx.world.emit("vault", actor=ctx.actor, action="saved", name=name)
    return {"ok": True, "saved": name}


@tool("vault_generate_password", "Create a random password, store it in the vault under a name, "
      "and never show it. Type it with browser_type_secret.",
      {"name": {"type": "string"}, "length": {"type": "integer"}}, ("name",))
def _v_gen(ctx: Ctx, name: str, length: int = 24) -> Any:
    if err := _check_new_name(name):
        return {"error": err}
    alphabet = string.ascii_letters + string.digits + "!#%+-_=?"
    pw = "".join(secrets.choice(alphabet) for _ in range(max(12, min(int(length), 128))))
    ctx.world.vault.put(name, pw)
    _own(ctx, name)
    return {"ok": True, "saved": name}


@tool("browser_type_secret", "Type a vault value you saved yourself (by name) into a field, "
      "without seeing it.", {"ref": {"type": "string"}, "name": {"type": "string"}}, ("ref", "name"))
def _v_type(ctx: Ctx, ref: str, name: str) -> Any:
    if name not in (ctx.world.state.read().get("ai_secrets") or []):
        return {"error": f"{name!r} is not a secret you saved"}
    _browser(ctx).type_secret(ref, ctx.world.vault.get(name))
    return {"ok": True}


@tool("vault_names", "Free. Names of the secrets you saved (values are never shown).")
def _v_names(ctx: Ctx) -> Any:
    return {"yours": ctx.world.state.read().get("ai_secrets") or []}


# ================================================================== payments
def _set_status(ctx: Ctx, pid: str, status: str) -> dict:
    def f(d: dict) -> None:
        p = d["payments"][pid]
        p["status"] = status
        p["updated_at"] = iso(ctx.world.clock.now())
        alerts = [a for a in d.get("alerts", []) if a.get("payment_id") != pid]
        if status == "waiting_for_bank":
            alerts.append({"kind": "bank_confirmation", "payment_id": pid, "amount": p["amount"],
                           "currency": p["currency"], "where": p["where"], "at": p["updated_at"]})
        d["alerts"] = alerts
    d = ctx.world.state.update(f)
    ctx.world.emit("payment", actor=ctx.actor, payment_id=pid, status=status)
    return d["payments"][pid]


def _charge(ctx: Ctx, p: dict, sign: int) -> None:
    """Ledger entry for a payment (sign=+1) or its reversal (sign=-1)."""
    credit = p.get("credit_service")
    if sign > 0:
        kind = "credit_purchase" if credit else "card_payment"
    else:
        kind = "credit_reversal" if credit else "card_reversal"
    eur = p["amount"] if p["currency"] == "EUR" else round(p["amount_usd"] / ctx.world.fx.eur_usd(), 4)
    ctx.world.ledger.append(kind, actor=p["by"], service=credit or "", amount_usd=p["amount_usd"],
                            amount_eur=eur, what=p["what"], where=p["where"], reason=p["reason"],
                            detail={"payment_id": p["id"], "amount": p["amount"], "currency": p["currency"]},
                            trading=(ctx.world.state.read().get("balances") or {}).get("trading"))


@tool("pay_card",
      "Fill your card (number, expiry, CVC, name) into the checkout open in your browser; you never "
      "see the details. Logged as a card payment of `amount` in `currency` with what, where and "
      "your reason; credit_service names the service when the payment buys prepaid credit (e.g. "
      "xai, tavily). Fields are found automatically, or give refs in `fields` (number, exp, "
      "exp_month, exp_year, cvc, name). You submit the checkout yourself, then report the outcome "
      "with payment_update.",
      {"amount": {"type": "number"}, "currency": {"type": "string"}, "what": {"type": "string"},
       "where": {"type": "string"}, "reason": {"type": "string"}, "credit_service": {"type": "string"},
       "fields": {"type": "object"}}, ("amount", "currency", "what", "where"), phases=ALL_PHASES)
def _pay(ctx: Ctx, amount: float, currency: str, what: str, where: str, reason: str = "",
         credit_service: str | None = None, fields: dict | None = None) -> Any:
    if not ctx.world.vault.has(CARD):
        return {"error": "no card in the vault"}
    amount = round(float(amount), 2)
    if amount <= 0:
        return {"error": "amount must be positive"}
    usd = ctx.world.fx.convert_to_usd(amount, currency)
    filled = _browser(ctx).fill_card(json.loads(ctx.world.vault.get(CARD)), fields)
    pid = uuid.uuid4().hex[:10]
    p = {"id": pid, "at": iso(ctx.world.clock.now()), "what": what, "where": where,
         "amount": amount, "currency": currency.upper(), "amount_usd": usd, "reason": reason,
         "credit_service": (credit_service or "").lower() or None, "status": "pending", "by": ctx.actor}
    ctx.world.state.update(lambda d: d.setdefault("payments", {}).__setitem__(pid, p))
    _charge(ctx, p, +1)
    ctx.world.emit("payment", actor=ctx.actor, payment_id=pid, status="pending", what=what,
                   where=where, amount=amount, currency=p["currency"], reason=reason)
    return {"payment_id": pid, "filled": filled, "amount_usd": usd, "status": "pending"}


@tool("payment_update",
      "Report what happened to a card payment: paid, failed (the money returns to your Wallet), "
      "or waiting_for_bank (the site asks the card owner to confirm in a bank app or by phone code; "
      "Lea is shown this). actual_amount/actual_currency correct the amount if it differed.",
      {"payment_id": {"type": "string"},
       "status": {"type": "string", "enum": ["paid", "failed", "waiting_for_bank"]},
       "actual_amount": {"type": "number"}, "actual_currency": {"type": "string"}},
      ("payment_id", "status"))
def _pay_update(ctx: Ctx, payment_id: str, status: str, actual_amount: float | None = None,
                actual_currency: str | None = None) -> Any:
    pays = ctx.world.state.read().get("payments") or {}
    p = pays.get(payment_id)
    if not p:
        return {"error": f"no payment {payment_id!r}"}
    if p["status"] == "failed":
        return {"error": "that payment is already recorded as failed"}
    if status == "failed":
        _charge(ctx, p, -1)
    elif actual_amount is not None:
        cur = (actual_currency or p["currency"]).upper()
        new_usd = ctx.world.fx.convert_to_usd(float(actual_amount), cur)
        if abs(new_usd - p["amount_usd"]) >= 0.005:
            _charge(ctx, p, -1)
            p = {**p, "amount": round(float(actual_amount), 2), "currency": cur, "amount_usd": new_usd}
            ctx.world.state.update(lambda d: d["payments"][payment_id].update(
                amount=p["amount"], currency=cur, amount_usd=new_usd))
            _charge(ctx, p, +1)
    return _set_status(ctx, payment_id, status)


@tool("payments", "Free. Your card payments and their status.")
def _payments(ctx: Ctx) -> Any:
    return list((ctx.world.state.read().get("payments") or {}).values())


# ===================================================================== email
@tool("email_info", "Free. Your email address and folders.")
def _m_info(ctx: Ctx) -> Any:
    m = ctx.world.mail
    return {"address": m.settings()["address"], "folders": m.folders()}


@tool("email_list", "Free. Messages in a folder, newest first: uid, from, subject, date, seen. "
      "Filters: unseen, search (text), from.",
      {"folder": {"type": "string"}, "unseen": {"type": "boolean"}, "limit": {"type": "integer"},
       "search": {"type": "string"}, "from": {"type": "string"}})
def _m_list(ctx: Ctx, folder: str = "INBOX", unseen: bool = False, limit: int = 20,
            search: str | None = None, **kw) -> Any:
    return ctx.world.mail.list(folder, unseen, limit, search, kw.get("from"))


@tool("email_read", "Free. Read one message (text, links, attachment names); marks it seen.",
      {"uid": {"type": "string"}, "folder": {"type": "string"}}, ("uid",))
def _m_read(ctx: Ctx, uid: str, folder: str = "INBOX") -> Any:
    return ctx.world.mail.read(uid, folder)


@tool("email_send", "Send an email from your address. in_reply_to: a message_id to reply to.",
      {"to": {"type": "string"}, "subject": {"type": "string"}, "body": {"type": "string"},
       "in_reply_to": {"type": "string"}}, ("to", "subject", "body"))
def _m_send(ctx: Ctx, to: str, subject: str, body: str, in_reply_to: str | None = None) -> Any:
    r = ctx.world.mail.send(to, subject, ctx.world.redactor.mask_card(ctx.world.vault.redact(body)),
                            in_reply_to)
    ctx.world.emit("email_sent", actor=ctx.actor, to=to, subject=subject)
    return r
