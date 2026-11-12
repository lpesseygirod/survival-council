"""The owner's dashboard. Plain code: reads files, never calls an AI or a paid API.

Runs behind Caddy (HTTPS) on 127.0.0.1. Password login; session cookie is
HttpOnly, Secure, SameSite=Strict; every POST also needs the X-Council header.

Its only inputs: the setup wizard, the funding confirmation, pause/resume.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import threading
import time
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

from ..clock import Clock, iso
from ..config import Config, Paths
from ..store import JsonStore, file_lock, write_json_atomic
from ..vault import Vault
from ..watchdog import state_store
from . import checks
from .snapshot import Snapshot

log = logging.getLogger("council.dashboard")
STATIC = Path(__file__).with_name("static")
SESSION_DAYS = 7
MAX_BODY = 100_000
CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
       "font-src https://fonts.gstatic.com; img-src 'self' data:; connect-src 'self'; "
       "frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
PAGES = {"/", "/login", "/setup", "/live", "/activity", "/dead"}
ACCOUNT_ITEMS = ("card", "alpaca", "xai", "tavily", "email", "server")


# ------------------------------------------------------------------ password
def set_password(path: Path, password: str) -> None:
    if len(password) < 12:
        raise ValueError("use at least 12 characters")
    salt = secrets.token_bytes(16)
    h = hashlib.scrypt(password.encode(), salt=salt, n=2**15, r=8, p=1, maxmem=64 * 2**20)
    path.parent.mkdir(parents=True, exist_ok=True)
    write_json_atomic(path, {"salt": salt.hex(), "hash": h.hex(), "n": 2**15, "r": 8, "p": 1})
    os.chmod(path, 0o600)


def check_password(path: Path, password: str) -> bool:
    try:
        d = json.loads(path.read_text())
    except (FileNotFoundError, ValueError):
        return False
    h = hashlib.scrypt(password.encode(), salt=bytes.fromhex(d["salt"]), n=d["n"], r=d["r"], p=d["p"],
                       maxmem=64 * 2**20)
    return hmac.compare_digest(h.hex(), d["hash"])


class Dashboard:
    def __init__(self, cfg: Config, data_dir: Path, vault: Vault, password_file: Path,
                 clock: Clock | None = None, secure_cookie: bool = True,
                 checkers: dict[str, Callable] | None = None,
                 start_genesis: Callable[[str], None] | None = None):
        self.cfg, self.data_dir, self.vault = cfg, data_dir, vault
        self.password_file, self.clock = password_file, clock or Clock()
        self.secure_cookie = secure_cookie
        self.snap = Snapshot(cfg, data_dir, vault)
        # sessions survive a dashboard restart; only token hashes are stored
        self._sessions_file = password_file.with_name("dashboard_sessions.json")
        self.sessions: dict[str, float] = {k: v for k, v in _load(self._sessions_file).items()
                                           if v > time.time()}
        self._fails: list[float] = []
        self._lock = threading.Lock()
        self.checkers = checkers or {
            "alpaca": lambda mode: checks.alpaca(vault, mode),
            "xai": lambda mode: checks.xai(vault),
            "tavily": lambda mode: checks.tavily(vault),
            "email": lambda mode: checks.email(vault),
        }
        self._start_genesis = start_genesis or self._default_start_genesis

    # ------------------------------------------------------------- helpers
    def paths(self) -> Paths:
        return self.snap.paths()

    def mode(self) -> str:
        return self.snap.mode()

    def state(self) -> JsonStore:
        return state_store(self.paths(), self.mode())

    def setup_store(self) -> JsonStore:
        p = self.paths()
        return JsonStore(p.setup, p.lock)

    def _default_start_genesis(self, mode: str) -> None:
        from ..brain import start_genesis
        from ..world import World
        start_genesis(World.build(mode, data_dir=self.data_dir, vault=self.vault))

    # ------------------------------------------------------------- sessions
    def login(self, password: str) -> str | None:
        now = time.time()
        with self._lock:
            self._fails = [t for t in self._fails if now - t < 60]
            if len(self._fails) >= 5:
                return None
        if not check_password(self.password_file, password):
            with self._lock:
                self._fails.append(now)
            return None
        tok = secrets.token_urlsafe(32)
        with self._lock:
            self.sessions[_h(tok)] = now + SESSION_DAYS * 86400
            self._save()
        return tok

    def _save(self) -> None:
        write_json_atomic(self._sessions_file, self.sessions)
        os.chmod(self._sessions_file, 0o600)

    def valid(self, tok: str | None) -> bool:
        if not tok:
            return False
        with self._lock:
            exp = self.sessions.get(_h(tok))
            if exp and exp > time.time():
                return True
            if self.sessions.pop(_h(tok), None) is not None:
                self._save()
            return False

    def logout(self, tok: str) -> None:
        with self._lock:
            self.sessions.pop(_h(tok), None)
            self._save()

    # --------------------------------------------------------------- actions
    def act(self, path: str, body: dict) -> tuple[int, Any]:
        mode, st_store = self.mode(), self.state()
        phase = st_store.read().get("phase", "setup")
        if path == "/api/pause" or path == "/api/resume":
            p = self.paths()
            if path == "/api/pause":
                p.paused_flag.write_text(iso(self.clock.now()))
                st_store.set(paused=True, paused_since=iso(self.clock.now()))
            else:
                p.paused_flag.unlink(missing_ok=True)
                st_store.set(paused=False, paused_since=None)
            return 200, {"ok": True}
        if path == "/api/funding/done":
            if phase != "funding":
                return 409, {"error": "not waiting for funding"}
            st_store.update(lambda d: d["split"].__setitem__("lea_confirmed_at", iso(self.clock.now())))
            return 200, {"ok": True}
        if not path.startswith("/api/setup/"):
            return 404, {"error": "not found"}

        what = path.rsplit("/", 1)[1]
        setup = self.setup_store()
        if what == "account":
            item = body.get("item")
            if item not in ACCOUNT_ITEMS:
                return 400, {"error": "unknown item"}
            setup.update(lambda d: d.setdefault("accounts", {}).__setitem__(item, bool(body.get("done"))))
            return 200, {"ok": True}
        if what == "mode":
            m = body.get("mode")
            if m not in ("paper", "live"):
                return 400, {"error": "paper or live"}
            (self.data_dir / "current_mode").write_text(m)
            Paths.for_mode(m, self.data_dir)
            return 200, {"ok": True, "mode": m}
        if what == "card":
            if phase != "setup":
                return 409, {"error": "the card can only be set before Genesis"}
            num = re.sub(r"\D", "", str(body.get("number", "")))
            try:
                mm, yy = int(body.get("exp_month")), int(body.get("exp_year"))
                yy = yy + 2000 if yy < 100 else yy
                cvv = re.sub(r"\D", "", str(body.get("cvv", "")))
                if not (1 <= mm <= 12 and 2024 <= yy <= 2100 and 3 <= len(cvv) <= 4 and _luhn(num)):
                    raise ValueError
                self.vault.put_card(str(body.get("name", ""))[:80], num, mm, yy, cvv)
            except (TypeError, ValueError):
                return 400, {"error": "check the card number, expiry and CVV"}
            limit = body.get("limit_eur")
            setup.update(lambda d: d.update(card_limit_eur=float(limit) if limit not in (None, "") else None))
            return 200, {"ok": True, "last4": num[-4:]}
        if what in ("alpaca", "xai", "tavily", "email"):
            fields = {
                "alpaca": {"key_id": f"alpaca_{mode}_key_id", "secret": f"alpaca_{mode}_secret"},
                "xai": {"key": "xai_api_key"}, "tavily": {"key": "tavily_api_key"},
                "email": {"address": "email_address", "password": "email_password",
                          "imap": "email_imap", "smtp": "email_smtp"},
            }[what]
            for f, name in fields.items():
                v = str(body.get(f) or "").strip()
                if v:
                    if len(v) > 500 or "\n" in v:
                        return 400, {"error": f"{f} looks wrong"}
                    self.vault.put(name, v)
            needed = [n for f, n in fields.items() if f not in ("imap", "smtp") and not self.vault.has(n)]
            if needed:
                return 400, {"error": "fill in every field"}
            try:
                res = self.checkers[what](mode)
            except Exception as e:
                res = {"ok": False, "error": self.vault.redact(f"{type(e).__name__}: {e}")[:300]}
            res["at"] = iso(self.clock.now())
            setup.update(lambda d: d.setdefault("tests", {}).__setitem__(what, res))
            return 200, res
        if what == "server":
            who = body.get("who_pays")
            if who == "free":
                st_store.update(lambda d: d.pop("rent", None))
                setup.update(lambda d: d.update(server_rent="free"))
                return 200, {"ok": True}
            if who != "card":
                return 400, {"error": "choose who pays the server"}
            try:
                rent = round(float(body["rent_eur"]), 2)
                due = date.fromisoformat(str(body["next_due"])).isoformat()
                if rent <= 0:
                    raise ValueError
            except (KeyError, TypeError, ValueError):
                return 400, {"error": "rent in euros and next payment date"}
            st_store.update(lambda d: d.__setitem__("rent", {**(d.get("rent") or {}), "amount_eur": rent,
                                                            "next_due": due, "period_months": 1,
                                                            "provider": str(body.get("provider") or "")[:40]}))
            setup.update(lambda d: d.update(server_rent="card"))
            return 200, {"ok": True}
        if what == "start-genesis":
            if phase != "setup":
                return 409, {"error": "Genesis has already started"}
            s = self.snap.build(self.clock.now())["setup"]
            if not s["ready"]:
                return 409, {"error": "every check must be ✓ first", "checks": s["checks"]}
            self._start_genesis(mode)
            return 200, {"ok": True}
        return 404, {"error": "not found"}

    # ---------------------------------------------------------------- server
    def watched(self) -> tuple:
        p = self.paths()
        files = [self.data_dir / "current_mode", p.state, p.events, p.ledger, p.architecture,
                 p.decisions, p.memory_log, p.history, p.setup]
        return tuple((f.stat().st_mtime_ns if f.exists() else 0) for f in files)

    def serve(self, host: str = "127.0.0.1", port: int = 8080) -> ThreadingHTTPServer:
        dash = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def _cookie(self) -> str | None:
                for part in (self.headers.get("Cookie") or "").split(";"):
                    k, _, v = part.strip().partition("=")
                    if k == "sc_session":
                        return v
                return None

            def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None):
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Security-Policy", CSP)
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Referrer-Policy", "no-referrer")
                self.send_header("X-Frame-Options", "DENY")
                for k, v in (extra or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(body)

            def _json(self, code: int, data: Any, extra: dict | None = None):
                self._send(code, json.dumps(data, default=str).encode(), "application/json", extra)

            def do_GET(self):
                path = self.path.split("?")[0]
                if path in PAGES:
                    return self._send(200, (STATIC / "app.html").read_bytes(), "text/html; charset=utf-8")
                if path in ("/app.js", "/app.css"):
                    ctype = "text/javascript" if path.endswith(".js") else "text/css"
                    return self._send(200, (STATIC / path[1:]).read_bytes(), ctype + "; charset=utf-8")
                if not dash.valid(self._cookie()):
                    return self._json(401, {"error": "login"})
                if path == "/api/snapshot":
                    return self._json(200, dash.snap.build())
                if path == "/api/stream":
                    return self._stream()
                return self._json(404, {"error": "not found"})

            def _stream(self):
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Accel-Buffering", "no")
                self.end_headers()
                last, beat = None, 0.0
                try:
                    while True:
                        cur = dash.watched()
                        if cur != last:
                            self.wfile.write(b"data: changed\n\n")
                            self.wfile.flush()
                            last = cur
                        elif time.time() - beat > 15:
                            self.wfile.write(b": ping\n\n")
                            self.wfile.flush()
                            beat = time.time()
                        time.sleep(1)
                except (BrokenPipeError, ConnectionResetError, OSError):
                    return

            def do_POST(self):
                path = self.path.split("?")[0]
                n = int(self.headers.get("Content-Length") or 0)
                if n > MAX_BODY:
                    return self._json(413, {"error": "too large"})
                try:
                    body = json.loads(self.rfile.read(n) or b"{}")
                except ValueError:
                    return self._json(400, {"error": "bad JSON"})
                if self.headers.get("X-Council") != "1":
                    return self._json(403, {"error": "missing header"})
                if path == "/api/login":
                    tok = dash.login(str(body.get("password", "")))
                    if not tok:
                        return self._json(401, {"error": "wrong password (or too many tries: wait a minute)"})
                    flags = "HttpOnly; SameSite=Strict; Path=/; Max-Age=%d" % (SESSION_DAYS * 86400)
                    if dash.secure_cookie:
                        flags += "; Secure"
                    return self._json(200, {"ok": True}, {"Set-Cookie": f"sc_session={tok}; {flags}"})
                tok = self._cookie()
                if not dash.valid(tok):
                    return self._json(401, {"error": "login"})
                if path == "/api/logout":
                    dash.logout(tok)
                    return self._json(200, {"ok": True}, {"Set-Cookie": "sc_session=; Max-Age=0; Path=/"})
                try:
                    code, data = dash.act(path, body if isinstance(body, dict) else {})
                except Exception as e:
                    log.exception("dashboard action %s", path)
                    code, data = 500, {"error": dash.vault.redact(f"{type(e).__name__}")}
                return self._json(code, data)

        srv = ThreadingHTTPServer((host, port), H)
        srv.daemon_threads = True
        return srv


def _h(tok: str) -> str:
    return hashlib.sha256(tok.encode()).hexdigest()


def _load(p: Path) -> dict:
    try:
        return json.loads(p.read_text())
    except (FileNotFoundError, ValueError):
        return {}


def _luhn(num: str) -> bool:
    if not 12 <= len(num) <= 19:
        return False
    total, alt = 0, False
    for d in reversed(num):
        n = int(d)
        if alt:
            n = n * 2 - 9 if n > 4 else n * 2
        total += n
        alt = not alt
    return total % 10 == 0
