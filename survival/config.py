"""Protected configuration and on-disk layout."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
MODES = ("paper", "live")


@dataclass(frozen=True)
class DeathRules:
    base_floor_usd: float
    grace_days: int
    daily_rise_usd: float


@dataclass(frozen=True)
class Config:
    start_usd: float
    genesis_budget_usd: float
    death: DeathRules
    poll_seconds: float
    alpaca_retry_seconds: float
    rent_warn_days: int
    fx_url: str
    first_provider: str
    first_model: str
    xai_base_url: str
    max_consult_depth: int
    http_retries: int
    sandbox_kind: str
    sandbox_user: str
    sandbox_python: str
    gateway_port: int
    run_timeout_max_s: int
    output_max_chars: int


def load_config(path: Path | None = None) -> Config:
    path = path or PROJECT_ROOT / "config.toml"
    raw = tomllib.loads(path.read_text())
    return Config(
        start_usd=float(raw["money"]["start_usd"]),
        genesis_budget_usd=float(raw["money"]["genesis_budget_usd"]),
        death=DeathRules(
            base_floor_usd=float(raw["death"]["base_floor_usd"]),
            grace_days=int(raw["death"]["grace_days"]),
            daily_rise_usd=float(raw["death"]["daily_rise_usd"]),
        ),
        poll_seconds=float(raw["watchdog"]["poll_seconds"]),
        alpaca_retry_seconds=float(raw["watchdog"]["alpaca_retry_seconds"]),
        rent_warn_days=int(raw["watchdog"]["rent_warn_days"]),
        fx_url=str(raw["fx"]["url"]),
        first_provider=str(raw["brain"]["first_provider"]),
        first_model=str(raw["brain"]["first_model"]),
        xai_base_url=str(raw["brain"]["xai_base_url"]),
        max_consult_depth=int(raw["brain"]["max_consult_depth"]),
        http_retries=int(raw["brain"]["http_retries"]),
        sandbox_kind=str(raw["sandbox"]["kind"]),
        sandbox_user=str(raw["sandbox"]["user"]),
        sandbox_python=str(raw["sandbox"]["python"]),
        gateway_port=int(raw["sandbox"]["gateway_port"]),
        run_timeout_max_s=int(raw["sandbox"]["run_timeout_max_s"]),
        output_max_chars=int(raw["sandbox"]["output_max_chars"]),
    )


@dataclass(frozen=True)
class Paths:
    """Everything one run (paper or live) writes. Each mode starts fresh."""

    root: Path
    code_root: Path | None = None  # server: outside the protected home, shared with the sandbox user

    @classmethod
    def for_mode(cls, mode: str, data_dir: Path | None = None) -> "Paths":
        if mode not in MODES:
            raise ValueError(f"mode must be one of {MODES}")
        code_env = os.environ.get("COUNCIL_CODE_ROOT")
        p = cls((data_dir or PROJECT_ROOT / "data") / mode,
                Path(code_env) / mode if code_env and data_dir is None else None)
        p.ensure()
        return p

    def ensure(self) -> None:
        for d in (self.root, self.memory, self.code, self.logs):
            d.mkdir(parents=True, exist_ok=True)

    # Logs the owner and the dashboard read (spec section 8)
    @property
    def ledger(self) -> Path: return self.root / "ledger.csv"
    @property
    def decisions(self) -> Path: return self.root / "decisions.jsonl"
    @property
    def architecture(self) -> Path: return self.root / "architecture.json"
    @property
    def state(self) -> Path: return self.root / "state.json"
    @property
    def memory(self) -> Path: return self.root / "memory"
    @property
    def code(self) -> Path: return self.code_root or self.root / "code"

    # Protected internals
    @property
    def logs(self) -> Path: return self.root / "logs"
    @property
    def dead_marker(self) -> Path: return self.root / "DEAD"
    @property
    def paused_flag(self) -> Path: return self.root / "PAUSED"
    @property
    def check_request(self) -> Path: return self.root / ".check-now"
    @property
    def fx_cache(self) -> Path: return self.root / "fx.json"
    @property
    def prices(self) -> Path: return self.root / "prices.json"
    @property
    def events(self) -> Path: return self.root / "events.jsonl"
    @property
    def memory_log(self) -> Path: return self.root / "memory_log.jsonl"
    @property
    def rules(self) -> Path: return self.root / "RULES.md"
    @property
    def tokens(self) -> Path: return self.root / ".gateway_tokens.json"
    @property
    def history(self) -> Path: return self.root / "history.jsonl"
    @property
    def setup(self) -> Path: return self.root / "setup.json"
    @property
    def wake_requests(self) -> Path: return self.root / "wake_requests.jsonl"
    @property
    def script_logs(self) -> Path: return self.code / ".logs"
    @property
    def lock(self) -> Path: return self.root / ".lock"
