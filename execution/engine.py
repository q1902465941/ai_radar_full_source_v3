from __future__ import annotations

import inspect
from typing import Any


class ExecutionEngine:
    def __init__(self, broker):
        self.broker = broker

    async def execute(self, signal, size, leverage: float | int | None = None):
        symbol = signal["symbol"]
        if leverage is not None and hasattr(self.broker, "set_leverage"):
            await _maybe_await(self.broker.set_leverage(symbol, leverage))

        order = await _maybe_await(
            self.broker.place_order(
                symbol=symbol,
                side=_order_side(signal["side"]),
                quantity=_order_quantity(signal, size, leverage),
                type="MARKET",
            )
        )

        return order


def _order_side(side: str) -> str:
    value = str(side or "").upper()
    if value == "LONG":
        return "BUY"
    if value == "SHORT":
        return "SELL"
    return value


def _order_quantity(signal: dict[str, Any], size: float, leverage: float | int | None) -> float:
    explicit = _float(signal.get("quantity") or signal.get("qty"), 0.0)
    if explicit > 0:
        return explicit
    price = _float(signal.get("price") or signal.get("entry_price"), 0.0)
    lev = max(1.0, _float(leverage, _float(signal.get("leverage"), 1.0)))
    if price > 0:
        return round(float(size) * lev / price, 8)
    return float(size)


async def _maybe_await(value):
    if inspect.isawaitable(value):
        return await value
    return value


def _float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default
