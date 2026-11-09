"""Everything the dashboard shows, built only from the files on disk.

No AI, no paid API, no secrets: the vault is only asked whether a name exists
and for the card's last 4 digits.
"""

from __future__ import annotations

import csv
import io
import json
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from ..clock import parse
from ..config import Config, Paths
from ..vault import Vault

MAX_FEED = 40
MAX_DECISIONS = 30


def _read_json(p: Path, default):
    try:
        return json.loads(p.read_text())
    except (FileNotFoundError, ValueError):
        return default


def _tail_jsonl(p: Path, n: int) -> list[dict]:
    if not p.exists():
        return []
    with open(p, "rb") as f:
        f.seek(0, 2)
        size = f.tell()
        block = min(size, max(65536, n * 4096))
        f.seek(size - block)
        lines = f.read().decode(errors="replace").splitlines()
    out = []
    for line in lines[-n:]:
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


class LedgerCache:
    """Reads ledger.csv incrementally."""

    def __init__(self, path: Path):
        self.path, self.rows, self._offset, self._header = path, [], 0, None

    def read(self) -> list[dict]:
        if not self.path.exists():
            return self.rows
        size = self.path.stat().st_size
        if size < self._offset:
            self.rows, self._offset, self._header = [], 0, None
        with open(self.path, "rb") as f:
            f.seek(self._offset)
            chunk = f.read()
        end = chunk.rfind(b"\n") + 1
        if end:
            text = chunk[:end].decode(errors="replace")
            lines = text.splitlines()
            if self._header is None and lines:
                self._header = next(csv.reader([lines[0]]))
                lines = lines[1:]
            for r in csv.DictReader(io.StringIO("\n".join(lines)), fieldnames=self._header):
                self.rows.append(r)
            self._offset += end
        return self.rows


def _f(x) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def _ago_days(ts: str | None, now: datetime) -> int | None:
    if not ts:
        return None
    return max(0, int((now - parse(ts)).total_seconds() // 86400))


def describe_wake(cond: dict | None) -> str:
    if not cond:
        return ""
    parts = []
    if cond.get("at"):
        parts.append(f"at {cond['at']}")
    for p in cond.get("price") or []:
        if p.get("move_pct") is not None:
            parts.append(f"if {p['symbol']} moves ±{p['move_pct']:g}%")
        if p.get("above") is not None:
            parts.append(f"if {p['symbol']} reaches {p['above']:g}")
        if p.get("below") is not None:
            parts.append(f"if {p['symbol']} falls to {p['below']:g}")
    return ", or ".join(parts)


def _order_chip(tool: str, raw: str) -> str:
    try:
        a = json.loads(raw)
    except ValueError:
        return tool.replace("_", " ")
    o = a.get("order") or a.get("changes") or a
    if tool == "place_order":
        size = f"${o['notional']}" if o.get("notional") else (o.get("qty") or "")
        kind = o.get("type", "")
        return f"Order placed: {str(o.get('side', '')).upper()} {o.get('symbol', '')} {size} {kind}".strip()
    if tool == "close_position":
        return f"Closed {a.get('symbol', '')}"
    if tool == "cancel_order":
        return "Cancelled all orders" if a.get("order_id") == "all" else "Order cancelled"
    return "Order changed"


class Snapshot:
    def __init__(self, cfg: Config, data_dir: Path, vault: Vault):
        self.cfg, self.data_dir, self.vault = cfg, data_dir, vault
        self._ledgers: dict[str, LedgerCache] = {}

    def mode(self) -> str:
        f = self.data_dir / "current_mode"
        return f.read_text().strip() if f.exists() else "paper"

    def paths(self) -> Paths:
        return Paths.for_mode(self.mode(), self.data_dir)

    def ledger(self, paths: Paths) -> list[dict]:
        key = str(paths.ledger)
        if key not in self._ledgers:
            self._ledgers[key] = LedgerCache(paths.ledger)
        return self._ledgers[key].read()

    # ------------------------------------------------------------------ build
    def build(self, now: datetime | None = None) -> dict:
        now = now or datetime.now(timezone.utc)
        paths = self.paths()
        st = _read_json(paths.state, {"phase": "setup"})
        arch = _read_json(paths.architecture, {"self": {}, "agents": {}, "history": []})
        rows = self.ledger(paths)
        events = _tail_jsonl(paths.events, 400)
        setup = _read_json(paths.setup, {})
        mode = self.mode()
        phase = st.get("phase", "setup")
        out: dict[str, Any] = {
            "now": now.isoformat(timespec="seconds"), "mode": mode, "phase": phase,
            "header": self._header(st, arch, now, mode),
            "alerts": self._alerts(st),
            "setup": self._setup(setup, st, mode),
        }
        if phase in ("genesis", "funding"):
            out["genesis"] = self._genesis(st, rows, events, setup)
        if phase in ("genesis", "funding", "survival", "dead"):
            out.update(self._live(paths, st, arch, rows, events, now))
            out["activity"] = self._activity(paths, st, arch, rows, events, now)
        if phase == "dead":
            out["death"] = self._death(paths, st, now)
        return out

    def _header(self, st: dict, arch: dict, now: datetime, mode: str) -> dict:
        start = st.get("survival_started_at") or st.get("genesis_started_at")
        cond = st.get("wake_conditions")
        return {
            "name": (arch.get("self") or {}).get("name"),
            "alive": st.get("alive", True), "awake": bool(st.get("awake")),
            "paused": bool(st.get("paused")), "phase": st.get("phase"),
            "day": (_ago_days(start, now) or 0) + 1 if start else None,
            "mode": mode, "next_wake": describe_wake(cond), "next_wake_at": (cond or {}).get("at"),
            "distance_usd": st.get("distance_usd"),
        }

    def _alerts(self, st: dict) -> list[dict]:
        out = []
        for a in st.get("alerts") or []:
            if a.get("kind") == "bank_confirmation":
                cur = "€" if a.get("currency") == "EUR" else "$" if a.get("currency") == "USD" else a.get("currency", "") + " "
                out.append({"kind": a["kind"], "text": f"your bank is asking you to confirm a {cur}{_f(a.get('amount')):.2f} "
                                                        f"payment to {a.get('where')}. Open your bank app."})
            elif a.get("kind") == "rent_uncovered":
                out.append({"kind": a["kind"], "text": f"the server rent of €{a.get('rent_eur')} is due on {a.get('due')} "
                                                        f"and its Wallet (${_f(a.get('wallet_usd')):.2f}) can't cover it. "
                                                        f"If the rent fails, the server stops and its positions stay open "
                                                        f"on Alpaca; you may want to close them yourself."})
        w = st.get("watchdog") or {}
        if int(w.get("consecutive_errors") or 0) >= 4:
            out.append({"kind": "alpaca_down", "text": f"Alpaca hasn't answered the watchdog {w['consecutive_errors']} "
                                                        f"times in a row (last: {w.get('last_error')})."})
        return out

    # ------------------------------------------------------------------ setup
    def _setup(self, setup: dict, st: dict, mode: str) -> dict:
        v = self.vault
        has = lambda n: v.has(n)  # noqa: E731
        tests = setup.get("tests", {})
        checks = {
            "card": has("card"),
            "alpaca": bool(tests.get("alpaca", {}).get("ok")) and has(f"alpaca_{mode}_key_id"),
            "xai": bool(tests.get("xai", {}).get("ok")) and has("xai_api_key"),
            "tavily": bool(tests.get("tavily", {}).get("ok")) and has("tavily_api_key"),
            "email": bool(tests.get("email", {}).get("ok")) and has("email_password"),
            "server": setup.get("server_rent") == "free"
                      or (setup.get("server_rent") == "card" and bool((st.get("rent") or {}).get("amount_eur"))),
        }
        return {
            "accounts": setup.get("accounts", {}),
            "card_last4": v.card_last4(), "card_limit_eur": setup.get("card_limit_eur"),
            "saved": {n: has(n) for n in ("card", f"alpaca_{mode}_key_id", f"alpaca_{mode}_secret",
                                          "xai_api_key", "tavily_api_key", "email_address", "email_password")},
            "email_address": v.get("email_address") if has("email_address") else None,
            "tests": tests, "rent": st.get("rent"), "server_rent": setup.get("server_rent"),
            "checks": checks, "ready": all(checks.values()),
        }

    # ---------------------------------------------------------------- genesis
    def _genesis(self, st: dict, rows: list[dict], events: list[dict], setup: dict) -> dict:
        budget = self.cfg.genesis_budget_usd
        used = sum(_f(r["amount_usd"]) for r in rows if r["kind"] in ("llm_call", "api_call"))
        steps = [self._feed_line(e) for e in events if e.get("type") in (
            "tool", "architecture", "memory", "code", "say", "consult", "genesis_ready", "script", "payment")]
        steps = [s for s in steps if s][-25:]
        split = st.get("split")
        out = {"budget": budget, "used": round(used, 4), "steps": steps, "split": split}
        if split:
            rate = (_read_json(self.paths().fx_cache, {}) or {}).get("rate")
            wallet = _f(split.get("wallet_usd"))
            out["instructions"] = {
                "transfer_usd": split["trading_usd"], "card_limit_usd": wallet,
                "card_limit_eur": round(wallet / rate, 2) if rate else None,
                "confirmed": split.get("lea_confirmed_at"),
            }
        return out

    # ------------------------------------------------------------------- live
    def _live(self, paths: Paths, st: dict, arch: dict, rows: list[dict], events: list[dict],
              now: datetime) -> dict:
        b = st.get("balances") or {}
        today = now.date().isoformat()
        hist = _tail_jsonl(paths.history, 5000)
        start_of_day = next((h["nw"] for h in hist if h["t"][:10] == today), None)
        nw = _f(b.get("net_worth"))
        spent_7 = sum(_f(r["amount_usd"]) for r in rows
                      if r["kind"] in ("llm_call", "api_call", "card_payment", "rent")
                      and parse(r["ts"]) >= now - timedelta(days=7))
        first = rows[0]["ts"] if rows else None
        days = max(1.0, min(7.0, (now - parse(first)).total_seconds() / 86400)) if first else 1.0
        rate = spent_7 / days
        spendable = _f(b.get("wallet")) + _f(b.get("credit_total"))
        rent = st.get("rent") or {}
        money = {
            "net_worth": nw, "today_change": round(nw - start_of_day, 4) if start_of_day is not None else None,
            "death_floor": st.get("death_floor"), "distance_usd": st.get("distance_usd"),
            "distance_pct": st.get("distance_pct"), "ath": st.get("ath_usd"),
            "days_since_high": st.get("days_since_high"),
            "floor_rising": (st.get("days_since_high") or 0) > self.cfg.death.grace_days,
            "daily_rise": self.cfg.death.daily_rise_usd, "grace_days": self.cfg.death.grace_days,
            "trading": b.get("trading"), "cash": b.get("cash"), "positions_value": b.get("positions_value"),
            "wallet": b.get("wallet"), "card_last4": self.vault.card_last4(),
            "credit": b.get("credit") or {}, "credit_total": b.get("credit_total"),
            "genesis_credit": b.get("genesis_credit"),
            "spent_today": b.get("spent_today"), "spent_total": b.get("spent_total"),
            "runway_days": round(spendable / rate) if rate > 0 else None,
            "next_rent": {"eur": rent.get("amount_eur"), "due": rent.get("next_due")} if rent else None,
            "as_of": b.get("as_of"),
        }
        step = max(1, len(hist) // 300)
        chart = [{"t": h["t"], "nw": h["nw"], "floor": h.get("floor"), "ath": h.get("ath")}
                 for h in hist[::step]]
        if hist and chart[-1]["t"] != hist[-1]["t"]:
            chart.append(hist[-1])
        return {"money": money, "chart": chart, "mind": self._mind(st, arch, events),
                "models": self._models(arch, rows, today), "scripts": self._scripts(st, rows, now),
                "positions": self._positions(st, rows), "feed": self._feed(events, rows)}

    def _mind(self, st: dict, arch: dict, events: list[dict]) -> dict:
        me = arch.get("self") or {}
        speaking = st.get("speaking")
        bubble = None
        for e in reversed(events):
            if e.get("type") in ("reply", "say") and e.get("text"):
                bubble = {"who": e.get("actor"), "text": e["text"][:600], "at": e.get("at")}
                break
        agents = [{"name": n, "job": a.get("job", "")[:140], "model": a.get("model"),
                   "tools": a.get("tools", [])} for n, a in (arch.get("agents") or {}).items()]
        return {"self": {"name": me.get("name") or "It", "model": me.get("model")},
                "agents": agents, "structure": arch.get("structure") or "",
                "awake": bool(st.get("awake")), "speaking": speaking, "bubble": bubble,
                "woken_by": (st.get("wake") or {}).get("woken_by"),
                "next_wake": describe_wake(st.get("wake_conditions"))}

    def _models(self, arch: dict, rows: list[dict], today: str) -> list[dict]:
        users = defaultdict(set)
        me = arch.get("self") or {}
        if me.get("model"):
            users[me["model"]].add(me.get("name") or "itself")
        for n, a in (arch.get("agents") or {}).items():
            users[a.get("model")].add(n)
        stats: dict[str, dict] = defaultdict(lambda: {"calls": 0, "cost": 0.0, "by": set()})
        for r in rows:
            if r["ts"][:10] != today:
                continue
            if r["kind"] == "llm_call":
                k = r["model"]
            elif r["kind"] == "api_call":
                k = f"{r['service']} (search)" if r["service"] == "tavily" else r["service"]
            else:
                continue
            stats[k]["calls"] += 1
            stats[k]["cost"] += _f(r["amount_usd"])
            stats[k]["by"].add(r["actor"])
        out = []
        for m in set(stats) | {m for m in users if m}:
            st_ = stats.get(m) or {"calls": 0, "cost": 0.0, "by": set()}
            out.append({"model": m, "used_by": sorted(users.get(m, set()) | st_["by"]),
                        "calls_today": st_["calls"], "cost_today": round(st_["cost"], 4)})
        return sorted(out, key=lambda x: (-x["cost_today"], x["model"]))

    def _scripts(self, st: dict, rows: list[dict], now: datetime) -> list[dict]:
        today = now.date().isoformat()
        out = []
        for n, s in (st.get("scripts") or {}).items():
            trades_today = sum(1 for r in rows if r["actor"] == n and r["kind"] == "fill" and r["ts"][:10] == today)
            trades = sum(1 for r in rows if r["actor"] == n and r["kind"] == "fill")
            out.append({"name": n, "path": s.get("path"), "status": s.get("status"),
                        "desired": s.get("desired"), "since": s.get("started_at"),
                        "days": _ago_days(s.get("started_at"), now), "trades_today": trades_today,
                        "trades": trades, "exits": s.get("exits", 0), "reason": s.get("reason", "")})
        return out

    def _positions(self, st: dict, rows: list[dict]) -> list[dict]:
        opener: dict[str, str] = {}
        for r in rows:
            if r["kind"] in ("fill", "order") and r["side"] == "buy" and r["symbol"]:
                opener[r["symbol"].replace("/", "")] = r["actor"]
        return [{"symbol": p["symbol"], "qty": p["qty"], "value": p["market_value"],
                 "pl": p["unrealized_pl"], "opened_by": opener.get(p["symbol"].replace("/", ""), "")}
                for p in st.get("positions") or []]

    # ------------------------------------------------------------------- feed
    def _feed_line(self, e: dict) -> dict | None:
        t, a = e.get("type"), e.get("actor", "")
        text = None
        if t == "wake_start":
            text = f"Woke up: {e.get('woken_by')}"
        elif t == "wake_end":
            text = f"Went to sleep · this wake-up cost ${_f(e.get('cost')):.2f}"
        elif t == "consult":
            text = f"{a} consulted {e.get('agent')}"
        elif t == "reply":
            text = f"{a} answered · ${_f(e.get('cost')):.2f}"
        elif t == "say":
            text = f"{a}: “{(e.get('text') or '')[:160]}”"
        elif t == "architecture":
            what = {"agent_create": f"created agent “{e.get('agent')}”", "agent_delete": f"removed agent “{e.get('agent')}”",
                    "agent_update": f"changed agent “{e.get('agent')}”", "self_update": "changed itself",
                    "structure": "redescribed its structure"}.get(e.get("change"), e.get("change"))
            text = f"{a} {what}" + (f" — {e['reason']}" if e.get("reason") else "")
        elif t == "memory":
            text = f"Memory: {e.get('action')} {e.get('path')}" + (
                f" “{e['preview'][:80]}”" if e.get("preview") and e.get("action") != "forgotten" else "")
        elif t == "code":
            text = f"Code: {e.get('action')} {e.get('path')}"
        elif t == "code_run":
            text = f"Ran {e.get('path')} · exit {e.get('exit_code')} · {e.get('seconds')}s"
        elif t == "script":
            text = f"Script {e.get('name')}: {e.get('action')}"
        elif t == "payment":
            if e.get("what"):
                text = f"Card payment · {e.get('what')} · {e.get('currency')} {_f(e.get('amount')):.2f} · {e.get('where')}"
            else:
                text = f"Payment {e.get('payment_id')}: {str(e.get('status', '')).replace('_', ' ')}"
        elif t == "genesis_ready":
            text = f"Decided it's ready · ${_f(e.get('trading_usd')):.2f} to Alpaca, ${_f(e.get('wallet_usd')):.2f} stays on its card"
        elif t == "tool" and e.get("tool") in ("web_search", "read_url", "github", "market_data", "browser_open"):
            try:
                args = json.loads(e.get("args") or "{}")
            except ValueError:
                args = {}
            detail = args.get("query") or args.get("url") or args.get("path") or ""
            text = f"{a} · {e['tool'].replace('_', ' ')} {str(detail)[:100]}"
        elif t == "email_sent":
            text = f"Sent an email to {e.get('to')}: {e.get('subject')}"
        elif t == "vault":
            text = f"Saved a secret to the vault: {e.get('name')}"
        elif t == "llm_error":
            text = f"{a}: model {e.get('model')} failed — {(e.get('error') or '')[:120]}"
        if not text:
            return None
        return {"at": e.get("at"), "text": text, "type": t}

    def _ledger_line(self, r: dict) -> dict | None:
        k, a = r["kind"], r["actor"]
        amt = _f(r["amount_usd"])
        if k == "fill":
            text = f"{a} {'bought' if r['side'] == 'buy' else 'sold'} {r['qty']} {r['symbol']} at ${_f(r['price']):,.2f}"
        elif k == "order" and r["what"] == "order placed":
            text = f"{a} placed {r['side'].upper()} {r['symbol']}" + (f" ~${amt:.2f}" if amt else "")
        elif k == "order":
            text = f"{a}: {r['what']}" + (f" {r['symbol']}" if r["symbol"] else "")
        elif k == "rent":
            text = f"Server rent · €{_f(r['amount_eur']):.2f} (${amt:.2f})"
        elif k == "death":
            text = "Died: Net Worth reached the death floor"
        elif k == "credit_zeroed":
            text = f"{r['service']} credit ran out"
        elif k == "phase":
            text = r["what"].capitalize()
        else:
            return None
        return {"at": r["ts"], "text": text, "type": k}

    def _feed(self, events: list[dict], rows: list[dict]) -> list[dict]:
        items = [x for x in (self._feed_line(e) for e in events[-200:]) if x]
        items += [x for x in (self._ledger_line(r) for r in rows[-200:]) if x]
        items.sort(key=lambda x: x["at"] or "", reverse=True)
        return items[:MAX_FEED]

    # --------------------------------------------------------------- activity
    def _activity(self, paths: Paths, st: dict, arch: dict, rows: list[dict], events: list[dict],
                  now: datetime) -> dict:
        decisions = []
        for d in reversed(_tail_jsonl(paths.decisions, MAX_DECISIONS)):
            steps, chips = [], []
            for s in d.get("steps", []):
                if s["type"] in ("say", "reply") and s.get("text"):
                    steps.append({"who": s["actor"], "text": s["text"], "cost": s.get("cost"),
                                  "model": s.get("model"), "kind": s["type"]})
                elif s["type"] == "consult":
                    steps.append({"who": s["actor"], "text": f"→ {s['agent']}: {s['message']}", "kind": "consult"})
                elif s["type"] == "tool":
                    tool = s["tool"]
                    if tool in ("place_order", "cancel_order", "replace_order", "close_position"):
                        chips.append(_order_chip(tool, s.get("args", "")))
                    elif tool.startswith("memory_write") or tool == "memory_delete":
                        chips.append("Memory updated")
                    elif tool in ("pay_card", "script_start", "script_stop", "agent_create", "agent_delete",
                                  "self_update", "genesis_ready", "set_wake", "email_send"):
                        chips.append(tool.replace("_", " "))
            mins = None
            if d.get("started") and d.get("ended"):
                mins = round((parse(d["ended"]) - parse(d["started"])).total_seconds() / 60, 1)
            decisions.append({"wake_id": d.get("wake_id"), "started": d.get("started"), "minutes": mins,
                              "woken_by": d.get("woken_by"), "cost": d.get("cost_usd"), "steps": steps,
                              "chips": sorted(set(chips), key=chips.index), "tools": d.get("tools_used", []),
                              "next_wake": describe_wake(d.get("next_wake"))})
        # every dollar: money rows, LLM/API calls folded per wake-up
        dollars, fold = [], None
        for r in reversed(rows[-3000:]):
            k = r["kind"]
            if k in ("llm_call", "api_call"):
                wake = (json.loads(r["detail"] or "{}") or {}).get("wake", "") if r["detail"].startswith("{") else ""
                if fold and fold["wake"] == wake and wake:
                    fold["calls"] += k == "llm_call"
                    fold["searches"] += k == "api_call"
                    fold["amount"] -= _f(r["amount_usd"])
                    fold["by"].add(r["actor"])
                    continue
                fold = {"at": r["ts"], "wake": wake, "calls": int(k == "llm_call"), "searches": int(k == "api_call"),
                        "amount": -_f(r["amount_usd"]), "by": {r["actor"]}, "kind": "compute"}
                dollars.append(fold)
                continue
            fold = None
            what = {"credit_purchase": f"Card → {r['service']} credit", "card_payment": f"Card payment · {r['what']} · {r['where']}",
                    "rent": "Server rent", "fill": f"{r['side'].upper()} {r['symbol']} (filled)",
                    "order": f"{r['what']} {r['side'].upper()} {r['symbol']}".strip(),
                    "card_reversal": f"Payment reversed · {r['what']}", "credit_reversal": f"Credit purchase reversed · {r['service']}",
                    "credit_zeroed": f"{r['service']} credit ran out", "wallet_start": "Its card (Wallet)",
                    "genesis_credit": "Genesis budget", "split": "Trading Treasury funded on Alpaca",
                    "paper_adjust": "Paper test adjustment", "death": "Death", "phase": r["what"]}.get(k, k)
            sign = -1 if k in ("card_payment", "rent") else 1
            dollars.append({"at": r["ts"], "what": what, "by": r["actor"], "amount": sign * _f(r["amount_usd"]) if r["amount_usd"] else None,
                            "reason": r["reason"], "kind": k})
            if len(dollars) >= 300:
                break
        for d in dollars:
            if d.get("kind") == "compute":
                parts = [f"{d['calls']} LLM call{'s' if d['calls'] != 1 else ''}"] if d["calls"] else []
                if d["searches"]:
                    parts.append(f"{d['searches']} search{'es' if d['searches'] != 1 else ''}")
                d["what"] = ", ".join(parts)
                d["by"] = ", ".join(sorted(d["by"]))
                d["amount"] = round(d["amount"], 4)
        # memory now + timeline
        mem_files = []
        for p in sorted(paths.memory.rglob("*")):
            if p.is_file():
                try:
                    txt = p.read_text(errors="replace")
                except OSError:
                    txt = ""
                mem_files.append({"path": str(p.relative_to(paths.memory)), "preview": txt[:400],
                                  "bytes": len(txt.encode())})
        timeline = list(reversed(_tail_jsonl(paths.memory_log, 200)))
        code = [{"path": str(p.relative_to(paths.code)), "bytes": p.stat().st_size}
                for p in sorted(paths.code.rglob("*"))
                if p.is_file() and not str(p.relative_to(paths.code)).startswith((".logs", ".packages"))] \
            if paths.code.exists() else []
        runs = [e for e in events if e.get("type") == "code_run"][-30:]
        return {"decisions": decisions, "dollars": dollars, "memory": mem_files, "memory_timeline": timeline,
                "code": code, "runs": list(reversed(runs)), "scripts": self._scripts(st, rows, now),
                "self_changes": list(reversed(arch.get("history") or []))[:200],
                "structure": arch.get("structure") or ""}

    # ------------------------------------------------------------------ death
    def _death(self, paths: Paths, st: dict, now: datetime) -> dict:
        d = st.get("death") or {}
        liq = d.get("liquidation") or {}
        last = None
        for m in reversed(_tail_jsonl(paths.memory_log, 500)):
            if m.get("action") in ("added", "changed"):
                p = paths.memory / m["path"]
                last = {"path": m["path"], "at": m["at"],
                        "text": p.read_text(errors="replace") if p.exists() else m.get("preview", "")}
                break
        start = st.get("survival_started_at") or st.get("genesis_started_at")
        return {"at": d.get("at"), "net_worth": d.get("net_worth"), "death_floor": d.get("death_floor"),
                "ath": d.get("ath_usd"), "spent_total": d.get("spent_total"),
                "day": (_ago_days(start, parse(d["at"])) or 0) + 1 if start and d.get("at") else None,
                "cancelled": liq.get("cancelled_count"), "closed": liq.get("closed", []),
                "waiting_for_open": liq.get("waiting_for_open", []), "cash_left": liq.get("cash_left"),
                "done": liq.get("done", False),
                "scripts": sorted((st.get("scripts") or {}).keys()), "last_memory": last}
