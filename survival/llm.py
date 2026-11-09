"""Any OpenAI-compatible chat API, billed at its real cost to the AI's credit.

Every call is logged in the ledger (who, model, tokens, cost). Keys come from
the vault and never appear in a prompt, a log or an error message.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable

import httpx

from .config import Config
from .ledger import Ledger
from .prices import TICKS_PER_USD, Prices
from .vault import Vault

log = logging.getLogger("council.llm")

PASSTHROUGH_OPTIONS = {"temperature", "top_p", "max_tokens", "max_completion_tokens",
                       "reasoning_effort", "seed", "stop", "frequency_penalty",
                       "presence_penalty", "response_format", "parallel_tool_calls"}
CREDIT_WORDS = ("credit", "spending limit", "insufficient balance", "insufficient funds",
                "quota", "billing", "payment required")


class NoCredit(Exception):
    """The provider refused for lack of credit. That credit is now $0."""


class LLMError(Exception):
    pass


@dataclass
class Reply:
    message: dict[str, Any]
    cost_usd: float
    prompt_tokens: int = 0
    cached_tokens: int = 0
    output_tokens: int = 0
    provider: str = ""
    model: str = ""
    raw_usage: dict = field(default_factory=dict)

    @property
    def text(self) -> str:
        return self.message.get("content") or ""

    @property
    def tool_calls(self) -> list[dict]:
        return self.message.get("tool_calls") or []


Transport = Callable[[str, dict, dict], httpx.Response]


def _http_post(url: str, headers: dict, body: dict) -> httpx.Response:
    return httpx.post(url, headers=headers, json=body, timeout=httpx.Timeout(600, connect=20))


class LLM:
    def __init__(self, cfg: Config, vault: Vault, prices: Prices, ledger: Ledger,
                 trading: Callable[[], float | None] = lambda: None,
                 post: Transport = _http_post, sleep: Callable[[float], None] = time.sleep):
        self.cfg, self.vault, self.prices, self.ledger = cfg, vault, prices, ledger
        self._trading, self._post, self._sleep = trading, post, sleep

    def chat(self, provider: str, model: str, messages: list[dict], *, actor: str,
             tools: list[dict] | None = None, options: dict | None = None,
             wake_id: str = "") -> Reply:
        prov = self.prices.provider(provider)
        if not prov or not prov.get("base_url") or not prov.get("key"):
            raise LLMError(f"unknown LLM provider {provider!r}")
        price = self.prices.model(provider, model)
        if price is None and provider != "xai":
            raise LLMError(f"no price recorded for {provider}/{model}; set one with set_price first")
        if not self.vault.has(prov["key"]):
            raise LLMError(f"no key named {prov['key']!r} in the vault")

        body: dict[str, Any] = {"model": model, "messages": messages}
        if tools:
            body["tools"] = tools
        for k, v in (options or {}).items():
            if k in PASSTHROUGH_OPTIONS:
                body[k] = v
        headers = {"Authorization": f"Bearer {self.vault.get(prov['key'])}"}
        url = prov["base_url"].rstrip("/") + "/chat/completions"

        resp = self._send(url, headers, body, provider)
        data = resp.json()
        usage = data.get("usage") or {}
        prompt = int(usage.get("prompt_tokens") or 0)
        cached = int((usage.get("prompt_tokens_details") or {}).get("cached_tokens") or 0)
        completion = int(usage.get("completion_tokens") or 0)
        reasoning = int((usage.get("completion_tokens_details") or {}).get("reasoning_tokens") or 0)
        output = completion + reasoning
        ticks = usage.get("cost_in_usd_ticks")
        if ticks:
            cost = int(ticks) / TICKS_PER_USD
        elif price is not None:
            cost = Prices.token_cost(price, prompt, cached, output)
        else:
            cost = 0.0
            log.error("no cost returned and no price for %s/%s", provider, model)

        self.ledger.append(
            "llm_call", actor=actor, service=provider, model=model,
            tokens_in=prompt, tokens_out=output, amount_usd=round(cost, 8),
            detail={"cached": cached, "reasoning": reasoning, "ticks": ticks, "wake": wake_id},
            trading=self._trading(),
        )
        choice = (data.get("choices") or [{}])[0]
        msg = choice.get("message") or {"role": "assistant", "content": ""}
        msg.setdefault("role", "assistant")
        # keep only what the API accepts back in the next request
        msg = {k: v for k, v in msg.items() if k in ("role", "content", "tool_calls", "reasoning_content")}
        return Reply(msg, cost, prompt, cached, output, provider, model, usage)

    def _send(self, url: str, headers: dict, body: dict, provider: str) -> httpx.Response:
        attempt = 0
        while True:
            attempt += 1
            try:
                r = self._post(url, headers, body)
            except httpx.TransportError as e:
                if attempt > self.cfg.http_retries:
                    raise LLMError(f"network error: {type(e).__name__}") from None
                self._sleep(2 ** attempt)
                continue
            if r.status_code < 400:
                return r
            text = self.vault.redact(r.text[:2000])
            low = text.lower()
            if r.status_code == 402 or (r.status_code in (400, 403, 429)
                                         and any(w in low for w in CREDIT_WORDS)):
                self.ledger.append("credit_zeroed", actor="provider", service=provider,
                                   what="provider refused: out of credit", detail={"status": r.status_code},
                                   trading=self._trading())
                raise NoCredit(f"{provider}: {r.status_code} {text[:300]}")
            if (r.status_code == 429 or r.status_code >= 500) and attempt <= self.cfg.http_retries:
                self._sleep(2 ** attempt * 2)
                continue
            raise LLMError(f"{provider} returned {r.status_code}: {text[:500]}")
