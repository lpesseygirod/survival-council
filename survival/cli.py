"""./council — Lea's command line on the server.

  ./council run [--paper|--live]   start everything (paper is the default)
  ./council pause                  freeze the AI and all its scripts (not death)
  ./council resume                 unfreeze
  ./council status                 one factual status line + phase
  ./council start-genesis          same as the setup wizard's "start Genesis" button
  ./council dashboard [--port N]   Lea's dashboard on 127.0.0.1 (behind Caddy for HTTPS)
  ./council dashboard-password     set the dashboard password
  ./council check                  check the server: permissions, sandbox, network limits, services
  ./council use paper|live         switch mode (live starts fresh)
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


def cmd_run(mode: str, explicit: bool = False) -> int:
    cfg = load_config()
    paths = Paths.for_mode(mode)
    if explicit:  # `./council run --live` makes live the current mode
        (paths.root.parent / "current_mode").write_text(mode)
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


def cmd_dashboard(port: int, data_dir: str | None, insecure: bool) -> int:
    from .dashboard.server import Dashboard
    from .vault import default_dir
    cfg = load_config()
    vault = Vault()
    pw = default_dir() / "dashboard.json"
    if not pw.exists():
        print("Set a password first: ./council dashboard-password", file=sys.stderr)
        return 2
    d = Dashboard(cfg, Path(data_dir) if data_dir else PROJECT_ROOT / "data", vault, pw,
                  secure_cookie=not insecure)
    srv = d.serve("127.0.0.1", port)
    print(f"Dashboard on http://127.0.0.1:{port} (Caddy serves it over HTTPS)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


def cmd_dashboard_password() -> int:
    import getpass
    from .dashboard.server import set_password
    from .vault import default_dir
    a = getpass.getpass("New dashboard password (12+ characters): ")
    if a != getpass.getpass("Again: "):
        print("They don't match.", file=sys.stderr)
        return 2
    try:
        set_password(default_dir() / "dashboard.json", a)
    except ValueError as e:
        print(e, file=sys.stderr)
        return 2
    print("Saved.")
    return 0


PROBE = r"""
import json, os, socket
r = {}
def can_read(p):
    try:
        open(p, "rb").read(1); return True
    except Exception:
        return False
def can_connect(host, port):
    try:
        socket.create_connection((host, port), timeout=4).close(); return True
    except Exception:
        return False
import sys
r["uid"] = os.getuid()
r["read_protected"] = [p for p in sys.argv[1:] if can_read(p)]
r["write_work"] = (open("probe.tmp", "w").write("x") == 1)
os.remove("probe.tmp")
r["dashboard"] = can_connect("127.0.0.1", 8080)
r["metadata"] = can_connect("169.254.169.254", 80) or can_connect("168.63.129.16", 80)
r["internet"] = can_connect("1.1.1.1", 443)
print(json.dumps(r))
"""


def cmd_check(mode: str) -> int:
    """Checks the server is set up the way the spec needs. Prints one line per check."""
    import os
    import stat
    from .sandbox import Sandbox
    from .vault import default_dir
    cfg = load_config()
    paths = Paths.for_mode(mode)
    vault = Vault()
    failed = 0

    def line(ok: bool | None, what: str, detail: str = "") -> None:
        nonlocal failed
        mark = "ok  " if ok else ("--  " if ok is None else "FAIL")
        failed += ok is False
        print(f"[{mark}] {what}{(' - ' + detail) if detail else ''}")

    sec = default_dir()
    line(stat.S_IMODE(sec.stat().st_mode) == 0o700, "vault folder is private (700)", str(sec))
    for f in ("vault.key", "vault.enc", "dashboard.json"):
        p = sec / f
        if p.exists():
            line(stat.S_IMODE(p.stat().st_mode) == 0o600, f"{f} is private (600)")
    home = Path.home()
    if PROJECT_ROOT.is_relative_to(home):
        line(stat.S_IMODE(home.stat().st_mode) & 0o077 == 0, "game folder is closed to other users", str(home))
    for name in ("card", f"alpaca_{mode}_key_id", f"alpaca_{mode}_secret", "xai_api_key",
                 "tavily_api_key", "email_address", "email_password"):
        line(vault.has(name) or None, f"vault has {name}", "" if vault.has(name) else "not set yet")
    line((sec / "dashboard.json").exists(), "dashboard password set")

    try:
        sb = Sandbox(cfg, paths, mode)
    except Exception as e:
        line(False, "sandbox", str(e))
        return 1
    if sb.kind != "bwrap":
        line(None, "sandbox isolation", "dev mode (no isolation) - only for a Mac in paper mode")
    else:
        protected = [str(sec / "vault.key"), str(sec / "vault.enc"), str(PROJECT_ROOT / "config.toml"),
                     str(paths.state), str(paths.ledger), str(PROJECT_ROOT / "survival" / "watchdog.py"),
                     "/etc/shadow"]
        res = sb.run(["-c", PROBE, *protected], "check", 60)
        try:
            r = json.loads(res["stdout"].strip().splitlines()[-1])
        except (ValueError, IndexError):
            line(False, "sandbox runs", (res["stderr"] or "no output")[-400:])
            return 1
        line(r["uid"] != os.getuid() and r["uid"] != 0, "AI code runs as its own user", f"uid {r['uid']}")
        line(not r["read_protected"], "AI code can't read the vault, config, state, ledger or game code",
             ", ".join(r["read_protected"]))
        line(r["write_work"], "AI code can write its code folder")
        line(not r["dashboard"], "AI code can't reach the dashboard port")
        line(not r["metadata"], "AI code can't reach the server metadata service")
        line(r["internet"], "AI code can reach the internet")
    if Path("/usr/bin/systemctl").exists() or Path("/bin/systemctl").exists():
        import subprocess
        for unit in ("council-netfilter", "council", "council-dashboard", "caddy"):
            st = subprocess.run(["systemctl", "is-active", unit], capture_output=True, text=True).stdout.strip()
            line(st == "active", f"service {unit}", st)
    print("All good." if not failed else f"{failed} check(s) failed.")
    return 1 if failed else 0


def cmd_use(mode: str) -> int:
    """Switch the run to paper or live (the wizard's Paper/Live choice)."""
    (PROJECT_ROOT / "data").mkdir(exist_ok=True)
    (PROJECT_ROOT / "data" / "current_mode").write_text(mode)
    Paths.for_mode(mode)
    print(f"Now {mode}. The running service restarts itself in {mode} within seconds."
          + (" Live starts fresh: new Genesis, empty memory, new ledger." if mode == "live" else ""))
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="council")
    p.add_argument("command", choices=["run", "pause", "resume", "status", "start-genesis",
                                        "paper-force-networth", "dashboard", "dashboard-password",
                                        "check", "use"])
    p.add_argument("--port", type=int, default=8080)
    p.add_argument("--data-dir", help=argparse.SUPPRESS)
    p.add_argument("--insecure-dev", action="store_true", help=argparse.SUPPRESS)
    p.add_argument("value", nargs="?")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--paper", action="store_true")
    g.add_argument("--live", action="store_true")
    args = p.parse_args(argv)
    mode = resolve_mode(args)
    match args.command:
        case "run": return cmd_run(mode, explicit=args.live or args.paper)
        case "pause": return cmd_pause(mode, True)
        case "resume": return cmd_pause(mode, False)
        case "status": return cmd_status(mode)
        case "start-genesis": return cmd_start_genesis(mode)
        case "dashboard": return cmd_dashboard(args.port, args.data_dir, args.insecure_dev)
        case "check": return cmd_check(mode)
        case "use":
            if args.value not in ("paper", "live"):
                p.error("council use paper|live")
            return cmd_use(args.value)
        case "dashboard-password": return cmd_dashboard_password()
        case "paper-force-networth":
            try:
                value = float(args.value)
            except (TypeError, ValueError):
                p.error("give the Net Worth to force, e.g. 10")
            return cmd_force(mode, value)
    return 1


if __name__ == "__main__":
    sys.exit(main())
