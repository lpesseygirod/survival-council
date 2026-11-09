"""Where the AI's own code runs.

bwrap (the server): the code runs as the separate Linux user `council-ai`
inside bubblewrap. It sees the system's programs read-only, its own code folder
at /work, the SDK at /opt/council-sdk, and a private /tmp. It does not see the
game's code, the vault, the ledger, state or its memory, and runs in its own
PID namespace. It has network access (the internet, and the gateway on
127.0.0.1 with its token). File ownership on the server is a second wall: the
protected files belong to another user with mode 0600/0700.

dev (a Mac, paper mode only): no isolation; refused in live mode.
"""

from __future__ import annotations

import json
import os
import platform
import secrets
import shutil
import subprocess
import sys
import time
from datetime import timedelta
from pathlib import Path

from .clock import Clock, iso, parse
from .config import PROJECT_ROOT, Config, Paths
from .store import file_lock, write_json_atomic

# server: a root-owned copy readable by the sandbox user (the game's own folder is not)
SDK_DIR = Path(os.environ.get("COUNCIL_SDK_DIR") or Path(__file__).with_name("sdk"))
INNER_WORK = "/work"
INNER_SDK = "/opt/council-sdk"


class SandboxError(Exception):
    pass


class Tokens:
    """Gateway tokens: which script (or one-off run) a request comes from."""

    def __init__(self, paths: Paths, clock: Clock | None = None):
        self.path, self.lock, self.clock = paths.tokens, paths.lock, clock or Clock()

    def _read(self) -> dict:
        try:
            return json.loads(self.path.read_text())
        except (FileNotFoundError, ValueError):
            return {}

    def _write(self, d: dict) -> None:
        write_json_atomic(self.path, d)
        os.chmod(self.path, 0o600)

    def issue(self, actor: str, ttl_s: float | None = None) -> str:
        tok = secrets.token_urlsafe(32)
        exp = iso(self.clock.now() + timedelta(seconds=ttl_s)) if ttl_s else None
        with file_lock(self.lock):
            d = {k: v for k, v in self._read().items()
                 if not v.get("expires") or parse(v["expires"]) > self.clock.now()}
            d[tok] = {"actor": actor, "expires": exp}
            self._write(d)
        return tok

    def actor(self, token: str) -> str | None:
        v = self._read().get(token or "")
        if not v:
            return None
        if v.get("expires") and parse(v["expires"]) <= self.clock.now():
            return None
        return v["actor"]

    def revoke_actor(self, actor: str) -> None:
        with file_lock(self.lock):
            self._write({k: v for k, v in self._read().items() if v.get("actor") != actor})


class Sandbox:
    def __init__(self, cfg: Config, paths: Paths, mode: str):
        self.cfg, self.paths, self.mode = cfg, paths, mode
        kind = cfg.sandbox_kind
        if kind == "auto":
            kind = "bwrap" if platform.system() == "Linux" else "dev"
        if kind == "dev" and mode == "live":
            raise SandboxError("live mode needs the bwrap sandbox (run it on the server)")
        self.kind = kind
        self.paths.code.mkdir(parents=True, exist_ok=True)
        self.paths.script_logs.mkdir(parents=True, exist_ok=True)

    def _inner_env(self, token: str) -> dict[str, str]:
        work = INNER_WORK if self.kind == "bwrap" else str(self.paths.code)
        sdk = INNER_SDK if self.kind == "bwrap" else str(SDK_DIR)
        return {
            "COUNCIL_GATEWAY": f"http://127.0.0.1:{self.cfg.gateway_port}",
            "COUNCIL_TOKEN": token,
            "PYTHONPATH": f"{sdk}:{work}/.packages",
            "HOME": work,
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "PYTHONUNBUFFERED": "1",
            "MPLCONFIGDIR": "/tmp",
            "LANG": "C.UTF-8",
        }

    def command(self, inner: list[str], token: str) -> tuple[list[str], dict[str, str], str]:
        """(argv, env, cwd) to start `inner` (python args, paths relative to the code folder)."""
        env = self._inner_env(token)
        if self.kind == "dev":
            return [sys.executable, *inner], env, str(self.paths.code)
        bw = shutil.which("bwrap") or "/usr/bin/bwrap"
        py = self.cfg.sandbox_python
        venv = str(Path(py).parent.parent)
        argv = ["sudo", "-n", "-u", self.cfg.sandbox_user, "--", bw,
                "--die-with-parent", "--unshare-all", "--share-net", "--new-session",
                "--ro-bind", "/usr", "/usr",
                "--symlink", "usr/lib", "/lib", "--symlink", "usr/lib64", "/lib64",
                "--symlink", "usr/bin", "/bin", "--symlink", "usr/sbin", "/sbin",
                "--ro-bind", "/etc/resolv.conf", "/etc/resolv.conf",
                "--ro-bind", "/etc/ssl", "/etc/ssl",
                "--ro-bind-try", "/etc/ca-certificates", "/etc/ca-certificates",
                "--ro-bind-try", "/etc/hosts", "/etc/hosts",
                "--ro-bind-try", "/etc/nsswitch.conf", "/etc/nsswitch.conf",
                "--ro-bind-try", "/etc/localtime", "/etc/localtime",
                "--ro-bind", venv, venv,
                "--ro-bind", str(SDK_DIR), INNER_SDK,
                "--bind", str(self.paths.code), INNER_WORK,
                "--tmpfs", "/tmp", "--proc", "/proc", "--dev", "/dev",
                "--chdir", INNER_WORK, "--clearenv"]
        for k, v in env.items():
            argv += ["--setenv", k, v]
        argv += ["--", py, *inner]
        return argv, {"PATH": "/usr/bin:/bin"}, str(PROJECT_ROOT)

    def run(self, inner: list[str], token: str, timeout_s: float, stdin: str | None = None) -> dict:
        argv, env, cwd = self.command(inner, token)
        t0 = time.monotonic()
        try:
            p = subprocess.run(argv, env=env, cwd=cwd, input=stdin, capture_output=True,
                               text=True, timeout=timeout_s)
            code, out, err = p.returncode, p.stdout, p.stderr
        except subprocess.TimeoutExpired as e:
            code = None
            out = (e.stdout or b"").decode(errors="replace") if isinstance(e.stdout, bytes) else (e.stdout or "")
            err = ((e.stderr or b"").decode(errors="replace") if isinstance(e.stderr, bytes) else (e.stderr or "")) \
                + f"\n[stopped after {timeout_s}s]"
        lim = self.cfg.output_max_chars
        return {"exit_code": code, "seconds": round(time.monotonic() - t0, 2),
                "stdout": _cut(out, lim), "stderr": _cut(err, lim)}

    def signal_all(self, sig: str) -> None:
        """Server only: signal every process of the sandbox user (catches anything that
        left its process group). sig: STOP, CONT, KILL."""
        if self.kind != "bwrap":
            return
        subprocess.run(["sudo", "-n", "-u", self.cfg.sandbox_user, "--", "pkill", f"-{sig}",
                        "-u", self.cfg.sandbox_user], capture_output=True, timeout=20)


def _cut(s: str, lim: int) -> str:
    if len(s) <= lim:
        return s
    return s[:lim] + f"\n[output cut at {lim} characters; {len(s)} in total]"
