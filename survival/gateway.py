"""The gateway: the AI's code reaches Alpaca and wakes the brain only through here.

HTTP on 127.0.0.1, one token per script or one-off run (the token names the
caller, so every order is logged under that script's name). Runs inside the
protected supervisor; keys never leave it.
"""

from __future__ import annotations

import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable

from .clock import Clock, iso
from .config import Paths
from .desk import Desk, DeskError
from .sandbox import Tokens
from .store import JsonStore, append_jsonl
from .watchdog import status_line

log = logging.getLogger("council.gateway")
MAX_BODY = 1_000_000

DESK_FNS = {
    # name: (method, takes actor)
    "account": False, "positions": False, "orders": False, "get_order": False,
    "activities": False, "portfolio_history": False, "assets": False, "asset": False,
    "clock_info": False, "calendar": False, "market_data": False,
    "place_order": True, "replace_order": True, "cancel_order": True, "cancel_all": True,
    "close_position": True,
}


def public_status(st: dict) -> dict:
    keys = ("phase", "balances", "death_floor", "distance_usd", "distance_pct", "ath_usd",
            "days_since_high", "positions")
    out = {k: st.get(k) for k in keys}
    if st.get("phase") == "survival" and st.get("balances"):
        out["status_line"] = status_line(st)
    return out


class Gateway:
    def __init__(self, desk_factory: Callable[[], Desk], paths: Paths, state: JsonStore,
                 tokens: Tokens, port: int, clock: Clock | None = None):
        self._desk_factory, self._desk = desk_factory, None
        self.paths, self.state, self.tokens, self.port = paths, state, tokens, port
        self.clock = clock or Clock()
        self._server: ThreadingHTTPServer | None = None

    @property
    def desk(self) -> Desk:
        if self._desk is None:
            self._desk = self._desk_factory()
        return self._desk

    def handle(self, token: str, fn: str, args: dict) -> tuple[int, dict]:
        actor = self.tokens.actor(token)
        if actor is None:
            return 401, {"error": "unknown or expired token"}
        if self.paths.dead_marker.exists():
            return 403, {"error": "dead"}
        if not isinstance(args, dict):
            return 400, {"error": "args must be an object"}
        try:
            if fn == "wake":
                reason = str(args.get("reason", ""))[:2000]
                append_jsonl(self.paths.wake_requests, self.paths.lock,
                             {"at": iso(self.clock.now()), "actor": actor, "reason": reason})
                return 200, {"result": {"ok": True}}
            if fn == "status":
                return 200, {"result": public_status(self.state.read())}
            if fn not in DESK_FNS:
                return 404, {"error": f"unknown function {fn!r}"}
            method = getattr(self.desk, fn)
            result: Any = method(actor, **args) if DESK_FNS[fn] else method(**args)
            return 200, {"result": result}
        except (DeskError, TypeError, ValueError) as e:
            return 400, {"error": str(e)}
        except Exception as e:
            log.exception("gateway %s failed", fn)
            return 500, {"error": f"{type(e).__name__}"}

    def start(self) -> None:
        gw = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):  # quiet
                pass

            def do_POST(self):
                if self.path != "/call":
                    return self._send(404, {"error": "not found"})
                n = int(self.headers.get("Content-Length") or 0)
                if n > MAX_BODY:
                    return self._send(413, {"error": "too large"})
                try:
                    body = json.loads(self.rfile.read(n) or b"{}")
                except ValueError:
                    return self._send(400, {"error": "invalid JSON"})
                auth = self.headers.get("Authorization", "")
                token = auth[7:] if auth.startswith("Bearer ") else ""
                code, data = gw.handle(token, str(body.get("fn", "")), body.get("args") or {})
                self._send(code, data)

            def _send(self, code: int, data: dict):
                raw = json.dumps(data, default=str).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        self._server = ThreadingHTTPServer(("127.0.0.1", self.port), Handler)
        self._server.daemon_threads = True
        threading.Thread(target=self._server.serve_forever, name="gateway", daemon=True).start()
        log.info("gateway on 127.0.0.1:%d", self.port)

    def stop(self) -> None:
        if self._server:
            self._server.shutdown()
            self._server.server_close()
