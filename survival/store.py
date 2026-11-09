"""Small JSON file store with cross-process locking and atomic writes."""

from __future__ import annotations

import fcntl
import json
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator


@contextmanager
def file_lock(path: Path) -> Iterator[None]:
    with open(path, "a") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lk, fcntl.LOCK_UN)


def write_json_atomic(path: Path, data: Any) -> None:
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True, default=str))
    os.replace(tmp, path)


class JsonStore:
    def __init__(self, path: Path, lock: Path, default: Callable[[], dict] = dict):
        self.path, self.lock, self._default = path, lock, default

    def read(self) -> dict:
        if not self.path.exists():
            return self._default()
        return json.loads(self.path.read_text())

    def update(self, fn: Callable[[dict], Any]) -> dict:
        """Read-modify-write under the lock. `fn` mutates the dict in place."""
        with file_lock(self.lock):
            data = self.read()
            fn(data)
            write_json_atomic(self.path, data)
            return data

    def set(self, **fields: Any) -> dict:
        return self.update(lambda d: d.update(fields))


def append_jsonl(path: Path, lock: Path, record: dict) -> None:
    with file_lock(lock):
        with open(path, "a") as f:
            f.write(json.dumps(record, default=str) + "\n")
