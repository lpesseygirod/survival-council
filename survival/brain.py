"""One wake-up of the AI's mind.

Only two things are fixed by code:
  1. At the start: the watchdog has confirmed it is alive, and the AI is shown
     the one-line factual status (or, at its very first Genesis wake-up, the
     Rules of the Game, once).
  2. At the end: everything it did is logged.
Everything in between is its own choice.

Run by the supervisor as its own process:  python -m survival.brain --mode paper --reason "..."
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import uuid
from datetime import timedelta

from .clock import iso, parse
from .llm import LLMError, NoCredit, Reply
from .rules import render as render_rules
from .store import append_jsonl
from . import tools_world  # noqa: F401  (registers trading, internet, code tools)
from .tools import TOOLS, Ctx, available, run_tool
from .watchdog import status_line
from .world import World

log = logging.getLogger("council.brain")


class Mind:
    def __init__(self, world: World):
        self.w = world

    # ------------------------------------------------------------ one call
    def _call(self, ctx: Ctx, who: dict, messages: list[dict], tool_names: set[str]) -> Reply | None:
        """One model call with the caller's model, then its fallbacks in order."""
        phase = self.w.state.read().get("phase")
        schemas = [t.schema() for t in available(phase) if t.name in tool_names]
        chain = [{"provider": who["provider"], "model": who["model"]}] + list(who.get("fallbacks") or [])
        for i, m in enumerate(chain):
            try:
                reply = self.w.llm.chat(m.get("provider", "xai"), m["model"], messages,
                                        actor=ctx.actor, tools=schemas or None,
                                        options=who.get("options"), wake_id=ctx.wake_id)
                ctx.costs.append(reply.cost_usd)
                return reply
            except (NoCredit, LLMError) as e:
                self.w.emit("llm_error", actor=ctx.actor, model=m["model"], error=str(e))
                ctx.trace.append({"actor": ctx.actor, "type": "llm_error", "model": m["model"],
                                  "error": self.w.vault.redact(str(e))})
        return None

    # ------------------------------------------------------- a conversation
    def converse(self, ctx: Ctx, who: dict, messages: list[dict], tool_names: set[str],
                 on_first_reply=None) -> str:
        """Run until the model replies without calling a tool. Returns its last text."""
        last_text = ""
        while True:
            reply = self._call(ctx, who, messages, tool_names)
            if reply is None:
                return last_text
            if on_first_reply:
                on_first_reply()
                on_first_reply = None
            messages.append(reply.message)
            if reply.text:
                last_text = reply.text
                self.w.emit("say", actor=ctx.actor, text=reply.text, model=reply.model,
                            cost=round(reply.cost_usd, 6))
                ctx.trace.append({"actor": ctx.actor, "type": "say", "text": reply.text,
                                  "model": reply.model, "cost": reply.cost_usd})
            if not reply.tool_calls:
                return last_text
            for call in reply.tool_calls:
                fn = call.get("function") or {}
                name = fn.get("name", "")
                result = run_tool(ctx, name, fn.get("arguments"), tool_names)
                ctx.trace.append({"actor": ctx.actor, "type": "tool", "tool": name,
                                  "args": self.w.vault.redact(fn.get("arguments") or "")[:2000],
                                  "result": result[:2000]})
                messages.append({"role": "tool", "tool_call_id": call.get("id", ""),
                                 "content": result})

    # ------------------------------------------------------------- consult
    def consult(self, ctx: Ctx, agent: str, message: str, thread: str | None) -> dict:
        arch = self.w.arch.read()
        a = arch["agents"].get(agent)
        if a is None:
            return {"error": f"no agent {agent!r}"}
        if ctx.depth + 1 > self.w.cfg.max_consult_depth:
            return {"error": f"consult depth limit ({self.w.cfg.max_consult_depth}) reached"}
        key = (agent, thread) if thread else None
        messages = ctx.threads.get(key) if key else None
        if messages is None:
            messages = [{"role": "system", "content": a["job"]}] if a.get("job") else []
        messages.append({"role": "user", "content": message})
        if key:
            ctx.threads[key] = messages
        sub = Ctx(self.w, ctx.wake_id, agent, ctx.depth + 1, ctx.consult, ctx.threads,
                  ctx.costs, ctx.trace, ctx.tools_used)
        before = sum(ctx.costs)
        self.w.emit("consult", actor=ctx.actor, agent=agent, message=message, thread=thread or "")
        ctx.trace.append({"actor": ctx.actor, "type": "consult", "agent": agent, "message": message})
        self.w.state.set(speaking=agent)
        text = self.converse(sub, a, messages, set(a.get("tools") or []))
        cost = round(sum(ctx.costs) - before, 6)
        self.w.state.set(speaking=None)
        self.w.emit("reply", actor=agent, to=ctx.actor, text=text, cost=cost)
        ctx.trace.append({"actor": agent, "type": "reply", "text": text, "cost": cost})
        return {"agent": agent, "reply": text, "cost_usd": cost}

    # ---------------------------------------------------------------- wake
    def opening(self, st: dict, woken_by: str) -> list[dict]:
        """The fixed start: RULES once at the first Genesis wake-up, else the status line."""
        arch = self.w.arch.read()
        messages: list[dict] = []
        if arch["self"].get("instructions"):
            messages.append({"role": "system", "content": arch["self"]["instructions"]})
        if st.get("phase") == "genesis" and not st.get("rules_given"):
            messages.append({"role": "user", "content": self.w.paths.rules.read_text()})
            return messages
        lines = [self._status_text(st), f"Woken by: {woken_by}", f"Time: {iso(self.w.clock.now())}"]
        for p in st.get("memory_autoload", []):
            try:
                lines.append(f"\n--- memory/{p} ---\n{self.w.memory.read(p)}")
            except (FileNotFoundError, ValueError):
                lines.append(f"\n--- memory/{p} --- (missing)")
        messages.append({"role": "user", "content": "\n".join(lines)})
        return messages

    def _status_text(self, st: dict) -> str:
        if st.get("phase") == "survival":
            return status_line(st)
        pots = self.w.ledger.pots()
        if st.get("phase") == "funding":
            s = st.get("split", {})
            return (f"Waiting for ${s.get('trading_usd', 0):.2f} to arrive on Alpaca | "
                    f"Wallet ${pots.wallet:.2f} | Credit ${pots.credit_total:.2f}")
        return (f"Genesis | Wallet ${pots.wallet:.2f} | Credit ${pots.credit_total:.2f} | "
                f"Genesis budget left ${pots.genesis_credit:.2f} of "
                f"${self.w.cfg.genesis_budget_usd:.2f}")

    def watchdog_confirmed(self, st: dict) -> bool:
        last = (st.get("watchdog") or {}).get("last_ok")
        if not last:
            return False
        limit = timedelta(seconds=max(120.0, 4 * self.w.cfg.poll_seconds))
        return self.w.clock.now() - parse(last) <= limit

    def wake(self, woken_by: str) -> dict | None:
        st = self.w.state.read()
        if st.get("phase") in ("dead", "setup") or self.w.paths.dead_marker.exists():
            return None
        if not self.watchdog_confirmed(st):
            log.warning("watchdog has not confirmed it is alive; not waking")
            self.w.emit("wake_refused", reason="watchdog not confirmed")
            return None

        wake_id = iso(self.w.clock.now()) + "-" + uuid.uuid4().hex[:6]
        me = self.w.arch.read()["self"]
        actor = me.get("name") or "self"
        ctx = Ctx(self.w, wake_id, actor)
        ctx.consult = self.consult
        messages = self.opening(st, woken_by)
        first_genesis = st.get("phase") == "genesis" and not st.get("rules_given")

        started = iso(self.w.clock.now())
        self.w.state.set(awake=True, wake={"id": wake_id, "started": started, "woken_by": woken_by},
                         wake_conditions=None, speaking=None)
        opening_text = messages[-1]["content"]
        self.w.emit("wake_start", wake_id=wake_id, actor=actor, woken_by=woken_by,
                    status=opening_text.splitlines()[0] if not first_genesis else "Rules of the Game")

        # it has every tool; each call offers those valid in the current phase
        tool_names = set(TOOLS)

        # RULES count as given once the model has actually answered them
        mark = (lambda: self.w.state.set(rules_given=True)) if first_genesis else None
        final = ""
        try:
            final = self.converse(ctx, me, messages, tool_names, on_first_reply=mark)
        finally:
            ended = iso(self.w.clock.now())
            record = {
                "wake_id": wake_id, "started": started, "ended": ended, "woken_by": woken_by,
                "opening": "Rules of the Game (given once)" if first_genesis else opening_text,
                "self": {"name": me.get("name"), "model": me["model"]},
                "steps": ctx.trace, "final_text": final,
                "cost_usd": round(sum(ctx.costs), 6), "tools_used": sorted(ctx.tools_used),
                "next_wake": self.w.state.read().get("wake_conditions"),
            }
            append_jsonl(self.w.paths.decisions, self.w.paths.lock,
                         json.loads(self.w.redact_json(record)))
            self.w.state.set(awake=False, wake=None, speaking=None, last_wake={
                "id": wake_id, "started": started, "ended": ended, "woken_by": woken_by,
                "cost_usd": record["cost_usd"]})
            self.w.emit("wake_end", wake_id=wake_id, actor=actor, cost=record["cost_usd"])
        return record


# ============================================================ starting Genesis
def start_genesis(world: World) -> None:
    """Lea's 'Wake it up: start Genesis' button (the setup wizard calls this)."""
    st = world.state.read()
    if st.get("phase") != "setup":
        raise RuntimeError(f"Genesis can only start from setup (phase is {st.get('phase')})")
    if world.vault.has("xai_api_key"):
        try:
            world.prices.sync_xai(world.vault.get("xai_api_key"))
        except Exception as e:
            log.warning("could not refresh xAI prices, using recorded ones: %s", world.vault.redact(str(e)))
    world.arch.store.update(lambda d: None)  # write architecture.json with the first model
    world.ledger.append("wallet_start", actor="lea", amount_usd=world.cfg.start_usd,
                        what="its card (Wallet)")
    world.ledger.append("genesis_credit", actor="lea", service="xai",
                        amount_usd=world.cfg.genesis_budget_usd, what="Genesis budget")
    world.paths.rules.write_text(render_rules(world.cfg, world.prices.all(), st, world.mode))
    world.state.set(phase="genesis", genesis_started_at=iso(world.clock.now()))
    world.emit("genesis_start")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="survival.brain")
    p.add_argument("--mode", choices=["paper", "live"], default="paper")
    p.add_argument("--reason", default="")
    a = p.parse_args(argv)
    world = World.build(a.mode)
    logging.basicConfig(level=logging.INFO, filename=world.paths.logs / "brain.log",
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    Mind(world).wake(a.reason)
    return 0


if __name__ == "__main__":
    sys.exit(main())
