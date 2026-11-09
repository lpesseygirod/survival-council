"""`import council` — your code's access to Alpaca and to waking you.

Every call goes to the gateway, which holds the API keys and logs every order
under this script's name. Standard library only.

    import council
    council.market_data("v1beta3/crypto/us/bars", symbols="BTC/USD", timeframe="1Hour", limit=48)
    council.place_order(symbol="BTC/USD", side="buy", type="limit", notional="3",
                        limit_price="97850", time_in_force="gtc")
    council.wake("BTC fell 3% in 10 minutes")
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any

_URL = os.environ.get("COUNCIL_GATEWAY", "http://127.0.0.1:8765")
_TOKEN = os.environ.get("COUNCIL_TOKEN", "")


class CouncilError(Exception):
    pass


def _call(fn: str, **args: Any) -> Any:
    req = urllib.request.Request(
        f"{_URL}/call", method="POST",
        data=json.dumps({"fn": fn, "args": args}).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {_TOKEN}"})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            data = json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            data = json.loads(e.read())
        except ValueError:
            raise CouncilError(f"gateway {e.code}") from None
    if isinstance(data, dict) and "error" in data:
        raise CouncilError(data["error"])
    return data["result"]


# --- trading -----------------------------------------------------------
def account() -> dict: return _call("account")
def positions() -> list: return _call("positions")
def orders(**params) -> list: return _call("orders", **params)
def get_order(order_id: str) -> dict: return _call("get_order", order_id=order_id)
def place_order(reason: str = "", **order) -> dict:
    """Fields of Alpaca's POST /v2/orders (symbol, side, type, qty or notional, time_in_force,
    limit_price, stop_price, trail_percent, order_class, take_profit, stop_loss, ...)."""
    return _call("place_order", order=order, reason=reason)
def replace_order(order_id: str, reason: str = "", **changes) -> dict:
    return _call("replace_order", order_id=order_id, changes=changes, reason=reason)
def cancel_order(order_id: str, reason: str = "") -> Any:
    return _call("cancel_order", order_id=order_id, reason=reason)
def cancel_all(reason: str = "") -> Any: return _call("cancel_all", reason=reason)
def close_position(symbol: str, qty: float | None = None, percentage: float | None = None,
                   reason: str = "") -> Any:
    return _call("close_position", symbol=symbol, qty=qty, percentage=percentage, reason=reason)
def activities(activity_type: str | None = None, **params) -> Any:
    return _call("activities", activity_type=activity_type, **params)
def portfolio_history(**params) -> Any: return _call("portfolio_history", **params)
def assets(**params) -> list: return _call("assets", **params)
def asset(symbol: str) -> dict: return _call("asset", symbol=symbol)
def clock() -> dict: return _call("clock_info")
def calendar(**params) -> list: return _call("calendar", **params)

# --- market data (any GET on data.alpaca.markets) ----------------------
def market_data(path: str, **params) -> Any:
    return _call("market_data", path=path, params=params)

# --- you ---------------------------------------------------------------
def wake(reason: str) -> Any:
    """Wake the brain. `reason` is shown to it as what woke it."""
    return _call("wake", reason=reason)
def status() -> dict:
    """Net Worth, pots, death floor, distance to death (free)."""
    return _call("status")
