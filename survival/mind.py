"""The AI's self-design (architecture.json) and its memory folder.

Both are entirely its own: it decides what agents exist, what they do, which
model they use, and what to remember or forget. The code only stores it and
keeps a timeline of every change for the owner's dashboard.
"""

from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .clock import Clock, iso
from .config import Config, Paths
from .store import JsonStore, append_jsonl

NAME_RE = re.compile(r"^[\w .'-]{1,40}$")
MAX_MEMORY_FILE = 2_000_000  # bytes; a technical limit, not a policy


def default_architecture(cfg: Config) -> dict:
    return {
        "self": {"name": None, "provider": cfg.first_provider, "model": cfg.first_model,
                 "instructions": "", "options": {}, "fallbacks": []},
        "agents": {},
        "structure": "",
        "history": [],
    }


class Architecture:
    def __init__(self, cfg: Config, paths: Paths, clock: Clock | None = None):
        self.clock = clock or Clock()
        self.store = JsonStore(paths.architecture, paths.lock, default=lambda: default_architecture(cfg))

    def read(self) -> dict:
        return self.store.read()

    def change(self, kind: str, detail: dict, reason: str, fn: Callable[[dict], None]) -> dict:
        def f(d: dict) -> None:
            fn(d)
            d.setdefault("history", []).append(
                {"at": iso(self.clock.now()), "change": kind, "detail": detail, "reason": reason})
        return self.store.update(f)


class Memory:
    def __init__(self, paths: Paths, clock: Clock | None = None):
        self.root = paths.memory
        self.paths = paths
        self.clock = clock or Clock()

    def _resolve(self, rel: str) -> Path:
        rel = (rel or "").strip().lstrip("/")
        if not rel or "\x00" in rel:
            raise ValueError("give a file path inside your memory, e.g. notes/market.md")
        p = (self.root / rel).resolve()
        root = self.root.resolve()
        if p != root and root not in p.parents:
            raise ValueError("path is outside your memory folder")
        return p

    def _log(self, action: str, rel: str, content: str | None = None) -> None:
        rec: dict[str, Any] = {"at": iso(self.clock.now()), "action": action, "path": rel}
        if content is not None:
            rec["bytes"] = len(content.encode())
            rec["preview"] = content[:300]
        append_jsonl(self.paths.memory_log, self.paths.lock, rec)

    def list(self) -> list[dict]:
        out = []
        for p in sorted(self.root.rglob("*")):
            if p.is_file():
                st = p.stat()
                out.append({"path": str(p.relative_to(self.root)), "bytes": st.st_size,
                            "modified": iso(datetime.fromtimestamp(st.st_mtime, tz=timezone.utc))})
        return out

    def read(self, rel: str) -> str:
        p = self._resolve(rel)
        if not p.is_file():
            raise FileNotFoundError(f"no memory file {rel!r}")
        return p.read_text(errors="replace")

    def write(self, rel: str, content: str) -> str:
        if len(content.encode()) > MAX_MEMORY_FILE:
            raise ValueError(f"a single memory file is limited to {MAX_MEMORY_FILE} bytes")
        p = self._resolve(rel)
        existed = p.exists()
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(f".{p.name}.tmp")
        tmp.write_text(content)
        os.replace(tmp, p)
        action = "changed" if existed else "added"
        self._log(action, str(p.relative_to(self.root.resolve())), content)
        return action

    def delete(self, rel: str) -> None:
        p = self._resolve(rel)
        if not p.is_file():
            raise FileNotFoundError(f"no memory file {rel!r}")
        p.unlink()
        self._log("forgotten", str(p.relative_to(self.root.resolve())))
