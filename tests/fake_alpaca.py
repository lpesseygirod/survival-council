"""A tiny fake of Alpaca's REST API for httpx.MockTransport."""

from __future__ import annotations

import json
import uuid

import httpx


class FakeAlpaca:
    def __init__(self, cash=100_000.0):
        self.cash = cash
        self.positions = {}       # symbol (no slash) -> dict
        self.orders = {}          # id -> dict
        self.fills = []
        self.prices = {"BTC/USD": 100_000.0, "AAPL": 200.0}
        self.calls = []

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def handle(self, req: httpx.Request) -> httpx.Response:
        self.calls.append((req.method, str(req.url)))
        assert req.headers.get("APCA-API-KEY-ID") == "kid" and req.headers.get("APCA-API-SECRET-KEY") == "sec"
        path = req.url.path
        q = dict(req.url.params)
        host = req.url.host
        if host == "data.alpaca.markets":
            if path == "/v1beta3/crypto/us/latest/trades":
                return self.ok({"trades": {s: {"p": self.prices[s]} for s in q["symbols"].split(",")}})
            if path.startswith("/v2/stocks/") and path.endswith("/trades/latest"):
                sym = path.split("/")[3]
                return self.ok({"trade": {"p": self.prices[sym]}})
            return self.ok({"echo": path, "params": q})
        if path == "/v2/account":
            return self.ok({"cash": str(self.cash), "equity": str(self.cash + sum(
                float(p["market_value"]) for p in self.positions.values())),
                "status": "ACTIVE", "crypto_status": "ACTIVE", "currency": "USD"})
        if path == "/v2/orders" and req.method == "GET":
            st = q.get("status", "open")
            return self.ok([o for o in self.orders.values() if st == "all" or o["status"] in ("new", "accepted")])
        if path == "/v2/orders" and req.method == "POST":
            body = json.loads(req.content)
            o = {"id": str(uuid.uuid4()), "status": "accepted", "filled_qty": "0", **body}
            self.orders[o["id"]] = o
            return self.ok(o)
        if path == "/v2/orders" and req.method == "DELETE":
            n = len(self.orders)
            self.orders.clear()
            return self.ok([{"status": 200}] * n)
        if path.startswith("/v2/orders/"):
            oid = path.rsplit("/", 1)[1]
            if req.method == "DELETE":
                self.orders.pop(oid, None)
                return httpx.Response(204)
            if req.method == "PATCH":
                old = self.orders.pop(oid)
                new = {**old, **json.loads(req.content), "id": str(uuid.uuid4())}
                self.orders[new["id"]] = new
                return self.ok(new)
            return self.ok(self.orders[oid])
        if path.startswith("/v2/positions/"):
            sym = path.rsplit("/", 1)[1]
            if sym not in self.positions:
                return httpx.Response(404, json={"message": "position does not exist"})
            if req.method == "DELETE":
                p = self.positions.pop(sym)
                return self.ok({"id": str(uuid.uuid4()), "symbol": p["symbol"], "side": "sell", "qty": p["qty"]})
            return self.ok(self.positions[sym])
        if path == "/v2/positions":
            return self.ok(list(self.positions.values()))
        if path == "/v2/account/activities/FILL":
            return self.ok(self.fills)
        if path == "/v2/account/portfolio/history":
            return self.ok({"equity": [100_000.0, 100_001.5], "base_value": 100_000.0})
        return httpx.Response(404, json={"message": f"fake: {req.method} {path}"})

    @staticmethod
    def ok(data) -> httpx.Response:
        return httpx.Response(200, json=data)
