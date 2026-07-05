from __future__ import annotations

from dataclasses import asdict, dataclass, field
import asyncio
import json
import time
from typing import Any

import websockets

from backend.config import settings


DEFAULT_STREAMS = ("aggTrade", "depth20@100ms", "kline_1m", "bookTicker")


@dataclass
class StreamSubscription:
    symbol: str
    subscribed_at: float
    last_sync: float
    streams: list[str] = field(default_factory=list)

    def asdict(self, now: float | None = None) -> dict[str, Any]:
        current = _now(now)
        row = asdict(self)
        row["age_seconds"] = round(max(0.0, current - self.subscribed_at), 3)
        row["idle_seconds"] = round(max(0.0, current - self.last_sync), 3)
        return row


class DynamicSymbolStream:
    def __init__(self, *, streams: tuple[str, ...] = DEFAULT_STREAMS) -> None:
        self.streams = tuple(streams)
        self._subscriptions: dict[str, StreamSubscription] = {}
        self._latest: dict[str, dict[str, Any]] = {}
        self._task: asyncio.Task | None = None
        self._version = 0
        self._last_error = ""
        self._last_message_at = 0.0

    def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._task = asyncio.create_task(self._run(), name="dynamic-symbol-stream")

    def sync(self, symbols: list[str] | tuple[str, ...] | set[str], *, now: float | None = None) -> dict[str, Any]:
        current = _now(now)
        wanted = {str(symbol or "").upper().strip() for symbol in symbols if str(symbol or "").strip()}
        current_symbols = set(self._subscriptions)
        subscribed = sorted(wanted - current_symbols)
        unsubscribed = sorted(current_symbols - wanted)
        for symbol in subscribed:
            self._subscriptions[symbol] = StreamSubscription(
                symbol=symbol,
                subscribed_at=current,
                last_sync=current,
                streams=list(self.streams),
            )
        for symbol in sorted(wanted & current_symbols):
            self._subscriptions[symbol].last_sync = current
        for symbol in unsubscribed:
            self._subscriptions.pop(symbol, None)
            self._latest.pop(symbol, None)
        if subscribed or unsubscribed:
            self._version += 1
        return {
            "subscribed": subscribed,
            "unsubscribed": unsubscribed,
            "active_count": len(self._subscriptions),
            "active_symbols": self.active_symbols(),
            "version": self._version,
        }

    def active_symbols(self) -> list[str]:
        return sorted(self._subscriptions)

    def latest(self, symbol: str) -> dict[str, Any]:
        return dict(self._latest.get(str(symbol or "").upper(), {}))

    def diagnostics(self) -> dict[str, Any]:
        now = _now()
        return {
            "active_count": len(self._subscriptions),
            "active_symbols": self.active_symbols(),
            "streams": list(self.streams),
            "subscriptions": [self._subscriptions[symbol].asdict(now) for symbol in self.active_symbols()],
            "version": self._version,
            "running": bool(self._task and not self._task.done()),
            "last_error": self._last_error,
            "last_message_age_seconds": round(max(0.0, now - self._last_message_at), 3) if self._last_message_at else None,
        }

    def reset(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
        self._task = None
        self._subscriptions.clear()
        self._latest.clear()
        self._version = 0
        self._last_error = ""
        self._last_message_at = 0.0

    async def _run(self) -> None:
        while True:
            try:
                if not self._subscriptions:
                    await asyncio.sleep(1.0)
                    continue
                version = self._version
                url = self._url(self.active_symbols())
                async with websockets.connect(url, ping_interval=20, ping_timeout=20, open_timeout=10) as ws:
                    async for message in ws:
                        if version != self._version:
                            break
                        self._remember_message(message)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._last_error = f"{type(exc).__name__}:{exc}"
                await asyncio.sleep(2.0)

    def _remember_message(self, message: str) -> None:
        payload = json.loads(message)
        data = payload.get("data") if isinstance(payload, dict) else payload
        if not isinstance(data, dict):
            return
        symbol = str(data.get("s") or data.get("symbol") or "").upper()
        if not symbol:
            return
        event_type = str(data.get("e") or "bookTicker")
        bucket = self._latest.setdefault(symbol, {})
        bucket[event_type] = data
        self._last_message_at = _now()
        self._last_error = ""

    def _url(self, symbols: list[str]) -> str:
        if settings.binance_ws_url:
            return settings.binance_ws_url
        host = "stream.binancefuture.com" if settings.binance_testnet else "fstream.binance.com"
        paths = []
        for symbol in symbols:
            lower = symbol.lower()
            for stream in self.streams:
                paths.append(f"{lower}@{stream}")
        return f"wss://{host}/stream?streams={'/'.join(paths)}"


def _now(value: float | None = None) -> float:
    return float(time.monotonic() if value is None else value)


dynamic_symbol_stream = DynamicSymbolStream()
