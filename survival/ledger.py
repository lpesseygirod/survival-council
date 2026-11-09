"""ledger.csv: every money event, and the pots derived from it.

The Wallet and prepaid credit are not readable from any API (EUR virtual card,
no balance endpoint), so they are the replay of this ledger. The Trading
Treasury always comes live from Alpaca, never from here.

Kinds and their effect on the pots:
  wallet_start      wallet = amount                         (Genesis, $50)
  genesis_credit    genesis credit = amount                 (Lea's $1, not Net Worth)
  split             wallet -= amount                        (the part sent to Alpaca)
  credit_purchase   wallet -= amount, credit[service] += amount
  card_payment      wallet -= amount                        (anything else it buys)
  rent              wallet -= amount                        (known recurring charge)
  llm_call/api_call credit[service] -= amount               (Genesis credit first for xai)
  credit_zeroed     credit[service] = 0                     (out-of-credit error)
  credit_set        credit[service] = amount                (provider reported balance)
  card_reversal     wallet += amount                        (a card payment that didn't go through)
  credit_reversal   wallet += amount, credit[service] -= amount
  paper_adjust      adjust += amount                        (paper-mode test only)
  order, fill, death, phase, info                           (no effect on pots)
"""

from __future__ import annotations

import csv
import io
import json
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from .clock import Clock, iso
from .config import Paths
from .store import file_lock

COLUMNS = [
    "ts", "kind", "actor", "service", "model", "tokens_in", "tokens_out",
    "amount_usd", "amount_eur", "what", "where", "reason",
    "symbol", "side", "qty", "price", "order_id", "detail",
    "wallet_after", "credit_after", "trading_after", "networth_after",
]

SPENDING = {"llm_call", "api_call", "card_payment", "rent"}
GENESIS_SERVICE = "xai"


@dataclass
class Pots:
    wallet: float = 0.0
    credit: dict[str, float] = field(default_factory=dict)
    genesis_credit: float = 0.0
    adjust: float = 0.0  # paper-mode test adjustments only
    spent_total: float = 0.0
    spent_by_day: dict[str, float] = field(default_factory=lambda: defaultdict(float))

    @property
    def credit_total(self) -> float:
        return round(sum(self.credit.values()), 6)

    def net_worth(self, trading: float) -> float:
        """Trading Treasury + Wallet + unused prepaid credit (Genesis credit excluded)."""
        return round(trading + self.wallet + self.credit_total + self.adjust, 6)

    def apply(self, row: dict[str, Any]) -> None:
        kind = row["kind"]
        amt = float(row.get("amount_usd") or 0)
        svc = row.get("service") or ""
        if kind == "wallet_start":
            self.wallet = amt
        elif kind == "genesis_credit":
            self.genesis_credit = amt
        elif kind in ("split", "card_payment", "rent"):
            self.wallet -= amt
        elif kind == "credit_purchase":
            self.wallet -= amt
            self.credit[svc] = self.credit.get(svc, 0.0) + amt
        elif kind in ("llm_call", "api_call"):
            rest = amt
            if svc == GENESIS_SERVICE and self.genesis_credit > 0:
                used = min(self.genesis_credit, rest)
                self.genesis_credit -= used
                rest -= used
            if rest:
                self.credit[svc] = max(0.0, self.credit.get(svc, 0.0) - rest)
        elif kind == "card_reversal":
            self.wallet += amt
            self.spent_total -= amt
            self.spent_by_day[row["ts"][:10]] -= amt
        elif kind == "credit_reversal":
            self.wallet += amt
            self.credit[svc] = max(0.0, self.credit.get(svc, 0.0) - amt)
        elif kind == "credit_zeroed":
            self.credit[svc] = 0.0
            if svc == GENESIS_SERVICE:
                self.genesis_credit = 0.0
        elif kind == "credit_set":
            self.credit[svc] = amt
        elif kind == "paper_adjust":
            self.adjust += amt
        if kind in SPENDING and amt:
            self.spent_total += amt
            self.spent_by_day[row["ts"][:10]] += amt
        self.wallet = round(self.wallet, 6)


class Ledger:
    """Append-only CSV, safe across processes. Keeps a running replay."""

    def __init__(self, paths: Paths, clock: Clock | None = None):
        self.paths, self.clock = paths, clock or Clock()
        self._offset = 0
        self._pots = Pots()
        if not paths.ledger.exists():
            with file_lock(paths.lock):
                if not paths.ledger.exists():
                    paths.ledger.write_text(",".join(COLUMNS) + "\n")

    def _catch_up(self) -> None:
        """Apply rows other processes appended since we last looked."""
        with open(self.paths.ledger, "rb") as f:
            f.seek(self._offset)
            chunk = f.read()
        if not chunk:
            return
        end = chunk.rfind(b"\n") + 1  # only whole lines
        lines = chunk[:end].decode().splitlines()
        if self._offset == 0:
            lines = lines[1:]  # header
        for row in csv.DictReader(io.StringIO("\n".join(lines)), fieldnames=COLUMNS):
            self._pots.apply(row)
        self._offset += end

    def pots(self) -> Pots:
        with file_lock(self.paths.lock):
            self._catch_up()
            return _copy(self._pots)

    def append(self, kind: str, *, trading: float | None = None, **fields: Any) -> dict:
        """Log one money event. `trading` = latest Trading Treasury value, if known."""
        row: dict[str, Any] = {c: "" for c in COLUMNS}
        row.update({k: v for k, v in fields.items() if k in COLUMNS})
        unknown = set(fields) - set(COLUMNS)
        if unknown:
            raise TypeError(f"unknown ledger fields: {sorted(unknown)}")
        row["ts"] = iso(self.clock.now())
        row["kind"] = kind
        if isinstance(row["detail"], (dict, list)):
            row["detail"] = json.dumps(row["detail"], separators=(",", ":"))
        for k in ("what", "where", "reason", "detail"):
            row[k] = str(row[k]).replace("\r", " ").replace("\n", " ")
        with file_lock(self.paths.lock):
            self._catch_up()
            self._pots.apply(row)
            row["wallet_after"] = f"{self._pots.wallet:.4f}"
            row["credit_after"] = json.dumps({k: round(v, 4) for k, v in self._pots.credit.items()},
                                             separators=(",", ":"))
            if trading is not None:
                row["trading_after"] = f"{trading:.4f}"
                row["networth_after"] = f"{self._pots.net_worth(trading):.4f}"
            buf = io.StringIO()
            csv.DictWriter(buf, fieldnames=COLUMNS, lineterminator="\n").writerow(row)
            data = buf.getvalue().encode()
            with open(self.paths.ledger, "ab") as f:
                f.write(data)
            self._offset += len(data)
        if kind in SPENDING or kind in ("order", "fill", "credit_zeroed", "paper_adjust"):
            self.paths.check_request.touch()  # ask the watchdog to check now
        return row

    def rows(self) -> list[dict]:
        with open(self.paths.ledger, newline="") as f:
            return list(csv.DictReader(f))


def _copy(p: Pots) -> Pots:
    q = Pots(p.wallet, dict(p.credit), p.genesis_credit, p.adjust, p.spent_total)
    q.spent_by_day = defaultdict(float, p.spent_by_day)
    return q
