"""What the AI may see of pages and emails.

- Card number, expiry and CVV never reach it (any spacing, any frame, any field).
- Every value stored in the vault is scrubbed.
- Long key-like strings (API keys, tokens) on pages and in emails are replaced by
  a handle like [hidden#3]. It can store the value in the vault with
  `vault_save` (by handle) without ever reading it.
"""

from __future__ import annotations

import json
import re

from .vault import CARD, Vault

KEY_PREFIXES = (r"sk|pk|rk|xai|tvly|ghp|gho|ghu|ghs|github_pat|AKIA|AIza|hf|gsk|key|api|tok"
                r"|secret|bearer")
KEY_LIKE = re.compile(
    rf"(?<![\w\-])(?:(?:{KEY_PREFIXES})[-_][A-Za-z0-9_\-]{{12,}}"
    r"|(?=[A-Za-z0-9_\-]*\d)(?=[A-Za-z0-9_\-]*[A-Za-z])[A-Za-z0-9_\-]{24,})(?![\w\-])",
    re.IGNORECASE)
URL = re.compile(r"https?://\S+")


class Redactor:
    def __init__(self, vault: Vault):
        self.vault = vault
        self.hidden: dict[str, str] = {}
        self._by_value: dict[str, str] = {}
        self._card_patterns: list[re.Pattern] | None = None
        self.card_values: dict[str, str] = {}
        self._load_card()

    def _load_card(self) -> None:
        if not self.vault.has(CARD):
            self._card_patterns = []
            return
        c = json.loads(self.vault.get(CARD))
        n = c["number"]
        mm = f"{int(c['exp_month']):02d}"
        yyyy = str(c["exp_year"])
        yy = yyyy[-2:]
        self.card_values = {"number": n, "cvv": str(c["cvv"]), "mm": mm, "yy": yy, "yyyy": yyyy}
        num = r"[ \-.]?".join(n)
        exp = rf"(?<!\d)0?{int(mm)}\s*[/\-.]\s*(?:{yyyy}|{yy})(?!\d)"
        self._card_patterns = [re.compile(num), re.compile(exp)]

    def mask_card(self, text: str) -> str:
        for p in self._card_patterns or []:
            text = p.sub("[card]", text)
        return text

    def is_card_value(self, value: str) -> bool:
        """For field values: anything equal to a piece of the card."""
        if not self.card_values or not value:
            return False
        v = re.sub(r"[\s\-./]", "", value)
        c = self.card_values
        return (c["number"] in v or v == c["cvv"]
                or v in (c["mm"] + c["yy"], c["mm"] + c["yyyy"]))

    def hide_keys(self, text: str) -> str:
        def repl(m: re.Match) -> str:
            val = m.group(0)
            h = self._by_value.get(val)
            if h is None:
                h = f"hidden#{len(self.hidden) + 1}"
                self.hidden[h] = val
                self._by_value[val] = h
            return f"[{h}]"

        # leave URLs alone (links must stay usable); hide key-like text elsewhere
        parts, last = [], 0
        for m in URL.finditer(text):
            parts.append(KEY_LIKE.sub(repl, text[last:m.start()]))
            parts.append(m.group(0))
            last = m.end()
        parts.append(KEY_LIKE.sub(repl, text[last:]))
        return "".join(parts)

    def outward(self, text: str, hide_keys: bool = True) -> str:
        text = self.mask_card(self.vault.redact(text or ""))
        return self.hide_keys(text) if hide_keys else text

    def take(self, handle: str) -> str:
        h = handle.strip().strip("[]")
        if h not in self.hidden:
            raise KeyError(f"no hidden value {handle!r} in this wake-up")
        return self.hidden[h]
