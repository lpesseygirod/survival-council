"""A real headless browser the AI controls (Chromium via Playwright).

Runs on the protected side with a persistent profile (cookies and logins stay
between wake-ups). The AI sees pages only through `view`, `html` and masked
screenshots; it cannot run its own JavaScript in pages. Card fields, card
values, vault values and key-like text never reach it (see redact.py).
Requests to private or local addresses are blocked.
"""

from __future__ import annotations

import ipaddress
import json
import re
import socket
from typing import Any, Callable
from urllib.parse import urlparse

from .redact import Redactor

PAYMENT_FRAME_HOSTS = ("stripe.com", "stripe.network", "adyen.com", "braintreegateway.com",
                       "braintree-api.com", "checkout.com", "paypal.com", "squareup.com",
                       "recurly.com", "chargebee.com", "paddle.com", "mollie.com", "klarna.com",
                       "worldpay.com", "authorize.net", "spreedly.com", "pay.google.com")
THREEDS = re.compile(r"3d[s_-]?secure|three[-_]?d|/3ds|acs|challenge|authenticat", re.I)
CARD_FIELD = re.compile(r"card.?num|cardnumber|cc-?num|ccnum|\bcvc|cvv|csc|security.?code|"
                        r"card.?code|expir|exp.?date|cc-?exp|name.?on.?card|cardholder", re.I)

INDEX_JS = r"""
(prefix) => {
  const sel = 'a[href], button, input, select, textarea, [role=button], [role=link], [role=checkbox],' +
              '[role=radio], [role=tab], [role=menuitem], [role=option], [contenteditable=true], summary';
  document.querySelectorAll('[data-cref]').forEach(e => e.removeAttribute('data-cref'));
  const out = []; let i = 0;
  for (const el of document.querySelectorAll(sel)) {
    if (el.tagName === 'INPUT' && el.type === 'hidden') continue;
    const r = el.getBoundingClientRect(); const st = getComputedStyle(el);
    if ((r.width === 0 && r.height === 0) || st.visibility === 'hidden' || st.display === 'none') continue;
    const ref = prefix + (++i);
    el.setAttribute('data-cref', ref);
    const lbl = (el.getAttribute('aria-label') || (el.labels && el.labels[0] && el.labels[0].innerText) ||
                 el.getAttribute('placeholder') || el.getAttribute('title') ||
                 (el.tagName === 'INPUT' ? '' : el.innerText) || el.getAttribute('name') || '').trim();
    const o = {ref, tag: el.tagName.toLowerCase(), type: el.getAttribute('type') || el.getAttribute('role') || '',
               label: lbl.replace(/\s+/g, ' ').slice(0, 160), name: el.getAttribute('name') || el.id || '',
               autocomplete: el.getAttribute('autocomplete') || ''};
    if (['INPUT', 'TEXTAREA', 'SELECT'].includes(el.tagName)) o.value = String(el.value || '').slice(0, 300);
    if (el.type === 'checkbox' || el.type === 'radio') o.checked = el.checked;
    if (el.tagName === 'A') o.href = el.href;
    if (el.tagName === 'SELECT') o.options = Array.from(el.options).slice(0, 60).map(x => x.text.trim().slice(0, 60));
    if (el.disabled) o.disabled = true;
    out.push(o);
  }
  return out;
}
"""

MARK_CARD_TEXT_JS = r"""
(digits) => {
  document.querySelectorAll('[data-cmask]').forEach(e => e.removeAttribute('data-cmask'));
  if (!digits) return 0;
  let n = 0;
  const w = document.createTreeWalker(document.body || document, NodeFilter.SHOW_TEXT);
  while (w.nextNode()) {
    const t = w.currentNode.textContent.replace(/\D/g, '');
    if (t.includes(digits) && w.currentNode.parentElement) { w.currentNode.parentElement.setAttribute('data-cmask', '1'); n++; }
  }
  document.querySelectorAll('input, textarea').forEach(e => {
    if (String(e.value || '').replace(/\D/g, '').includes(digits)) { e.setAttribute('data-cmask', '1'); n++; }
  });
  return n;
}
"""


class BrowserError(Exception):
    pass


def _is_public(host: str, resolve: Callable) -> bool:
    try:
        for info in resolve(host, None):
            ip = ipaddress.ip_address(info[4][0])
            if (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast
                    or ip.is_reserved or ip.is_unspecified):
                return False
        return True
    except (socket.gaierror, ValueError):
        return False


class Browser:
    def __init__(self, profile_dir, redactor: Redactor, headless: bool = True,
                 resolve: Callable = socket.getaddrinfo, allow_private: bool = False):
        self.profile_dir, self.r, self.headless = profile_dir, redactor, headless
        self._resolve, self._allow_private = resolve, allow_private
        self._pw = self._ctx = None
        self._page = None
        self._frames: dict[str, Any] = {}
        self._fields: dict[str, dict] = {}
        self._host_ok: dict[str, bool] = {}

    # ------------------------------------------------------------ lifecycle
    def start(self) -> None:
        if self._ctx:
            return
        from playwright.sync_api import sync_playwright

        self._pw = sync_playwright().start()
        self._ctx = self._pw.chromium.launch_persistent_context(
            str(self.profile_dir), headless=self.headless, viewport={"width": 1280, "height": 900},
            locale="en-US", accept_downloads=False)
        self._ctx.route("**/*", self._guard)
        self._page = self._ctx.pages[0] if self._ctx.pages else self._ctx.new_page()

    def close(self) -> None:
        try:
            if self._ctx:
                self._ctx.close()
            if self._pw:
                self._pw.stop()
        finally:
            self._ctx = self._pw = self._page = None

    def _guard(self, route) -> None:
        url = route.request.url
        u = urlparse(url)
        if u.scheme in ("data", "blob", "about"):
            return route.continue_()
        if u.scheme not in ("http", "https") or not u.hostname:
            return route.abort()
        if not self._allow_private:
            ok = self._host_ok.get(u.hostname)
            if ok is None:
                ok = self._host_ok[u.hostname] = _is_public(u.hostname, self._resolve)
            if not ok:
                return route.abort("blockedbyclient")
        return route.continue_()

    @property
    def page(self):
        self.start()
        if self._page is None or self._page.is_closed():
            pages = self._ctx.pages
            self._page = pages[-1] if pages else self._ctx.new_page()
        return self._page

    # --------------------------------------------------------------- index
    def _card_frame(self, frame) -> bool:
        host = urlparse(frame.url or "").hostname or ""
        return any(host == h or host.endswith("." + h) for h in PAYMENT_FRAME_HOSTS)

    def _index(self) -> list[dict]:
        self._frames, self._fields = {}, {}
        items: list[dict] = []
        for i, frame in enumerate(self.page.frames):
            prefix = "e" if frame == self.page.main_frame else f"f{i}e"
            try:
                els = frame.evaluate(INDEX_JS, prefix)
            except Exception:
                continue
            card_frame = self._card_frame(frame)
            for el in els:
                self._frames[el["ref"]] = frame
                el["card_field"] = card_frame or bool(
                    el.get("autocomplete", "").startswith("cc-")
                    or CARD_FIELD.search(" ".join([el.get("name", ""), el.get("label", "")])))
                self._fields[el["ref"]] = el
                items.append(el)
        return items

    def _public_el(self, el: dict) -> dict:
        o = {k: v for k, v in el.items() if v not in ("", None, False) and k != "card_field"}
        if el.get("card_field"):
            o["card_field"] = True
        if "value" in el:
            v = el["value"]
            if el.get("card_field") and v:
                o["value"] = "[card field: hidden]"
            elif el.get("type") == "password" and v:
                o["value"] = "[password: hidden]"
            elif self.r.is_card_value(v) and v:
                o["value"] = "[card]"
        return o

    def _locate(self, ref: str):
        if ref not in self._frames:
            self._index()
        frame = self._frames.get(ref)
        if frame is None:
            raise BrowserError(f"no element {ref!r} on the page (call browser_view to refresh refs)")
        return frame.locator(f'[data-cref="{ref}"]').first

    # ---------------------------------------------------------------- reads
    def view(self, max_chars: int | None = None, text: bool = True) -> dict:
        p = self.page
        els = [self._public_el(e) for e in self._index()]
        out: dict[str, Any] = {"url": p.url, "title": p.title(),
                               "tabs": len(self._ctx.pages), "elements": els}
        if text:
            try:
                t = p.main_frame.inner_text("body", timeout=5000)
            except Exception:
                t = ""
            if max_chars:
                t = t[: int(max_chars)]
            out["text"] = t
        frames = [f.url for f in p.frames if f != p.main_frame and f.url and f.url != "about:blank"]
        if frames:
            out["frames"] = frames
        if any(THREEDS.search(u) for u in frames):
            out["bank_confirmation_frame"] = True
        return json.loads(self.r.outward(json.dumps(out, ensure_ascii=False)))

    def html(self, ref: str | None = None) -> str:
        self._mark_card_text()
        if ref:
            h = self._locate(ref).evaluate("e => e.outerHTML")
        else:
            h = self.page.content()
        # values typed into fields are not in HTML attributes, but scrub anyway
        h = re.sub(r'(<[^>]*data-cmask="1"[^>]*>)', "[card]", h)
        return self.r.outward(h)

    def screenshot(self, full_page: bool = False) -> bytes:
        p = self.page
        self._index()
        self._mark_card_text()
        masks = []
        for ref, el in self._fields.items():
            if el.get("card_field") or el.get("type") == "password" or self.r.is_card_value(el.get("value", "")):
                masks.append(self._frames[ref].locator(f'[data-cref="{ref}"]'))
        for f in p.frames:
            try:
                masks.append(f.locator("[data-cmask]"))
            except Exception:
                pass
        for h in PAYMENT_FRAME_HOSTS:
            masks.append(p.locator(f'iframe[src*="{h}"]'))
        return p.screenshot(full_page=full_page, mask=masks, mask_color="#000000", timeout=15000)

    def _mark_card_text(self) -> None:
        digits = self.r.card_values.get("number", "")
        for f in self.page.frames:
            try:
                f.evaluate(MARK_CARD_TEXT_JS, digits)
            except Exception:
                pass

    def raw_value(self, ref: str) -> str:
        """Protected: an element's value or text, for saving into the vault."""
        el = self._fields.get(ref) or {}
        if el.get("card_field"):
            raise BrowserError("that is a card field")
        loc = self._locate(ref)
        v = loc.evaluate("e => ('value' in e && e.tagName !== 'BUTTON') ? e.value : e.innerText")
        return str(v or "").strip()

    # -------------------------------------------------------------- actions
    def open(self, url: str, new_tab: bool = False) -> dict:
        u = urlparse(url)
        if u.scheme not in ("http", "https"):
            raise BrowserError("only http(s) URLs")
        if new_tab:
            self.start()
            self._page = self._ctx.new_page()
        try:
            self.page.goto(url, wait_until="domcontentloaded", timeout=45000)
        except Exception as e:
            raise BrowserError(self.r.outward(f"could not open {url}: {str(e).splitlines()[0]}")) from None
        self._settle()
        return self.view()

    def _settle(self) -> None:
        try:
            self.page.wait_for_load_state("networkidle", timeout=5000)
        except Exception:
            pass

    def click(self, ref: str) -> dict:
        self._locate(ref).click(timeout=15000)
        self._settle()
        return self.view()

    def type(self, ref: str, text: str, submit: bool = False, clear: bool = True) -> dict:
        el = self._fields.get(ref) or {}
        if el.get("card_field"):
            raise BrowserError("card fields are filled only by pay_card")
        loc = self._locate(ref)
        if clear:
            loc.fill("", timeout=15000)
        loc.press_sequentially(text, delay=20, timeout=60000)
        if submit:
            loc.press("Enter")
            self._settle()
        return self.view()

    def type_secret(self, ref: str, value: str) -> None:
        el = self._fields.get(ref) or {}
        if el.get("card_field"):
            raise BrowserError("card fields are filled only by pay_card")
        loc = self._locate(ref)
        loc.fill("", timeout=15000)
        loc.press_sequentially(value, delay=20, timeout=60000)

    def select(self, ref: str, option: str) -> dict:
        loc = self._locate(ref)
        try:
            loc.select_option(label=option, timeout=10000)
        except Exception:
            loc.select_option(value=option, timeout=10000)
        return self.view()

    def check(self, ref: str, checked: bool = True) -> dict:
        self._locate(ref).set_checked(checked, timeout=10000)
        return self.view()

    def press(self, key: str) -> dict:
        self.page.keyboard.press(key)
        self._settle()
        return self.view()

    def scroll(self, pixels: int = 800) -> dict:
        self.page.mouse.wheel(0, int(pixels))
        self.page.wait_for_timeout(300)
        return self.view()

    def back(self) -> dict:
        self.page.go_back(timeout=30000)
        self._settle()
        return self.view()

    def wait(self, seconds: float = 2, text: str | None = None) -> dict:
        if text:
            try:
                self.page.get_by_text(text).first.wait_for(timeout=min(seconds, 60) * 1000)
            except Exception:
                pass
        else:
            self.page.wait_for_timeout(min(float(seconds), 60) * 1000)
        return self.view()

    def tabs(self) -> list[dict]:
        self.start()
        return [{"index": i, "url": p.url, "title": self.r.outward(p.title()), "current": p == self._page}
                for i, p in enumerate(self._ctx.pages)]

    def switch_tab(self, index: int) -> dict:
        self.start()
        self._page = self._ctx.pages[int(index)]
        self._page.bring_to_front()
        return self.view()

    def close_tab(self, index: int) -> list[dict]:
        self.start()
        self._ctx.pages[int(index)].close()
        self._page = None
        return self.tabs()

    # ------------------------------------------------------------ card fill
    def fill_card(self, card: dict, refs: dict[str, str] | None = None) -> list[str]:
        """Protected: type the card into the checkout. Returns which parts were filled."""
        fields = self._index()
        mm, yyyy = f"{int(card['exp_month']):02d}", str(card["exp_year"])
        yy = yyyy[-2:]

        def find(kind: str) -> str | None:
            if refs and refs.get(kind):
                return refs[kind]
            pats = {
                "number": (r"^cc-number$", r"card.?num|cardnumber|cc-?num|ccnum|credit.?card"),
                "exp": (r"^cc-exp$", r"expir.*(date)?|exp.?date|mm\s*/\s*yy|\bexp\b"),
                "exp_month": (r"^cc-exp-month$", r"exp.*month|month|\bmm\b"),
                "exp_year": (r"^cc-exp-year$", r"exp.*year|year|\byy(yy)?\b"),
                "cvc": (r"^cc-csc$", r"cvc|cvv|csc|security.?code|card.?code"),
                "name": (r"^cc-name$", r"name.?on.?card|cardholder|card.?holder|full.?name"),
            }[kind]
            for el in fields:
                if el["tag"] not in ("input", "select"):
                    continue
                if re.search(pats[0], el.get("autocomplete", ""), re.I):
                    return el["ref"]
            for el in fields:
                if el["tag"] not in ("input", "select"):
                    continue
                hay = " ".join([el.get("name", ""), el.get("label", "")])
                if kind in ("exp_month", "exp_year", "name") and not el.get("card_field") and kind != "name":
                    continue
                if re.search(pats[1], hay, re.I):
                    return el["ref"]
            return None

        filled = []

        def put(ref: str, value: str, part: str) -> None:
            loc = self._locate(ref)
            el = self._fields.get(ref, {})
            if el.get("tag") == "select":
                for opt in (value, value.lstrip("0"), yy if part == "exp_year" else value):
                    try:
                        loc.select_option(value=opt, timeout=5000)
                        break
                    except Exception:
                        try:
                            loc.select_option(label=opt, timeout=5000)
                            break
                        except Exception:
                            continue
            else:
                loc.click(timeout=10000)
                loc.fill("", timeout=10000)
                loc.press_sequentially(value, delay=35, timeout=60000)
            filled.append(part)

        n = find("number")
        if not n:
            raise BrowserError("no card number field found; give its ref in `fields`")
        put(n, card["number"], "number")
        e = find("exp")
        m, y = find("exp_month"), find("exp_year")
        e_el = self._fields.get(e or "", {})
        combined = bool(e) and (e_el.get("autocomplete") == "cc-exp"
                                or re.search(r"mm\s*/\s*yy", e_el.get("label", ""), re.I)
                                or not (m and y and m != y))
        if combined:
            put(e, f"{mm}{yy}", "expiry")
        else:
            if m:
                put(m, mm, "exp_month")
            if y:
                el = self._fields.get(y, {})
                put(y, yyyy if el.get("tag") == "select" or "yyyy" in el.get("label", "").lower() else yy, "exp_year")
        c = find("cvc")
        if c:
            put(c, str(card["cvv"]), "cvc")
        nm = find("name")
        if nm and card.get("name") and nm != n:
            put(nm, card["name"], "name")
        return filled
