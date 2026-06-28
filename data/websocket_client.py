from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from typing import Any

import websockets


MarketMessageHandler = Callable[[dict[str, Any]], Any]


class WebSocketClient:
    def __init__(self, event_bus):
        self.bus = event_bus
        self._task: asyncio.Task | None = None

    def on_message(self, msg):
        payload = _normalize_message(msg)
        if payload is not None:
            self.bus.emit("market_tick", payload)

    def start(self, symbols: list[str] | tuple[str, ...] | None = None) -> asyncio.Task | None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            asyncio.run(connect_to_binance_ws(on_message=self.on_message, symbols=symbols))
            return None
        if self._task and not self._task.done():
            return self._task
        self._task = loop.create_task(
            connect_to_binance_ws(on_message=self.on_message, symbols=symbols),
            name="hedge-fund-market-ws",
        )
        return self._task

    def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
        self._task = None


async def connect_to_binance_ws(
    *,
    on_message: MarketMessageHandler,
    symbols: list[str] | tuple[str, ...] | None = None,
    url: str | None = None,
) -> None:
    target = url or _binance_url(symbols)
    while True:
        try:
            async with websockets.connect(target, ping_interval=20, ping_timeout=20, open_timeout=10) as ws:
                async for raw in ws:
                    payload = json.loads(raw)
                    rows = _payload_rows(payload)
                    for row in rows:
                        normalized = _normalize_message(row)
                        if normalized is not None:
                            on_message(normalized)
        except asyncio.CancelledError:
            raise
        except Exception:
            await asyncio.sleep(2.0)


def _binance_url(symbols: list[str] | tuple[str, ...] | None = None) -> str:
    if not symbols:
        return "wss://fstream.binance.com/ws/!ticker@arr"
    streams = "/".join(f"{str(symbol).lower()}@aggTrade" for symbol in symbols if str(symbol).strip())
    return f"wss://fstream.binance.com/stream?streams={streams}"


def _payload_rows(payload: Any) -> list[Any]:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict) and isinstance(payload.get("data"), list):
        return payload["data"]
    if isinstance(payload, dict) and isinstance(payload.get("data"), dict):
        return [payload["data"]]
    return [payload]


def _normalize_message(msg: Any) -> dict[str, Any] | None:
    if isinstance(msg, str):
        try:
            msg = json.loads(msg)
        except json.JSONDecodeError:
            return None
    if not isinstance(msg, dict):
        return None
    data = msg.get("data") if isinstance(msg.get("data"), dict) else msg
    if not isinstance(data, dict):
        return None
    symbol = str(data.get("symbol") or data.get("s") or "").upper()
    if not symbol and "price" not in data:
        return dict(data)
    price = data.get("price", data.get("p", data.get("c")))
    out = dict(data)
    if symbol:
        out["symbol"] = symbol
    if price is not None:
        try:
            out["price"] = float(price)
        except (TypeError, ValueError):
            pass
    return out
