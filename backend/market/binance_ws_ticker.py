from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import websockets

from backend.config import settings


class BinanceTickerStream:
    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._tickers: dict[str, dict[str, Any]] = {}
        self._updated_at = 0.0

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
            except asyncio.CancelledError:
                raise
            except Exception:
                await asyncio.sleep(3)

    def _url(self) -> str:
        if settings.binance_ws_url:
            return settings.binance_ws_url
        host = "stream.binancefuture.com" if settings.binance_testnet else "fstream.binance.com"
        return f"wss://{host}/ws/!ticker@arr"


binance_ticker_stream = BinanceTickerStream()
