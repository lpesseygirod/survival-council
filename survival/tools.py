"""The AI's tools. Descriptions state what a tool does, never what to do with it.

Later phases register more tools here (internet, market data, orders, code,
scripts, browser, email, payments) with the same `@tool` decorator.
"""

from __future__ import annotations

import json
import traceback
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Callable

from .clock import iso, parse
from .mind import NAME_RE
from .watchdog import status_line
from .world import World

ALL_PHASES = ("genesis", "funding", "survival")


@dataclass
class Tool:
    name: str
    description: str
    properties: dict
    required: tuple[str, ...]
    fn: Callable[..., Any]
    phases: tuple[str, ...] = ALL_PHASES

    def schema(self) -> dict:
        return {"type": "function", "function": {
            "name": self.name, "description": self.description,
            "parameters": {"type": "object", "properties": self.properties,
                           "required": list(self.required)}}}


TOOLS: dict[str, Tool] = {}


def tool(name: str, description: str, properties: dict | None = None,
         required: tuple[str, ...] = (), phases: tuple[str, ...] = ALL_PHASES):
    def deco(fn):
        TOOLS[name] = Tool(name, description, properties or {}, required, fn, phases)
        return fn
    return deco


@dataclass
class Ctx:
    world: World
    wake_id: str
    actor: str
    depth: int = 0
    consult: Callable[..., str] | None = None
    threads: dict = field(default_factory=dict)
    costs: list = field(default_factory=list)       # every LLM cost in this wake
    trace: list = field(default_factory=list)       # steps for decisions.jsonl
    tools_used: set = field(default_factory=set)
    pending_images: list = field(default_factory=list)  # screenshots to show the model


def available(phase: str, names: list[str] | None = None) -> list[Tool]:
    pool = TOOLS.values() if names is None else [TOOLS[n] for n in names if n in TOOLS]
    return [t for t in pool if phase in t.phases]


def run_tool(ctx: Ctx, name: str, raw_args: str | None, allowed: set[str]) -> str:
    phase = ctx.world.state.read().get("phase")
    t = TOOLS.get(name)
    if t is None or name not in allowed or phase not in t.phases:
        return json.dumps({"error": f"tool {name!r} is not available here"})
    try:
        args = json.loads(raw_args or "{}")
        if not isinstance(args, dict):
            raise ValueError("arguments must be a JSON object")
    except ValueError as e:
        return json.dumps({"error": f"invalid arguments: {e}"})
    ctx.tools_used.add(name)
    try:
        result = t.fn(ctx, **args)
    except TypeError as e:
        result = {"error": f"bad arguments: {e}"}
    except Exception as e:  # tool failures are information for the AI, not crashes
        result = {"error": f"{type(e).__name__}: {e}"}
        ctx.world.emit("tool_error", actor=ctx.actor, tool=name,
                       detail=traceback.format_exc(limit=3))
    out = ctx.world.redact_json(result)
    ctx.world.emit("tool", actor=ctx.actor, tool=name, args=ctx.world.redact_json(args)[:1000],
                   result=out[:1000])
    return out


# =============================================================== status & costs
@tool("status", "Free. Current pots, Total Net Worth, death floor, distance to death, "
      "all-time high, days since last high, spending so far, wake conditions, and the time.")
def _status(ctx: Ctx) -> dict:
    st = ctx.world.state.read()
    pots = ctx.world.ledger.pots()
    out = {
        "time": iso(ctx.world.clock.now()),
        "phase": st.get("phase"),
        "balances": st.get("balances"),
        "wallet_usd": round(pots.wallet, 4),
        "prepaid_credit_usd": {k: round(v, 4) for k, v in pots.credit.items()},
        "genesis_credit_left_usd": round(pots.genesis_credit, 4),
        "spent_total_usd": round(pots.spent_total, 4),
        "spent_today_usd": round(pots.spent_by_day.get(ctx.world.clock.now().date().isoformat(), 0), 4),
        "wake_conditions": st.get("wake_conditions"),
        "rent": st.get("rent"),
    }
    if st.get("phase") == "survival" and st.get("balances"):
        out.update({
            "status_line": status_line(st),
            "death_floor_usd": st.get("death_floor"),
            "distance_to_death_usd": st.get("distance_usd"),
            "distance_to_death_pct": st.get("distance_pct"),
            "all_time_high_usd": st.get("ath_usd"),
            "days_since_last_high": st.get("days_since_high"),
            "positions": st.get("positions"),
        })
    if st.get("split"):
        out["split"] = st["split"]
    return out


@tool("prices", "Free. All recorded prices: LLM models per provider, paid services, trading fees.")
def _prices(ctx: Ctx) -> dict:
    return ctx.world.prices.all()


@tool("estimate_cost",
      "Free. Estimated cost in USD of model calls from recorded prices. Give either an agent "
      "name (uses its model), 'self', or provider+model. `count` multiplies (e.g. calls per day).",
      {"agent": {"type": "string"}, "provider": {"type": "string"}, "model": {"type": "string"},
       "input_tokens": {"type": "integer"}, "output_tokens": {"type": "integer"},
       "cached_tokens": {"type": "integer"}, "count": {"type": "number"}},
      ("input_tokens", "output_tokens"))
def _estimate(ctx: Ctx, input_tokens: int, output_tokens: int, cached_tokens: int = 0,
              count: float = 1, agent: str | None = None, provider: str | None = None,
              model: str | None = None) -> dict:
    arch = ctx.world.arch.read()
    if agent:
        a = arch["self"] if agent == "self" else arch["agents"].get(agent)
        if not a:
            return {"error": f"no agent {agent!r}"}
        provider, model = a["provider"], a["model"]
    if not provider or not model:
        return {"error": "give agent, or provider and model"}
    one = ctx.world.prices.estimate(provider, model, input_tokens, output_tokens, cached_tokens)
    return {"provider": provider, "model": model, "per_call_usd": one,
            "count": count, "total_usd": round(one * count, 6)}


@tool("set_price",
      "Record or update a price. kind='model': USD per 1M tokens (fields input, cached_input, "
      "output, optional long_context_threshold, input_long, cached_input_long, output_long). "
      "kind='service': any fields describing a paid service's prices.",
      {"kind": {"type": "string", "enum": ["model", "service"]},
       "provider": {"type": "string"}, "model": {"type": "string"},
       "service": {"type": "string"}, "fields": {"type": "object"}},
      ("kind", "fields"))
def _set_price(ctx: Ctx, kind: str, fields: dict, provider: str | None = None,
               model: str | None = None, service: str | None = None) -> dict:
    if kind == "model":
        if not provider or not model:
            return {"error": "provider and model are required"}
        for k in ("input", "output"):
            if k not in fields and ctx.world.prices.model(provider, model) is None:
                return {"error": f"a new model needs at least '{k}'"}
        ctx.world.prices.set_model(provider, model, fields)
    else:
        if not service:
            return {"error": "service is required"}
        ctx.world.prices.set_service(service, fields)
    return {"ok": True}


@tool("add_llm_provider",
      "Register an OpenAI-compatible LLM provider. key_name is the name of a key already "
      "saved in the vault. Record its model prices with set_price.",
      {"provider": {"type": "string"}, "base_url": {"type": "string"}, "key_name": {"type": "string"}},
      ("provider", "base_url", "key_name"))
def _add_provider(ctx: Ctx, provider: str, base_url: str, key_name: str) -> dict:
    if not ctx.world.vault.has(key_name):
        return {"error": f"no key named {key_name!r} in the vault"}
    if not base_url.startswith("https://"):
        return {"error": "base_url must be https"}
    ctx.world.prices.set_provider(provider, base_url, key_name)
    return {"ok": True}


# ===================================================================== the mind
def _check_model(ctx: Ctx, provider: str, model: str) -> str | None:
    if not ctx.world.prices.provider(provider):
        return f"unknown provider {provider!r}"
    if ctx.world.prices.model(provider, model) is None:
        return f"no recorded price for {provider}/{model}"
    return None


def _check_tools(names: list[str]) -> str | None:
    bad = [n for n in names if n not in TOOLS]
    return f"unknown tools: {bad}" if bad else None


@tool("self_update",
      "Change yourself: your name, your model (and provider), optional fallback models tried in "
      "order when a call fails, model options (e.g. reasoning_effort, temperature), and your own "
      "instructions, which are given to you as the system message at the start of every wake-up.",
      {"name": {"type": "string"}, "provider": {"type": "string"}, "model": {"type": "string"},
       "fallbacks": {"type": "array", "items": {"type": "object", "properties": {
           "provider": {"type": "string"}, "model": {"type": "string"}}}},
       "options": {"type": "object"}, "instructions": {"type": "string"},
       "reason": {"type": "string"}})
def _self_update(ctx: Ctx, reason: str = "", **fields) -> dict:
    fields = {k: v for k, v in fields.items() if v is not None}
    if "name" in fields and not NAME_RE.match(fields["name"]):
        return {"error": "name: 1-40 letters, digits, spaces, . ' -"}
    cur = ctx.world.arch.read()["self"]
    prov, model = fields.get("provider", cur["provider"]), fields.get("model", cur["model"])
    if "model" in fields or "provider" in fields:
        if err := _check_model(ctx, prov, model):
            return {"error": err}
    for fb in fields.get("fallbacks", []):
        if err := _check_model(ctx, fb.get("provider", "xai"), fb.get("model", "")):
            return {"error": f"fallback: {err}"}
    ctx.world.arch.change("self_update", fields, reason, lambda d: d["self"].update(fields))
    if "name" in fields and ctx.depth == 0:
        ctx.actor = fields["name"]  # its new name shows from its next step on
    ctx.world.emit("architecture", actor=ctx.actor, change="self_update", reason=reason,
                   detail=json.dumps({k: v for k, v in fields.items() if k != "instructions"}))
    return {"ok": True, "self": ctx.world.arch.read()["self"]}


@tool("agent_create",
      "Create an agent: a name, a job (its system message, written by you), a model, the tools "
      "it may use (names of tools listed to you), and optional model options.",
      {"name": {"type": "string"}, "job": {"type": "string"}, "provider": {"type": "string"},
       "model": {"type": "string"}, "tools": {"type": "array", "items": {"type": "string"}},
       "options": {"type": "object"}, "reason": {"type": "string"}},
      ("name", "job", "model"))
def _agent_create(ctx: Ctx, name: str, job: str, model: str, provider: str = "xai",
                  tools: list[str] | None = None, options: dict | None = None,
                  reason: str = "") -> dict:
    if not NAME_RE.match(name):
        return {"error": "name: 1-40 letters, digits, spaces, . ' -"}
    if name in ctx.world.arch.read()["agents"]:
        return {"error": f"agent {name!r} exists; use agent_update"}
    if err := _check_model(ctx, provider, model) or _check_tools(tools or []):
        return {"error": err}
    now = iso(ctx.world.clock.now())
    agent = {"job": job, "provider": provider, "model": model, "tools": tools or [],
             "options": options or {}, "created_at": now, "updated_at": now}
    ctx.world.arch.change("agent_create", {"name": name, **agent}, reason,
                          lambda d: d["agents"].__setitem__(name, agent))
    ctx.world.emit("architecture", actor=ctx.actor, change="agent_create", agent=name,
                   model=model, reason=reason)
    return {"ok": True}


@tool("agent_update", "Change an agent's job, model, provider, tools, options, or rename it.",
      {"name": {"type": "string"}, "new_name": {"type": "string"}, "job": {"type": "string"},
       "provider": {"type": "string"}, "model": {"type": "string"},
       "tools": {"type": "array", "items": {"type": "string"}}, "options": {"type": "object"},
       "reason": {"type": "string"}},
      ("name",))
def _agent_update(ctx: Ctx, name: str, reason: str = "", new_name: str | None = None, **fields) -> dict:
    agents = ctx.world.arch.read()["agents"]
    if name not in agents:
        return {"error": f"no agent {name!r}"}
    fields = {k: v for k, v in fields.items() if v is not None}
    a = {**agents[name], **fields}
    if "model" in fields or "provider" in fields:
        if err := _check_model(ctx, a["provider"], a["model"]):
            return {"error": err}
    if err := _check_tools(a.get("tools", [])):
        return {"error": err}
    if new_name and (not NAME_RE.match(new_name) or new_name in agents):
        return {"error": "new_name is invalid or taken"}
    a["updated_at"] = iso(ctx.world.clock.now())

    def f(d):
        d["agents"].pop(name)
        d["agents"][new_name or name] = a
    ctx.world.arch.change("agent_update", {"name": name, "new_name": new_name, **fields}, reason, f)
    ctx.world.emit("architecture", actor=ctx.actor, change="agent_update", agent=new_name or name,
                   reason=reason)
    return {"ok": True}


@tool("agent_delete", "Delete an agent.",
      {"name": {"type": "string"}, "reason": {"type": "string"}}, ("name",))
def _agent_delete(ctx: Ctx, name: str, reason: str = "") -> dict:
    if name not in ctx.world.arch.read()["agents"]:
        return {"error": f"no agent {name!r}"}
    ctx.world.arch.change("agent_delete", {"name": name}, reason, lambda d: d["agents"].pop(name))
    ctx.world.emit("architecture", actor=ctx.actor, change="agent_delete", agent=name, reason=reason)
    return {"ok": True}


@tool("architecture", "Free. Your current self-design: yourself, your agents, your structure "
      "description, and the history of changes.",
      {"history": {"type": "boolean", "description": "include the full change history"}})
def _architecture(ctx: Ctx, history: bool = False) -> dict:
    a = ctx.world.arch.read()
    if not history:
        a = {**a, "history": f"{len(a.get('history', []))} changes (pass history=true)"}
    return a


@tool("describe_structure",
      "Record a description of how you are organised (shown with your architecture), with a reason.",
      {"description": {"type": "string"}, "reason": {"type": "string"}}, ("description",))
def _structure(ctx: Ctx, description: str, reason: str = "") -> dict:
    ctx.world.arch.change("structure", {"description": description}, reason,
                          lambda d: d.__setitem__("structure", description))
    ctx.world.emit("architecture", actor=ctx.actor, change="structure", detail=description,
                   reason=reason)
    return {"ok": True}


@tool("consult",
      "Send a message to one of your agents and get its reply. The agent runs on its own model "
      "with its own tools. Passing the same `thread` again within this wake-up continues that "
      "conversation; without it the agent starts fresh.",
      {"agent": {"type": "string"}, "message": {"type": "string"}, "thread": {"type": "string"}},
      ("agent", "message"))
def _consult(ctx: Ctx, agent: str, message: str, thread: str | None = None) -> dict:
    if ctx.consult is None:
        return {"error": "consult is unavailable"}
    return ctx.consult(ctx, agent, message, thread)


# ======================================================================= memory
@tool("memory_list", "Free. Your memory files with size and last change.")
def _mem_list(ctx: Ctx) -> dict:
    st = ctx.world.state.read()
    return {"files": ctx.world.memory.list(), "autoload": st.get("memory_autoload", [])}


@tool("memory_read", "Read one memory file.", {"path": {"type": "string"}}, ("path",))
def _mem_read(ctx: Ctx, path: str) -> dict:
    return {"path": path, "content": ctx.world.memory.read(path)}


@tool("memory_write", "Create or overwrite a memory file (text).",
      {"path": {"type": "string"}, "content": {"type": "string"}}, ("path", "content"))
def _mem_write(ctx: Ctx, path: str, content: str) -> dict:
    action = ctx.world.memory.write(path, content)
    ctx.world.emit("memory", actor=ctx.actor, action=action, path=path, preview=content[:300])
    return {"ok": True, "action": action}


@tool("memory_delete", "Forget (delete) a memory file.", {"path": {"type": "string"}}, ("path",))
def _mem_delete(ctx: Ctx, path: str) -> dict:
    ctx.world.memory.delete(path)
    ctx.world.state.update(lambda d: d.__setitem__(
        "memory_autoload", [p for p in d.get("memory_autoload", []) if p != path]))
    ctx.world.emit("memory", actor=ctx.actor, action="forgotten", path=path)
    return {"ok": True}


@tool("memory_autoload",
      "Choose which memory files are loaded automatically at the start of every wake-up "
      "(replaces the current list; an empty list loads nothing).",
      {"paths": {"type": "array", "items": {"type": "string"}}}, ("paths",))
def _mem_autoload(ctx: Ctx, paths: list[str]) -> dict:
    for p in paths:
        ctx.world.memory._resolve(p)
    ctx.world.state.set(memory_autoload=list(paths))
    return {"ok": True, "autoload": paths}


# ======================================================================== waking
@tool("set_wake",
      "Set the conditions that wake you next (replaces any current ones). Any of: `at` (ISO "
      "time, UTC), `in_minutes`, and `price` triggers: [{symbol, move_pct}] fires when the price "
      "moves that many percent either way from its price now; [{symbol, above}] / [{symbol, below}] "
      "fire at a level. Symbols as Alpaca writes them (e.g. BTC/USD, AAPL). Your scripts can also "
      "wake you. The first condition met wakes you.",
      {"at": {"type": "string"}, "in_minutes": {"type": "number"},
       "price": {"type": "array", "items": {"type": "object", "properties": {
           "symbol": {"type": "string"}, "move_pct": {"type": "number"},
           "above": {"type": "number"}, "below": {"type": "number"}}, "required": ["symbol"]}},
       "note": {"type": "string"}})
def _set_wake(ctx: Ctx, at: str | None = None, in_minutes: float | None = None,
              price: list[dict] | None = None, note: str = "") -> dict:
    now = ctx.world.clock.now()
    when = None
    if at:
        when = parse(at)
    if in_minutes is not None:
        t = now + timedelta(minutes=float(in_minutes))
        when = min(when, t) if when else t
    triggers = []
    for p in price or []:
        if not any(k in p for k in ("move_pct", "above", "below")):
            return {"error": f"price trigger for {p.get('symbol')} needs move_pct, above or below"}
        triggers.append({**p, "ref_price": None})
    cond = {"at": iso(when) if when else None, "price": triggers, "note": note,
            "set_at": iso(now), "set_by": ctx.actor}
    ctx.world.state.set(wake_conditions=cond)
    ctx.world.emit("wake_set", actor=ctx.actor, conditions=json.dumps(cond))
    return {"ok": True, "wake_conditions": cond}


# ======================================================================= genesis
@tool("genesis_ready",
      "End Genesis: choose how many dollars go to Alpaca for trading. The rest of your Wallet "
      "stays on your card. Survival starts when the deposit appears on Alpaca.",
      {"trading_usd": {"type": "number"}, "reason": {"type": "string"}}, ("trading_usd",),
      phases=("genesis",))
def _genesis_ready(ctx: Ctx, trading_usd: float, reason: str = "") -> dict:
    wallet = ctx.world.ledger.pots().wallet
    x = round(float(trading_usd), 2)
    if x < 0 or x > round(wallet, 2):
        return {"error": f"trading_usd must be between 0 and your Wallet (${wallet:.2f})"}
    now = iso(ctx.world.clock.now())
    split = {"trading_usd": x, "wallet_usd": round(wallet - x, 2), "reason": reason, "decided_at": now}
    ctx.world.state.set(phase="funding", split=split)
    ctx.world.ledger.append("phase", actor=ctx.actor, what="genesis ready; split chosen",
                            amount_usd=x, reason=reason)
    ctx.world.emit("genesis_ready", actor=ctx.actor, trading_usd=x,
                   wallet_usd=split["wallet_usd"], reason=reason)
    return {"ok": True, "split": split}
