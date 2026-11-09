"""prices.json: every price the AI pays, readable by it and updatable by it.

LLM prices are USD per 1M tokens. xAI prices are refreshed from xAI's free
GET /v1/language-models endpoint (units there: USD cents per 100M tokens, so
dollars per 1M = value / 10_000). The amount actually charged for an xAI call
is the exact cost xAI returns with the answer (usage.cost_in_usd_ticks);
these prices are for estimates and for providers that return no cost.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from .clock import Clock, iso
from .config import Config, Paths
from .store import JsonStore

log = logging.getLogger("council.prices")

TICKS_PER_USD = 10_000_000_000

# From docs.x.ai/docs/models (checked 2026-11-09); replaced by the live list on sync.
_XAI_SEED = {
    "grok-4.7": (2.00, 0.50, 6.00, 4.00, 1.00, 12.00),
    "grok-4.6": (2.00, 0.50, 6.00, 4.00, 1.00, 12.00),
    "grok-4.5": (2.00, 0.30, 6.00, 4.00, 0.60, 12.00),
    "grok-4.3": (1.25, 0.20, 2.50, 2.50, 0.40, 5.00),
    "grok-4.20-0309-reasoning": (1.25, 0.20, 2.50, 2.50, 0.40, 5.00),
    "grok-4.20-0309-non-reasoning": (1.25, 0.20, 2.50, 2.50, 0.40, 5.00),
    "grok-4.20-multi-agent-0309": (1.25, 0.20, 2.50, 2.50, 0.40, 5.00),
    "grok-build-0.1": (1.00, 0.20, 2.00, 2.00, 0.40, 4.00),
}


def _model(i, c, o, il, cl, ol, threshold=200_000) -> dict:
    return {"input": i, "cached_input": c, "output": o,
            "long_context_threshold": threshold,
            "input_long": il, "cached_input_long": cl, "output_long": ol}


def seed(cfg: Config) -> dict:
    return {
        "units": {"llm": "USD per 1M tokens", "service": "USD"},
        "llm": {
            "xai": {
                "base_url": cfg.xai_base_url,
                "key": "xai_api_key",
                "models": {m: _model(*p) for m, p in _XAI_SEED.items()},
            }
        },
        "services": {
            "tavily": {
                "usd_per_credit": 0.008,
                "credits": {"search_basic": 1, "search_advanced": 2,
                            "extract_basic_per_5_urls": 1, "extract_advanced_per_5_urls": 2},
                "free_credits_per_month": 1000,
            },
        },
        "trading": {
            "alpaca": {"crypto_maker_pct": 0.15, "crypto_taker_pct": 0.25,
                       "stock_commission_usd": 0.0,
                       "note": "crypto fees are charged in the asset received"},
        },
    }


class Prices:
    def __init__(self, cfg: Config, paths: Paths, clock: Clock | None = None, get=None):
        self.cfg, self.clock = cfg, clock or Clock()
        self._get = get or (lambda url, headers: httpx.get(url, headers=headers, timeout=20))
        self.store = JsonStore(paths.prices, paths.lock, default=lambda: seed(cfg))

    def all(self) -> dict:
        return self.store.read()

    def model(self, provider: str, model: str) -> dict | None:
        return self.all().get("llm", {}).get(provider, {}).get("models", {}).get(model)

    def provider(self, provider: str) -> dict | None:
        return self.all().get("llm", {}).get(provider)

    # ---------------------------------------------------------------- costs
    @staticmethod
    def token_cost(entry: dict, prompt: int, cached: int, output: int) -> float:
        long = prompt >= int(entry.get("long_context_threshold") or 10**12)
        sfx = "_long" if long and "input_long" in entry else ""
        per = 1_000_000
        return ((prompt - cached) * entry["input" + sfx]
                + cached * entry.get("cached_input" + sfx, entry["input" + sfx])
                + output * entry["output" + sfx]) / per

    def estimate(self, provider: str, model: str, input_tokens: int, output_tokens: int,
                 cached_tokens: int = 0) -> float:
        entry = self.model(provider, model)
        if entry is None:
            raise KeyError(f"no price for {provider}/{model}")
        return round(self.token_cost(entry, input_tokens, cached_tokens, output_tokens), 6)

    # --------------------------------------------------------------- updates
    def set_model(self, provider: str, model: str, fields: dict[str, Any]) -> None:
        def f(d):
            prov = d.setdefault("llm", {}).setdefault(provider, {"models": {}})
            prov.setdefault("models", {}).setdefault(model, {}).update(fields)
        self.store.update(f)

    def set_provider(self, provider: str, base_url: str, key: str) -> None:
        def f(d):
            prov = d.setdefault("llm", {}).setdefault(provider, {"models": {}})
            prov.update(base_url=base_url, key=key)
        self.store.update(f)

    def set_service(self, service: str, fields: dict[str, Any]) -> None:
        self.store.update(lambda d: d.setdefault("services", {}).setdefault(service, {}).update(fields))

    def sync_xai(self, api_key: str) -> list[str]:
        """Refresh xAI models and prices from the free language-models endpoint."""
        r = self._get(f"{self.cfg.xai_base_url}/language-models",
                      {"Authorization": f"Bearer {api_key}"})
        r.raise_for_status()
        models = {}
        for m in r.json().get("models", []):
            def usd(field: str, fallback: str | None = None) -> float | None:
                v = m.get(field, m.get(fallback) if fallback else None)
                return None if v is None else float(v) / 10_000
            entry = {"input": usd("prompt_text_token_price"),
                     "cached_input": usd("cached_prompt_text_token_price"),
                     "output": usd("completion_text_token_price")}
            if entry["input"] is None or entry["output"] is None:
                continue
            if entry["cached_input"] is None:
                entry["cached_input"] = entry["input"]
            if m.get("long_context_threshold"):
                entry["long_context_threshold"] = int(m["long_context_threshold"])
                entry["input_long"] = usd("prompt_text_token_price_long_context") or entry["input"]
                entry["cached_input_long"] = (usd("cached_prompt_text_token_price_long_context")
                                              or entry["cached_input"])
                entry["output_long"] = usd("completion_text_token_price_long_context") or entry["output"]
            if m.get("aliases"):
                entry["aliases"] = m["aliases"]
            if m.get("capabilities"):
                entry["capabilities"] = m["capabilities"]
            models[m["id"]] = entry
        if models:
            def f(d):
                d["llm"]["xai"]["models"] = models
                d["llm"]["xai"]["synced_at"] = iso(self.clock.now())
            self.store.update(f)
        return sorted(models)

