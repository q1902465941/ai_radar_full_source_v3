from __future__ import annotations
import asyncio
from typing import Any

import httpx

from backend.config import settings
from backend.exchange.binance_futures import (
    BinanceAPIError,
    binance_futures,
    binance_http_proxy,
    binance_http_limits,
    binance_http_timeout,
)

class BinanceRestCompat:
    def __init__(self) -> None:
        self.last_public_source = "testnet" if settings.binance_testnet else "mainnet"
        self._testnet_client: httpx.AsyncClient | None = None
        self._testnet_client_loop_id: int | None = None

    async def public_get(self, path: str, params=None):
        try:
            data = await binance_futures.public_get(path, params or {})
            self.last_public_source = "testnet" if settings.binance_testnet else "mainnet"
            return data
        except BinanceAPIError as exc:
            if self._can_fallback(exc):
                data = await self._testnet_public_get(path, params or {})
                self.last_public_source = "testnet_fallback"
                return data
            raise

    async def exchange_info(self):
        return await binance_futures.exchange_info()

    async def premium_index(self):
        return await self.public_get("/fapi/v1/premiumIndex")

    async def ticker_24hr(self, symbol: str | None = None):
        params = {"symbol": symbol} if symbol else {}
        return await self.public_get("/fapi/v1/ticker/24hr", params)

    async def ticker_price(self, symbol: str | None = None):
        params = {"symbol": symbol} if symbol else {}
        return await self.public_get("/fapi/v1/ticker/price", params)

    async def book_ticker(self, symbol: str | None = None):
        params = {"symbol": symbol} if symbol else {}
        return await self.public_get("/fapi/v1/ticker/bookTicker", params)

    async def klines(self, symbol: str, interval: str = "5m", limit: int = 30):
        return await self.public_get("/fapi/v1/klines", {"symbol": symbol, "interval": interval, "limit": limit})

    async def depth(self, symbol: str, limit: int = 50):
        return await self.public_get("/fapi/v1/depth", {"symbol": symbol, "limit": limit})

    async def open_interest(self, symbol: str):
        return await self.public_get("/fapi/v1/openInterest", {"symbol": symbol})

    async def open_interest_hist(self, symbol: str, period: str = "5m", limit: int = 30):
        return await self.public_get("/futures/data/openInterestHist", {"symbol": symbol, "period": period, "limit": limit})

    async def taker_long_short_ratio(self, symbol: str, period: str = "5m", limit: int = 30):
        return await self.public_get("/futures/data/takerlongshortRatio", {"symbol": symbol, "period": period, "limit": limit})

    async def new_order(self, **params):
        return await binance_futures.new_order(**params)

    def _can_fallback(self, exc: BinanceAPIError) -> bool:
        if settings.binance_testnet or not settings.binance_market_fallback_testnet:
            return False
        if exc.status_code == 451:
            return True
        return isinstance(exc.payload, dict) and "restricted location" in str(exc.payload.get("msg", "")).lower()

    async def _testnet_public_get(self, path: str, params: dict[str, Any]) -> Any:
        client = await self._fallback_client()
        response = await client.get("https://testnet.binancefuture.com" + path, params=params)
        if response.status_code >= 400:
            try:
                payload = response.json()
            except Exception:
                payload = response.text
            raise BinanceAPIError(response.status_code, payload)
        if not response.text.strip():
            return None
        try:
            return response.json()
        except ValueError:
            return response.text.strip() or None

    async def _fallback_client(self) -> httpx.AsyncClient:
        loop_id = id(asyncio.get_running_loop())
        if self._testnet_client is not None and self._testnet_client_loop_id == loop_id:
            return self._testnet_client
        if self._testnet_client is not None:
            try:
                await self._testnet_client.aclose()
            except Exception:
                pass
        self._testnet_client = httpx.AsyncClient(
            timeout=binance_http_timeout(),
            limits=binance_http_limits(),
            proxy=binance_http_proxy(),
            trust_env=False,
        )
        self._testnet_client_loop_id = loop_id
        return self._testnet_client

binance_rest = BinanceRestCompat()
