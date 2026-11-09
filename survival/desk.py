"""The trading desk: the only door to Alpaca for the AI (brain tools and scripts).

Holds the keys, never returns them. Every order, replace and cancel is logged
in the ledger with who placed it, and asks the watchdog to check right away.

Trading is allowed only during Survival and never after death.

Paper mode: the paper account holds more than the AI's Trading Treasury, so the
desk shows a virtual account (cash and equity minus the fixed offset) and
applies the rules a small live cash account has: buys only up to its cash,
no short selling.
"""

from __future__ import annotations

import json
import re
from typing import Any

import httpx

from .broker import key_names
from .clock import Clock
from .config import Paths
from .ledger import Ledger
from .store import JsonStore, file_lock, write_json_atomic
from .vault import Vault

TRADING_BASE = {"paper": "https://paper-api.alpaca.markets", "live": "https://api.alpaca.markets"}
DATA_BASE = "https://data.alpaca.markets"
DATA_PATH = re.compile(r"^v\d[\w]*/[\w./\-%:,]+$")
SYMBOL = re.compile(r"^[A-Za-z0-9./\-]{1,20}$")
ORDER_ID = re.compile(r"^[0-9a-fA-F\-]{8,64}$")


class DeskError(Exception):
    pass


class Desk:
    def __init__(self, mode: str, paths: Paths, vault: Vault, ledger: Ledger,
                 state: JsonStore, clock: Clock | None = None, client: httpx.Client | None = None):
        self.mode, self.paths, self.ledger, self.state = mode, paths, ledger, state
        self.clock = clock or Clock()
        self._vault = vault
        self._client = client or httpx.Client(timeout=30)
        self._orders_by = paths.root / "orders_by.json"

    # ------------------------------------------------------------- plumbing
    def _headers(self) -> dict:
        kid, sec = key_names(self.mode)
        return {"APCA-API-KEY-ID": self._vault.get(kid), "APCA-API-SECRET-KEY": self._vault.get(sec)}

    def _req(self, method: str, url: str, params: dict | None = None, body: Any = None) -> Any:
        try:
            r = self._client.request(method, url, params=_clean(params), json=body,
                                     headers=self._headers())
        except httpx.HTTPError as e:
            raise DeskError(f"Alpaca unreachable: {type(e).__name__}") from None
        if r.status_code >= 400:
            raise DeskError(f"Alpaca {r.status_code}: {self._vault.redact(r.text[:1000])}")
        if not r.content:
            return {"ok": True, "status": r.status_code}
        return r.json()

    def _t(self, method: str, path: str, params: dict | None = None, body: Any = None) -> Any:
        return self._req(method, f"{TRADING_BASE[self.mode]}{path}", params, body)

    @property
    def _offset(self) -> float:
        return float(self.state.read().get("paper_offset") or 0.0) if self.mode == "paper" else 0.0

    def _require_trading(self) -> None:
        st = self.state.read()
        if self.paths.dead_marker.exists() or st.get("phase") == "dead":
            raise DeskError("dead: no trading")
        if st.get("phase") != "survival":
            raise DeskError(f"trading starts with Survival (phase is {st.get('phase')})")

    def _trading_now(self) -> float | None:
        return (self.state.read().get("balances") or {}).get("trading")

    def _remember(self, order_id: str, actor: str) -> None:
        with file_lock(self.paths.lock):
            m = json.loads(self._orders_by.read_text()) if self._orders_by.exists() else {}
            m[order_id] = actor
            write_json_atomic(self._orders_by, m)

    def placed_by(self, order_id: str) -> str:
        if not self._orders_by.exists():
            return ""
        return json.loads(self._orders_by.read_text()).get(order_id, "")

    def _log_order(self, kind_what: str, actor: str, o: dict, reason: str = "") -> None:
        qty = o.get("qty") or ""
        price = o.get("limit_price") or o.get("stop_price") or ""
        notional = o.get("notional")
        est = float(notional) if notional else (float(qty) * float(price) if qty and price else "")
        self.ledger.append("order", actor=actor, symbol=o.get("symbol", ""), side=o.get("side", ""),
                           qty=qty, price=price, order_id=o.get("id", ""),
                           amount_usd=round(est, 4) if est != "" else "", what=kind_what,
                           reason=reason,
                           detail={k: o.get(k) for k in ("type", "time_in_force", "status",
                                                          "order_class", "notional", "trail_percent",
                                                          "trail_price") if o.get(k) is not None},
                           trading=self._trading_now())

    # ------------------------------------------------------------- account
    def account(self) -> dict:
        a = self._t("GET", "/v2/account")
        if self.mode == "live":
            return a
        off = self._offset
        cash = float(a["cash"]) - off
        return {
            "note": "paper account, shown as your Trading Treasury",
            "cash": round(cash, 4),
            "equity": round(float(a["equity"]) - off, 4),
            "long_market_value": a.get("long_market_value"),
            "buying_power": round(self._paper_buying_power(cash), 4),
            "status": a.get("status"), "crypto_status": a.get("crypto_status"),
            "pattern_day_trader": a.get("pattern_day_trader"), "daytrade_count": a.get("daytrade_count"),
            "currency": a.get("currency"),
        }

    def _open_buy_reserve(self) -> float:
        total = 0.0
        for o in self._t("GET", "/v2/orders", {"status": "open", "limit": 500}):
            if o.get("side") != "buy":
                continue
            if o.get("notional"):
                total += float(o["notional"])
            else:
                remaining = float(o.get("qty") or 0) - float(o.get("filled_qty") or 0)
                px = o.get("limit_price") or o.get("stop_price")
                if px:
                    total += remaining * float(px)
        return total

    def _paper_buying_power(self, cash: float | None = None) -> float:
        if cash is None:
            cash = float(self._t("GET", "/v2/account")["cash"]) - self._offset
        return max(0.0, cash - self._open_buy_reserve())

    def _latest_price(self, symbol: str) -> float:
        if "/" in symbol:
            d = self._req("GET", f"{DATA_BASE}/v1beta3/crypto/us/latest/trades", {"symbols": symbol})
            return float(d["trades"][symbol]["p"])
        d = self._req("GET", f"{DATA_BASE}/v2/stocks/{symbol}/trades/latest", {"feed": "iex"})
        return float(d["trade"]["p"])

    def _paper_checks(self, o: dict, freed: float = 0.0) -> None:
        """What a small live cash account enforces: buy only with your cash, no shorting."""
        sym = str(o.get("symbol", ""))
        side = o.get("side")
        if side == "buy":
            if o.get("notional"):
                cost = float(o["notional"])
            else:
                px = o.get("limit_price") or o.get("stop_price") or self._latest_price(sym)
                cost = float(o.get("qty") or 0) * float(px)
            bp = self._paper_buying_power() + freed
            if cost > bp + 0.005:
                raise DeskError(f"insufficient buying power: order ~${cost:.2f}, available ${bp:.2f}")
        elif side == "sell":
            try:
                pos = self._t("GET", f"/v2/positions/{_sym_path(sym)}")
                have = float(pos.get("qty_available") or pos.get("qty") or 0)
            except DeskError:
                have = 0.0
            want = (float(o["qty"]) if o.get("qty")
                    else float(o.get("notional") or 0) / self._latest_price(sym))
            if want > have * 1.0001:
                raise DeskError(f"cannot sell {want:g} {sym}: you hold {have:g} (no short selling)")

    # -------------------------------------------------------------- orders
    def place_order(self, actor: str, order: dict, reason: str = "") -> dict:
        """Any order Alpaca accepts: the fields of POST /v2/orders, passed through."""
        self._require_trading()
        if not isinstance(order, dict) or not order.get("symbol") or not order.get("side"):
            raise DeskError("an order needs at least symbol and side (see Alpaca's POST /v2/orders)")
        if self.mode == "paper":
            self._paper_checks(order)
        o = self._t("POST", "/v2/orders", body=order)
        self._remember(o["id"], actor)
        self._log_order("order placed", actor, o, reason)
        return o

    def replace_order(self, actor: str, order_id: str, changes: dict, reason: str = "") -> dict:
        self._require_trading()
        _check_id(order_id)
        if self.mode == "paper":
            old = self._t("GET", f"/v2/orders/{order_id}")
            new = {**old, **{k: v for k, v in changes.items() if v is not None}}
            freed = 0.0
            if old.get("side") == "buy":
                px = old.get("limit_price") or old.get("stop_price")
                freed = float(old.get("notional") or 0) or (
                    float(old.get("qty") or 0) * float(px) if px else 0.0)
            if new.get("side") == "buy":
                self._paper_checks(new, freed)
        o = self._t("PATCH", f"/v2/orders/{order_id}", body=changes)
        self._remember(o["id"], actor)
        self._log_order(f"order replaced ({order_id})", actor, o, reason)
        return o

    def cancel_order(self, actor: str, order_id: str, reason: str = "") -> dict:
        self._require_trading()
        _check_id(order_id)
        r = self._t("DELETE", f"/v2/orders/{order_id}")
        self.ledger.append("order", actor=actor, order_id=order_id, what="order cancelled",
                           reason=reason, trading=self._trading_now())
        return r

    def cancel_all(self, actor: str, reason: str = "") -> Any:
        self._require_trading()
        r = self._t("DELETE", "/v2/orders")
        self.ledger.append("order", actor=actor, what="all orders cancelled", reason=reason,
                           trading=self._trading_now())
        return r

    def close_position(self, actor: str, symbol: str, qty: float | None = None,
                       percentage: float | None = None, reason: str = "") -> dict:
        self._require_trading()
        _check_symbol(symbol)
        o = self._t("DELETE", f"/v2/positions/{_sym_path(symbol)}",
                    {"qty": qty, "percentage": percentage})
        if isinstance(o, dict) and o.get("id"):
            self._remember(o["id"], actor)
            self._log_order("close position", actor, o, reason)
        return o

    # ---------------------------------------------------------------- reads
    def positions(self) -> Any:
        return self._t("GET", "/v2/positions")

    def orders(self, **params) -> Any:
        return self._t("GET", "/v2/orders", params)

    def get_order(self, order_id: str) -> Any:
        _check_id(order_id)
        return self._t("GET", f"/v2/orders/{order_id}")

    def activities(self, activity_type: str | None = None, **params) -> Any:
        if activity_type and not re.fullmatch(r"[A-Z]{2,8}", activity_type):
            raise DeskError("activity_type like FILL, DIV, CFEE")
        path = "/v2/account/activities" + (f"/{activity_type}" if activity_type else "")
        return self._t("GET", path, params)

    def portfolio_history(self, **params) -> Any:
        h = self._t("GET", "/v2/account/portfolio/history", params)
        off = self._offset
        if off and isinstance(h, dict):
            h["equity"] = [None if e is None else round(e - off, 4) for e in h.get("equity", [])]
            if h.get("base_value") is not None:
                h["base_value"] = round(h["base_value"] - off, 4)
        return h

    def assets(self, **params) -> Any:
        return self._t("GET", "/v2/assets", params)

    def asset(self, symbol: str) -> Any:
        _check_symbol(symbol)
        return self._t("GET", f"/v2/assets/{_sym_path(symbol)}")

    def clock_info(self) -> Any:
        return self._t("GET", "/v2/clock")

    def calendar(self, **params) -> Any:
        return self._t("GET", "/v2/calendar", params)

    def market_data(self, path: str, params: dict | None = None) -> Any:
        """Any GET of Alpaca's market data API (data.alpaca.markets), e.g. v2/stocks/bars,
        v1beta3/crypto/us/bars, v1beta3/crypto/us/latest/orderbooks, v1beta1/news,
        v1beta1/screener/stocks/most-actives."""
        path = path.strip().lstrip("/")
        if not DATA_PATH.match(path) or ".." in path:
            raise DeskError("path like v2/stocks/bars or v1beta3/crypto/us/bars")
        return self._req("GET", f"{DATA_BASE}/{path}", params)

    # ----------------------------------------------------------- fill sync
    def sync_fills(self) -> int:
        """Log new fills in the ledger, attributed to whoever placed the order."""
        st = self.state.read()
        cursor = st.get("fills_cursor") or {}
        params = {"direction": "asc", "page_size": 100}
        if cursor.get("after"):
            params["after"] = cursor["after"]
        acts = self._t("GET", "/v2/account/activities/FILL", params)
        seen = set(cursor.get("ids", []))
        n = 0
        last_time = cursor.get("after")
        for a in acts or []:
            if a.get("id") in seen:
                continue
            qty, price = float(a.get("qty") or 0), float(a.get("price") or 0)
            self.ledger.append("fill", actor=self.placed_by(a.get("order_id", "")) or "unknown",
                               symbol=a.get("symbol", ""), side=a.get("side", ""), qty=qty, price=price,
                               order_id=a.get("order_id", ""), amount_usd=round(qty * price, 4),
                               what=a.get("type", "fill"), trading=self._trading_now(),
                               detail={"activity_id": a.get("id"), "time": a.get("transaction_time")})
            seen.add(a.get("id"))
            last_time = a.get("transaction_time") or last_time
            n += 1
        if n:
            ids = [i for i in seen][-500:]
            self.state.set(fills_cursor={"after": last_time, "ids": ids})
        return n


def _clean(p: dict | None) -> dict | None:
    if not p:
        return None
    out = {}
    for k, v in p.items():
        if v is None:
            continue
        if isinstance(v, (list, tuple)):
            v = ",".join(map(str, v))
        elif isinstance(v, dict):
            v = json.dumps(v)
        elif isinstance(v, bool):
            v = str(v).lower()
        out[k] = v
    return out


def _sym_path(symbol: str) -> str:
    _check_symbol(symbol)
    return symbol.replace("/", "")


def _check_symbol(symbol: str) -> None:
    if not SYMBOL.match(symbol or ""):
        raise DeskError(f"bad symbol {symbol!r}")


def _check_id(order_id: str) -> None:
    if not ORDER_ID.match(order_id or ""):
        raise DeskError(f"bad order id {order_id!r}")
