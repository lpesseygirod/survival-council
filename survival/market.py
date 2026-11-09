"""Alpaca market data (protected side; keys stay here). Extended in phase 3."""

from __future__ import annotations

from .broker import key_names
from .vault import Vault


class Market:
    def __init__(self, vault: Vault, mode: str):
        from alpaca.data.historical import CryptoHistoricalDataClient, StockHistoricalDataClient

        kid, sec = key_names(mode)
        k, s = vault.get(kid), vault.get(sec)
        self._crypto = CryptoHistoricalDataClient(k, s)
        self._stock = StockHistoricalDataClient(k, s)

    @staticmethod
    def is_crypto(symbol: str) -> bool:
        return "/" in symbol

    def latest_price(self, symbol: str) -> float:
        from alpaca.data.enums import DataFeed
        from alpaca.data.requests import CryptoLatestTradeRequest, StockLatestTradeRequest

        if self.is_crypto(symbol):
            r = self._crypto.get_crypto_latest_trade(CryptoLatestTradeRequest(symbol_or_symbols=symbol))
        else:
            r = self._stock.get_stock_latest_trade(
                StockLatestTradeRequest(symbol_or_symbols=symbol, feed=DataFeed.IEX))
        return float(r[symbol].price)
