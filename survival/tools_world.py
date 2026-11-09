"""Tools for the world outside its mind: trading, market data, internet, code, scripts."""

from __future__ import annotations

import re
from typing import Any

from .clock import iso
from .scripts import NAME as SCRIPT_NAME
from .tools import Ctx, tool

SURVIVAL = ("survival",)
CODE_FILE = re.compile(r"^[\w./\-]{1,200}$")


def _code_path(ctx: Ctx, rel: str):
    rel = (rel or "").strip().lstrip("/")
    if not CODE_FILE.match(rel) or ".." in rel.split("/") or rel.startswith(".logs"):
        raise ValueError("path inside your code folder, e.g. bots/grid.py")
    p = (ctx.world.paths.code / rel).resolve()
    if ctx.world.paths.code.resolve() not in p.parents:
        raise ValueError("path is outside your code folder")
    return p


# =================================================================== trading
@tool("trading_account", "Free. Your Alpaca account: cash, equity, buying power, status.")
def _acct(ctx: Ctx) -> Any:
    return ctx.world.desk.account()


@tool("trading_positions", "Free. Your open positions on Alpaca with value and profit/loss.")
def _positions(ctx: Ctx) -> Any:
    return ctx.world.desk.positions()


@tool("trading_orders", "Free. Your orders. Params as Alpaca's GET /v2/orders (status open|closed|all, "
      "limit, after, until, direction, symbols, nested, side).",
      {"params": {"type": "object"}})
def _orders(ctx: Ctx, params: dict | None = None) -> Any:
    return ctx.world.desk.orders(**(params or {}))


@tool("place_order",
      "Place any order Alpaca accepts. `order` holds the fields of Alpaca's POST /v2/orders: "
      "symbol, side, type (market, limit, stop, stop_limit, trailing_stop), qty or notional, "
      "time_in_force (day, gtc, ioc, fok, opg, cls), limit_price, stop_price, trail_price, "
      "trail_percent, extended_hours, client_order_id, order_class (simple, bracket, oco, oto), "
      "take_profit, stop_loss. Logged under your name with the reason you give.",
      {"order": {"type": "object"}, "reason": {"type": "string"}}, ("order",), phases=SURVIVAL)
def _place(ctx: Ctx, order: dict, reason: str = "") -> Any:
    return ctx.world.desk.place_order(ctx.actor, order, reason)


@tool("replace_order", "Change an open order (Alpaca PATCH /v2/orders/{id}: qty, time_in_force, "
      "limit_price, stop_price, trail, client_order_id).",
      {"order_id": {"type": "string"}, "changes": {"type": "object"}, "reason": {"type": "string"}},
      ("order_id", "changes"), phases=SURVIVAL)
def _replace(ctx: Ctx, order_id: str, changes: dict, reason: str = "") -> Any:
    return ctx.world.desk.replace_order(ctx.actor, order_id, changes, reason)


@tool("cancel_order", "Cancel one open order, or all of them with order_id='all'.",
      {"order_id": {"type": "string"}, "reason": {"type": "string"}}, ("order_id",), phases=SURVIVAL)
def _cancel(ctx: Ctx, order_id: str, reason: str = "") -> Any:
    if order_id == "all":
        return ctx.world.desk.cancel_all(ctx.actor, reason)
    return ctx.world.desk.cancel_order(ctx.actor, order_id, reason)


@tool("close_position", "Sell (close) a position, entirely or by qty or percentage.",
      {"symbol": {"type": "string"}, "qty": {"type": "number"}, "percentage": {"type": "number"},
       "reason": {"type": "string"}}, ("symbol",), phases=SURVIVAL)
def _close(ctx: Ctx, symbol: str, qty: float | None = None, percentage: float | None = None,
           reason: str = "") -> Any:
    return ctx.world.desk.close_position(ctx.actor, symbol, qty, percentage, reason)


@tool("trading_history",
      "Free. kind='activities': account activities (fills, fees, ...; activity_type e.g. FILL, CFEE) "
      "with Alpaca's params. kind='portfolio': equity and profit/loss over time (period, timeframe, "
      "start, end, intraday_reporting, pnl_reset).",
      {"kind": {"type": "string", "enum": ["activities", "portfolio"]},
       "activity_type": {"type": "string"}, "params": {"type": "object"}}, ("kind",))
def _history(ctx: Ctx, kind: str, activity_type: str | None = None, params: dict | None = None) -> Any:
    if kind == "portfolio":
        return ctx.world.desk.portfolio_history(**(params or {}))
    return ctx.world.desk.activities(activity_type, **(params or {}))


@tool("trading_reference",
      "Free. kind='assets' (what Alpaca offers; params status, asset_class us_equity|crypto, "
      "exchange), 'asset' (one symbol: tradable, fractionable, min order size...), 'clock' "
      "(market open now, next open/close), 'calendar' (params start, end).",
      {"kind": {"type": "string", "enum": ["assets", "asset", "clock", "calendar"]},
       "symbol": {"type": "string"}, "params": {"type": "object"}}, ("kind",))
def _reference(ctx: Ctx, kind: str, symbol: str | None = None, params: dict | None = None) -> Any:
    d = ctx.world.desk
    if kind == "asset":
        return d.asset(symbol or "")
    if kind == "clock":
        return d.clock_info()
    if kind == "calendar":
        return d.calendar(**(params or {}))
    return d.assets(**(params or {}))


@tool("market_data",
      "Free. Any GET on Alpaca's market data API (data.alpaca.markets). `path` without the host, "
      "e.g. v2/stocks/bars, v2/stocks/snapshots, v2/stocks/quotes/latest, v1beta3/crypto/us/bars, "
      "v1beta3/crypto/us/latest/orderbooks, v1beta3/crypto/us/snapshots, v1beta1/news, "
      "v1beta1/screener/stocks/movers, v1beta1/options/snapshots/{underlying}. `params` as "
      "Alpaca documents them (symbols, timeframe, start, end, limit, feed, ...). The full response "
      "is returned.",
      {"path": {"type": "string"}, "params": {"type": "object"}}, ("path",))
def _market(ctx: Ctx, path: str, params: dict | None = None) -> Any:
    return ctx.world.desk.market_data(path, params)


# ================================================================== internet
@tool("web_search",
      "Tavily web search (paid per credit: basic 1, advanced 2). Options: search_depth "
      "(basic|advanced), max_results, topic (general|news|finance), time_range (day|week|month|year), "
      "start_date, end_date, include_answer, include_raw_content, include_domains, exclude_domains.",
      {"query": {"type": "string"}, "options": {"type": "object"}}, ("query",))
def _search(ctx: Ctx, query: str, options: dict | None = None) -> Any:
    return ctx.world.net.search(ctx.actor, query, **(options or {}))


@tool("web_extract", "Tavily extraction of page contents (paid: 1 credit per 5 pages, advanced 2).",
      {"urls": {"type": "array", "items": {"type": "string"}},
       "extract_depth": {"type": "string", "enum": ["basic", "advanced"]},
       "format": {"type": "string", "enum": ["markdown", "text"]}}, ("urls",))
def _extract(ctx: Ctx, urls: list[str], extract_depth: str = "basic", format: str | None = None) -> Any:
    return ctx.world.net.extract(ctx.actor, urls, extract_depth, format=format)


@tool("read_url",
      "Free. Open any web page or file and read it. mode 'text' (readable text and title), "
      "'html' (the HTML), 'raw' (as served). links=true also lists the page's links.",
      {"url": {"type": "string"}, "mode": {"type": "string", "enum": ["text", "html", "raw"]},
       "links": {"type": "boolean"}}, ("url",))
def _read(ctx: Ctx, url: str, mode: str = "text", links: bool = False) -> Any:
    return ctx.world.net.read_url(url, mode, links)


@tool("github",
      "Free. GET on GitHub's REST API (api.github.com), e.g. search/repositories with "
      "params {q, sort}, repos/{owner}/{repo}, repos/{owner}/{repo}/contents/{path}, "
      "repos/{owner}/{repo}/readme. Raw files: read_url on raw.githubusercontent.com.",
      {"path": {"type": "string"}, "params": {"type": "object"}}, ("path",))
def _github(ctx: Ctx, path: str, params: dict | None = None) -> Any:
    return ctx.world.net.github(path, params)


# ====================================================================== code
@tool("code_list", "Free. Files in your code folder.")
def _code_list(ctx: Ctx) -> Any:
    root = ctx.world.paths.code
    return [{"path": str(p.relative_to(root)), "bytes": p.stat().st_size}
            for p in sorted(root.rglob("*"))
            if p.is_file() and not str(p.relative_to(root)).startswith((".logs", ".packages"))]


@tool("code_read", "Read a file from your code folder.", {"path": {"type": "string"}}, ("path",))
def _code_read(ctx: Ctx, path: str) -> Any:
    return {"path": path, "content": _code_path(ctx, path).read_text(errors="replace")}


@tool("code_write", "Create or overwrite a file in your code folder.",
      {"path": {"type": "string"}, "content": {"type": "string"}}, ("path", "content"))
def _code_write(ctx: Ctx, path: str, content: str) -> Any:
    p = _code_path(ctx, path)
    existed = p.exists()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)
    ctx.world.emit("code", actor=ctx.actor, action="changed" if existed else "added", path=path,
                   bytes=len(content.encode()))
    return {"ok": True}


@tool("code_delete", "Delete a file from your code folder.", {"path": {"type": "string"}}, ("path",))
def _code_delete(ctx: Ctx, path: str) -> Any:
    _code_path(ctx, path).unlink()
    ctx.world.emit("code", actor=ctx.actor, action="deleted", path=path)
    return {"ok": True}


@tool("code_run",
      "Run a Python file from your code folder once, in your sandbox, and get its output "
      "(stdout/stderr, each cut at the configured maximum). Inside, `import council` reaches "
      "Alpaca and can wake you; the internet is reachable directly. timeout_s up to the maximum.",
      {"path": {"type": "string"}, "args": {"type": "array", "items": {"type": "string"}},
       "timeout_s": {"type": "number"}, "stdin": {"type": "string"}}, ("path",))
def _code_run(ctx: Ctx, path: str, args: list[str] | None = None, timeout_s: float = 300,
              stdin: str | None = None) -> Any:
    _code_path(ctx, path)
    timeout = max(1.0, min(float(timeout_s), ctx.world.cfg.run_timeout_max_s))
    actor = f"run:{path}"
    token = ctx.world.tokens.issue(actor, ttl_s=timeout + 60)
    try:
        result = ctx.world.sandbox.run([path, *(args or [])], token, timeout, stdin)
    finally:
        ctx.world.tokens.revoke_actor(actor)
    ctx.world.emit("code_run", actor=ctx.actor, path=path, exit_code=result["exit_code"],
                   seconds=result["seconds"])
    return result


@tool("pip_install", "Install Python packages for your code (pip, into your code folder's "
      ".packages, available to all your code).",
      {"packages": {"type": "array", "items": {"type": "string"}}}, ("packages",))
def _pip(ctx: Ctx, packages: list[str]) -> Any:
    for pkg in packages:
        if not re.fullmatch(r"[A-Za-z0-9_.\-\[\],<>=!~ ]{1,100}", pkg):
            return {"error": f"bad package spec {pkg!r}"}
    token = ctx.world.tokens.issue("pip", ttl_s=900)
    try:
        return ctx.world.sandbox.run(["-m", "pip", "install", "--quiet", "--disable-pip-version-check",
                                      "--target", ".packages", "--upgrade", *packages],
                                     token, 900)
    finally:
        ctx.world.tokens.revoke_actor("pip")


# =================================================================== scripts
@tool("script_start",
      "Start an always-on script: a Python file from your code folder that keeps running in your "
      "sandbox while you sleep, across restarts and reboots, until you stop it. restart=true starts "
      "it again if it exits. Its orders are logged under its name. Scripts cost no LLM credit.",
      {"name": {"type": "string"}, "path": {"type": "string"},
       "args": {"type": "array", "items": {"type": "string"}}, "restart": {"type": "boolean"},
       "reason": {"type": "string"}}, ("name", "path"))
def _script_start(ctx: Ctx, name: str, path: str, args: list[str] | None = None,
                  restart: bool = True, reason: str = "") -> Any:
    if not SCRIPT_NAME.match(name):
        return {"error": "name: letters, digits, _ . - (max 64)"}
    if not _code_path(ctx, path).is_file():
        return {"error": f"no file {path!r} in your code folder"}
    entry = {"path": path, "args": args or [], "restart": bool(restart), "desired": "running",
             "status": "new", "exits": 0, "reason": reason, "created_at": iso(ctx.world.clock.now()),
             "created_by": ctx.actor}
    ctx.world.state.update(lambda d: d.setdefault("scripts", {}).__setitem__(name, entry))
    ctx.world.emit("script", actor=ctx.actor, action="start", name=name, path=path, reason=reason)
    return {"ok": True, "note": "the supervisor starts it within a few seconds"}


@tool("script_stop", "Stop an always-on script (it stays listed; start it again with script_start).",
      {"name": {"type": "string"}, "reason": {"type": "string"}}, ("name",))
def _script_stop(ctx: Ctx, name: str, reason: str = "") -> Any:
    st = ctx.world.state.read().get("scripts") or {}
    if name not in st:
        return {"error": f"no script {name!r}"}
    ctx.world.state.update(lambda d: d["scripts"][name].update(desired="stopped"))
    ctx.world.emit("script", actor=ctx.actor, action="stop", name=name, reason=reason)
    return {"ok": True}


@tool("scripts", "Free. Your scripts: status, starts, exits, and the orders each has placed.")
def _scripts(ctx: Ctx) -> Any:
    scripts = ctx.world.state.read().get("scripts") or {}
    counts: dict[str, int] = {}
    for r in ctx.world.ledger.rows():
        if r["kind"] in ("order", "fill") and r["actor"] in scripts:
            counts[f"{r['actor']}:{r['kind']}"] = counts.get(f"{r['actor']}:{r['kind']}", 0) + 1
    return {n: {**s, "orders": counts.get(f"{n}:order", 0), "fills": counts.get(f"{n}:fill", 0)}
            for n, s in scripts.items()}


@tool("script_log", "Free. The last lines a script printed.",
      {"name": {"type": "string"}, "lines": {"type": "integer"}}, ("name",))
def _script_log(ctx: Ctx, name: str, lines: int = 100) -> Any:
    if not SCRIPT_NAME.match(name):
        return {"error": "bad name"}
    p = ctx.world.paths.script_logs / f"{name}.log"
    if not p.exists():
        return {"name": name, "log": ""}
    return {"name": name, "log": "\n".join(p.read_text(errors="replace").splitlines()[-max(1, int(lines)):])}
