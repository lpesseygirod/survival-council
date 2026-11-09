"""Always-on scripts: the brain declares them in state["scripts"]; the supervisor
keeps reality in line (start, restart if asked, stop), including after a reboot."""

from __future__ import annotations

import logging
import re
import subprocess
from datetime import timedelta

from .clock import Clock, iso, parse
from .config import Paths
from .sandbox import Sandbox, Tokens
from .store import JsonStore

log = logging.getLogger("council.scripts")
NAME = re.compile(r"^[A-Za-z0-9_.\-]{1,64}$")
LOG_KEEP = 1_000_000
LOG_MAX = 5_000_000


def proc_name(name: str) -> str:
    return f"script:{name}"


class ScriptManager:
    def __init__(self, paths: Paths, state: JsonStore, ai, sandbox: Sandbox, tokens: Tokens,
                 clock: Clock | None = None):
        self.paths, self.state, self.ai, self.sandbox, self.tokens = paths, state, ai, sandbox, tokens
        self.clock = clock or Clock()

    def _log_file(self, name: str):
        p = self.paths.script_logs / f"{name}.log"
        if p.exists() and p.stat().st_size > LOG_MAX:
            tail = p.read_bytes()[-LOG_KEEP:]
            p.write_bytes(tail)
        return open(p, "ab")

    def _start(self, name: str, s: dict) -> dict:
        token = self.tokens.issue(name)
        argv, env, cwd = self.sandbox.command([s["path"], *s.get("args", [])], token)
        with self._log_file(name) as out:
            out.write(f"\n=== start {iso(self.clock.now())} ===\n".encode())
            out.flush()
            p = self.ai.spawn(proc_name(name), argv, env=env, cwd=cwd, stdout=out,
                              stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
        log.info("script %s started (pid %s)", name, p.pid)
        return {"status": "running", "pid": p.pid, "started_at": iso(self.clock.now()),
                "next_start_at": None}

    def on_boot(self) -> None:
        """After a restart or reboot nothing is running yet: a script that was running
        starts again (the restart isn't counted as its exit)."""
        def f(d: dict) -> None:
            for s in (d.get("scripts") or {}).values():
                if s.get("status") == "running":
                    s.update(status="new", pid=None, next_start_at=None)
        self.state.update(f)

    def reconcile(self) -> None:
        if self.ai.stopped or self.paths.dead_marker.exists():
            return
        scripts = self.state.read().get("scripts") or {}
        now = self.clock.now()
        updates: dict[str, dict] = {}
        for name, s in scripts.items():
            if not NAME.match(name):
                continue
            pname = proc_name(name)
            alive = self.ai.is_alive(pname)
            if s.get("desired") == "running":
                if alive:
                    continue
                if s.get("status") == "running":  # it was running: it exited
                    exits = int(s.get("exits", 0)) + 1
                    upd = {"status": "exited", "exits": exits,
                           "last_exit_code": self.ai.returncode(pname),
                           "last_exit_at": iso(now), "pid": None}
                    if s.get("restart", True):
                        delay = min(300, 5 * 2 ** min(exits - 1, 6))
                        upd["next_start_at"] = iso(now + timedelta(seconds=delay))
                    else:
                        upd["desired"] = "stopped"
                        self.tokens.revoke_actor(name)
                    updates[name] = upd
                    continue
                nxt = s.get("next_start_at")
                if s.get("status") in (None, "new", "exited", "stopped") and (not nxt or now >= parse(nxt)):
                    if s.get("status") == "exited" and not s.get("restart", True):
                        continue
                    try:
                        updates[name] = self._start(name, s)
                    except Exception as e:
                        updates[name] = {"status": "failed", "error": str(e)}
            elif alive:
                self.ai.stop(pname)
                self.tokens.revoke_actor(name)
                updates[name] = {"status": "stopped", "pid": None, "stopped_at": iso(now)}
            elif s.get("status") == "running":
                updates[name] = {"status": "stopped", "pid": None}
        if updates:
            def f(d: dict) -> None:
                for n, u in updates.items():
                    if n in d.get("scripts", {}):
                        d["scripts"][n].update(u)
            self.state.update(f)
