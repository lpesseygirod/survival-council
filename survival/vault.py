"""Encrypted secret store.

Holds API keys, the card and the email login. Readable only by protected code
(the gateway, the payment tool, the watchdog). The file and its key live in a
directory the AI's sandbox cannot see; on the server they belong to the
protected user with mode 0600.

Values are never printed: `Vault` has no method that returns all secrets, its
repr shows only names, and `redact()` scrubs any stored value out of text
before that text is logged or shown to the AI.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
from pathlib import Path

from cryptography.fernet import Fernet

from .config import PROJECT_ROOT

CARD = "card"  # stored as JSON: {"name", "number", "exp_month", "exp_year", "cvv"}


def default_dir() -> Path:
    return Path(os.environ.get("COUNCIL_SECRETS_DIR", PROJECT_ROOT / "secrets"))


class Vault:
    def __init__(self, directory: Path | None = None):
        self.dir = directory or default_dir()
        self.dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.dir, 0o700)
        self._key_path = self.dir / "vault.key"
        self._data_path = self.dir / "vault.enc"
        self._lock_path = self.dir / ".vault.lock"
        self._fernet = Fernet(self._load_or_create_key())

    def __repr__(self) -> str:
        return f"Vault(names={self.names()})"

    # ---- key + file ---------------------------------------------------
    def _load_or_create_key(self) -> bytes:
        if self._key_path.exists():
            return self._key_path.read_bytes().strip()
        key = Fernet.generate_key()
        fd = os.open(self._key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(key)
        return key

    def _read(self) -> dict[str, str]:
        if not self._data_path.exists():
            return {}
        return json.loads(self._fernet.decrypt(self._data_path.read_bytes()))

    def _write(self, data: dict[str, str]) -> None:
        tmp = self._data_path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(self._fernet.encrypt(json.dumps(data).encode()))
        os.replace(tmp, self._data_path)

    # ---- public API ---------------------------------------------------
    def put(self, name: str, value: str) -> None:
        if not re.fullmatch(r"[a-z0-9_.-]{1,64}", name):
            raise ValueError("secret names are lowercase letters, digits, _ . -")
        with open(self._lock_path, "a") as lk:
            fcntl.flock(lk, fcntl.LOCK_EX)
            data = self._read()
            data[name] = value
            self._write(data)

    def get(self, name: str) -> str:
        """Protected code only. Never pass the result to a prompt, log or page."""
        data = self._read()
        if name not in data:
            raise KeyError(f"no secret named {name!r} in the vault")
        return data[name]

    def has(self, name: str) -> bool:
        return name in self._read()

    def names(self) -> list[str]:
        return sorted(self._read())

    def delete(self, name: str) -> None:
        with open(self._lock_path, "a") as lk:
            fcntl.flock(lk, fcntl.LOCK_EX)
            data = self._read()
            data.pop(name, None)
            self._write(data)

    # ---- card ---------------------------------------------------------
    def put_card(self, name: str, number: str, exp_month: int, exp_year: int, cvv: str) -> None:
        digits = re.sub(r"\D", "", number)
        if not 12 <= len(digits) <= 19:
            raise ValueError("card number looks wrong")
        self.put(CARD, json.dumps({
            "name": name, "number": digits, "exp_month": int(exp_month),
            "exp_year": int(exp_year), "cvv": str(cvv),
        }))

    def card_last4(self) -> str | None:
        if not self.has(CARD):
            return None
        return json.loads(self.get(CARD))["number"][-4:]

    # ---- redaction ----------------------------------------------------
    def secret_strings(self) -> list[str]:
        """Every stored value worth scrubbing, including card number variants."""
        out: list[str] = []
        for name, value in self._read().items():
            if name == CARD:
                card = json.loads(value)
                n = card["number"]
                out += [n, " ".join(n[i:i + 4] for i in range(0, len(n), 4)),
                        "-".join(n[i:i + 4] for i in range(0, len(n), 4))]
            elif len(value) >= 6:
                out.append(value)
        return sorted(set(out), key=len, reverse=True)

    def redact(self, text: str) -> str:
        for s in self.secret_strings():
            text = text.replace(s, "[secret]")
        return text
