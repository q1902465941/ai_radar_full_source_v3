from __future__ import annotations

import asyncio
import json
import time
from typing import Any
from urllib.parse import urlparse

import websockets

from backend.config import settings


class BinanceTickerStream:
    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._tickers: dict[str, dict[str, Any]] = {}
        self._updated_at = 0.0
        self._last_error = ""

    def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._task = asyncio.create_task(self._run(), name="binance-ticker-stream")

    def snapshot_rows(self) -> list[dict[str, Any]]:
        if not self._tickers:
            return []
        if time.monotonic() - self._updated_at > settings.binance_ws_stale_seconds:
            return []
        return [
            {
                "symbol": row.get("s"),
                "lastPrice": row.get("c"),
                "quoteVolume": row.get("q"),
                "priceChangePercent": row.get("P"),
            }
            for row in self._tickers.values()
        ]

    def diagnostics(self) -> dict[str, Any]:
        age = max(0.0, time.monotonic() - self._updated_at) if self._updated_at else None
        return {
            "running": bool(self._task and not self._task.done()),
            "ticker_count": len(self._tickers),
            "stale": bool(age is None or age > settings.binance_ws_stale_seconds),
            "last_message_age_seconds": round(age, 3) if age is not None else None,
            "last_error": self._last_error,
            "stale_seconds": settings.binance_ws_stale_seconds,
            "custom_url_configured": bool(settings.binance_ws_url),
            "host": _host_of(self._url()),
        }

    def reset(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
        self._task = None
        self._tickers.clear()
        self._updated_at = 0.0
        self._last_error = ""

    async def _run(self) -> None:
        while True:
            try:
                async with websockets.connect(self._url(), ping_interval=20, ping_timeout=20, open_timeout=10) as ws:
                    async for message in ws:
                        payload = json.loads(message)
                        rows = payload if isinstance(payload, list) else [payload]
                        for row in rows:
                            if isinstance(row, dict) and row.get("s"):
                                self._tickers[str(row["s"])] = row
                        self._updated_at = time.monotonic()
                        self._last_error = ""
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._last_error = f"{type(exc).__name__}:{exc}"
                await asyncio.sleep(3)

    def _url(self) -> str:
        if settings.binance_ws_url:
            return settings.binance_ws_url
        host = "stream.binancefuture.com" if settings.binance_testnet else "fstream.binance.com"
        return f"wss://{host}/market/ws/!ticker@arr"


def _host_of(url: str) -> str:
    try:
        return urlparse(url).netloc
    except Exception:
        return ""


binance_ticker_stream = BinanceTickerStream()
