"""EUR -> USD at the ECB reference rate (its card is in euros, Net Worth in dollars)."""

from __future__ import annotations

import json
import logging
from typing import Callable

import httpx

from .clock import Clock
from .config import Paths

log = logging.getLogger(__name__)


class Fx:
    def __init__(self, paths: Paths, url: str, clock: Clock | None = None,
                 fetch: Callable[[str], float] | None = None):
        self.paths, self.url, self.clock = paths, url, clock or Clock()
        self._fetch = fetch or self._fetch_http

    @staticmethod
    def _fetch_http(url: str) -> float:
        r = httpx.get(url, timeout=10)
        r.raise_for_status()
        return float(r.json()["rates"]["USD"])

    def _cache(self) -> dict:
        try:
            return json.loads(self.paths.fx_cache.read_text())
        except (FileNotFoundError, ValueError):
            return {}

    def eur_usd(self) -> float:
        """Today's rate; falls back to the last known rate if the source is down."""
        today = self.clock.now().date().isoformat()
        cache = self._cache()
        if cache.get("date") == today:
            return float(cache["rate"])
        try:
            rate = self._fetch(self.url)
            self.paths.fx_cache.write_text(json.dumps({"date": today, "rate": rate}))
            return rate
        except Exception as e:  # network, parsing
            if "rate" in cache:
                log.warning("FX fetch failed (%s); using rate from %s", e, cache.get("date"))
                return float(cache["rate"])
            raise RuntimeError("no EUR/USD rate available yet") from e

    def to_usd(self, eur: float) -> float:
        return round(eur * self.eur_usd(), 4)
