"""./council — Lea's command line on the server.

  ./council run [--paper|--live]   start everything (paper is the default)
  ./council pause                  freeze the AI and all its scripts (not death)
  ./council resume                 unfreeze
  ./council status                 one factual status line + phase
  ./council start-genesis          same as the setup wizard's "start Genesis" button
  ./council paper-force-networth V paper mode only: test death by forcing Net Worth to V
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from .clock import Clock, iso
from .config import PROJECT_ROOT, Paths, load_config
from .fx import Fx
from .ledger import Ledger
from .supervisor import EXIT_DEAD, AIProcesses, Supervisor, is_finally_dead
from .vault import Vault
from .waker import Waker
from .watchdog import Watchdog, state_store, status_line


class RedactFilter(logging.Filter):
    def __init__(self, vault: Vault):
        super().__init__()
        self.vault = vault

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = self.vault.redact(record.getMessage())
        record.args = ()
        return True


def setup_logging(paths: Paths, vault: Vault) -> None:
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    handlers = [logging.FileHandler(paths.logs / "council.log"), logging.StreamHandler()]
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    for h in handlers:
        h.setFormatter(fmt)
        h.addFilter(RedactFilter(vault))
        root.addHandler(h)


def resolve_mode(args: argparse.Namespace) -> str:
    if args.live:
        return "live"
    if args.paper:
        return "paper"
    cur = PROJECT_ROOT / "data" / "current_mode"
    return cur.read_text().strip() if cur.exists() else "paper"


def cmd_run(mode: str) -> int:
    cfg = load_config()
    paths = Paths.for_mode(mode)
    if is_finally_dead(paths):
        print(f"The {mode} run is dead. It will never start again.", file=sys.stderr)
        return EXIT_DEAD
    vault = Vault()
    setup_logging(paths, vault)
    clock = Clock()
    ledger = Ledger(paths, clock)
    fx = Fx(paths, cfg.fx_url, clock)

    from .desk import Desk
    from .gateway import Gateway
    from .sandbox import Sandbox, Tokens
    from .scripts import ScriptManager

    sandbox = Sandbox(cfg, paths, mode)
    tokens = Tokens(paths, clock)
    state = state_store(paths, mode)
    desk_holder: list[Desk] = []

    def desk() -> Desk:
        if not desk_holder:
            desk_holder.append(Desk(mode, paths, vault, ledger, state, clock))
        return desk_holder[0]

    def fill_sync() -> int:
        return desk().sync_fills()

    def make_watchdog(ai: AIProcesses) -> Watchdog:
        from .broker import AlpacaBroker
        return Watchdog(cfg, paths, mode, ledger, lambda: AlpacaBroker(vault, mode), fx,
                        clock, stop_ai=ai.stop_all, fill_sync=fill_sync)

    def make_waker(ai: AIProcesses) -> Waker:
        def spawn(reason: str) -> None:
            ai.spawn("brain", [sys.executable, "-m", "survival.brain", "--mode", mode,
                               "--reason", reason], cwd=PROJECT_ROOT)

        market = None

        def price(symbol: str) -> float:
            nonlocal market
            if market is None:
                from .market import Market
                market = Market(vault, mode)
            return market.latest_price(symbol)

        return Waker(state_store(paths, mode), paths, clock, spawn,
                     lambda: "brain" in ai.names(), price)

    ai = AIProcesses(signal_all=sandbox.signal_all)
    gateway = Gateway(desk, paths, state, tokens, cfg.gateway_port, clock)
    return Supervisor(cfg, paths, mode, make_watchdog, clock, make_waker, ai=ai, gateway=gateway,
                      make_scripts=lambda a: ScriptManager(paths, state, a, sandbox, tokens, clock)).run()


def cmd_start_genesis(mode: str) -> int:
    """Same as the setup wizard's 'Wake it up: start Genesis' button."""
    from .brain import start_genesis
    from .world import World
    world = World.build(mode)
    missing = [k for k in ("xai_api_key", f"alpaca_{mode}_key_id", f"alpaca_{mode}_secret")
               if not world.vault.has(k)]
    if missing:
        print(f"Missing in the vault: {', '.join(missing)}. Finish the setup wizard first.",
              file=sys.stderr)
        return 2
    start_genesis(world)
    print("Genesis started. The running supervisor wakes it within seconds.")
    return 0


def cmd_pause(mode: str, pause: bool) -> int:
    paths = Paths.for_mode(mode)
    st = state_store(paths, mode)
    if pause:
        paths.paused_flag.write_text(iso(Clock().now()))
        st.set(paused=True, paused_since=iso(Clock().now()))
        print("Paused. The AI and its scripts are frozen. The stagnation clock keeps running.")
    else:
        paths.paused_flag.unlink(missing_ok=True)
        st.set(paused=False, paused_since=None)
        print("Resumed.")
    return 0


def cmd_status(mode: str) -> int:
    paths = Paths.for_mode(mode)
    st = state_store(paths, mode).read()
    print(f"[{mode}] phase: {st.get('phase')}  alive: {st.get('alive')}  "
          f"paused: {st.get('paused')}  awake: {st.get('awake')}")
    if st.get("phase") == "survival" and "balances" in st:
        print(status_line(st))
    elif "balances" in st:
        print(json.dumps(st["balances"], indent=2))
    if st.get("alerts"):
        print("Needs you:", json.dumps(st["alerts"]))
    return 0


def cmd_force(mode: str, value: float) -> int:
    if mode != "paper":
        print("Refused: only possible in paper mode.", file=sys.stderr)
        return 2
    paths = Paths.for_mode(mode)
    st = state_store(paths, mode).read()
    nw = st.get("balances", {}).get("net_worth")
    if nw is None:
        print("No Net Worth reading yet; start the run first.", file=sys.stderr)
        return 2
    Ledger(paths).append("paper_adjust", actor="lea", amount_usd=round(value - nw, 4),
                         what=f"paper test: force Net Worth to {value:.2f}")
    print(f"Net Worth will read ${value:.2f} at the watchdog's next check.")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="council")
    p.add_argument("command", choices=["run", "pause", "resume", "status", "start-genesis",
                                        "paper-force-networth"])
    p.add_argument("value", nargs="?", type=float)
    g = p.add_mutually_exclusive_group()
    g.add_argument("--paper", action="store_true")
    g.add_argument("--live", action="store_true")
    args = p.parse_args(argv)
    mode = resolve_mode(args) if args.command != "run" else ("live" if args.live else "paper")
    match args.command:
        case "run": return cmd_run(mode)
        case "pause": return cmd_pause(mode, True)
        case "resume": return cmd_pause(mode, False)
        case "status": return cmd_status(mode)
        case "start-genesis": return cmd_start_genesis(mode)
        case "paper-force-networth":
            if args.value is None:
                p.error("give the Net Worth to force, e.g. 10")
            return cmd_force(mode, args.value)
    return 1


if __name__ == "__main__":
    sys.exit(main())
