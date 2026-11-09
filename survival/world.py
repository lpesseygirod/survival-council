"""Wiring: one object holding everything a wake-up and its tools can use."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .clock import Clock, iso
from .config import Config, Paths, load_config
from .fx import Fx
from .ledger import Ledger
from .llm import LLM, Transport, _http_post
from .mind import Architecture, Memory
from .prices import Prices
from .store import JsonStore, append_jsonl
from .vault import Vault
from .watchdog import state_store

if False:  # typing only
    from .desk import Desk
    from .net import Net
    from .sandbox import Sandbox, Tokens
    from .redact import Redactor
    from .mail import Mail

MAX_EVENT_TEXT = 4000


@dataclass
class World:
    cfg: Config
    paths: Paths
    mode: str
    clock: Clock
    vault: Vault
    ledger: Ledger
    prices: Prices
    llm: LLM
    state: JsonStore
    arch: Architecture
    memory: Memory
    fx: Fx
    desk: "Desk"
    net: "Net"
    sandbox: "Sandbox"
    tokens: "Tokens"
    redactor: "Redactor"
    mail: "Mail"
    browser_factory: object = None
    _browser: object = None

    def get_browser(self):
        if self._browser is None:
            self._browser = self.browser_factory()
        return self._browser

    def close_browser(self) -> None:
        if self._browser is not None:
            try:
                self._browser.close()
            finally:
                self._browser = None

    @classmethod
    def build(cls, mode: str, *, data_dir: Path | None = None, clock: Clock | None = None,
              vault: Vault | None = None, post: Transport = _http_post, sleep=None,
              cfg: Config | None = None, get=None, alpaca_client=None,
              net_client=None, resolve=None, browser_factory=None, mail=None) -> "World":
        from .browser import Browser
        from .desk import Desk
        from .mail import Mail
        from .net import Net
        from .redact import Redactor
        from .sandbox import Sandbox, Tokens
        cfg = cfg or load_config()
        clock = clock or Clock()
        paths = Paths.for_mode(mode, data_dir)
        vault = vault or Vault()
        ledger = Ledger(paths, clock)
        prices = Prices(cfg, paths, clock, get=get)
        state = state_store(paths, mode)

        def trading() -> float | None:
            return (state.read().get("balances") or {}).get("trading")

        kw = {"sleep": sleep} if sleep else {}
        llm = LLM(cfg, vault, prices, ledger, trading=trading, post=post, **kw)
        desk = Desk(mode, paths, vault, ledger, state, clock, client=alpaca_client)
        net_kw = {"resolve": resolve} if resolve else {}
        net = Net(vault, prices, ledger, state, clock, client=net_client, **net_kw)
        redactor = Redactor(vault)
        profile = paths.root / "browser"
        bf = browser_factory or (lambda: Browser(profile, redactor))
        return cls(cfg, paths, mode, clock, vault, ledger, prices, llm, state,
                   Architecture(cfg, paths, clock), Memory(paths, clock),
                   Fx(paths, cfg.fx_url, clock), desk, net, Sandbox(cfg, paths, mode),
                   Tokens(paths, clock), redactor, mail or Mail(vault, redactor), bf)

    def emit(self, type_: str, **fields) -> None:
        """Live activity feed for the dashboard (events.jsonl). Secrets scrubbed."""
        rec = {"at": iso(self.clock.now()), "type": type_}
        for k, v in fields.items():
            if isinstance(v, str):
                v = self.vault.redact(v)
                if len(v) > MAX_EVENT_TEXT:
                    v = v[:MAX_EVENT_TEXT] + "…"
            rec[k] = v
        append_jsonl(self.paths.events, self.paths.lock, rec)

    def redact_json(self, obj) -> str:
        return self.vault.redact(json.dumps(obj, default=str, ensure_ascii=False))
