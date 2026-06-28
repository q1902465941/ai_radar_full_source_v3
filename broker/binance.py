from __future__ import annotations

from typing import Any

from backend.config import settings
from backend.exchange.binance_futures import binance_futures


class BinanceBroker:
    async def set_leverage(self, symbol: str, leverage: int | float):
        _assert_real_live_enabled()
        return await binance_futures.change_leverage(symbol, int(leverage))

    async def place_order(self, **kwargs):
        _assert_real_live_enabled()
        return await futures_create_order(**kwargs)

    async def fetch_trade(self, order, signal=None, size=0):
        signal = signal or {}
        qty = _float(order.get("executedQty"), _float(order.get("origQty"), 0.0))
        avg_price = _float(order.get("avgPrice"), 0.0)
        cum_quote = _float(order.get("cumQuote"), 0.0)
        if avg_price <= 0 and qty > 0 and cum_quote > 0:
            avg_price = cum_quote / qty
        return {
            "order_id": order.get("orderId") or order.get("clientOrderId"),
            "strategy": signal.get("name") or signal.get("strategy") or signal.get("strategy_id", ""),
            "symbol": signal.get("symbol") or order.get("symbol"),
            "side": signal.get("side") or order.get("side"),
            "qty": qty,
            "price": avg_price,
            "notional": cum_quote or avg_price * qty,
            "allocation": size,
            "pnl": _float(order.get("realizedPnl"), 0.0),
            "raw_order": dict(order),
        }


async def futures_create_order(**kwargs: Any):
    params = dict(kwargs)
    if str(params.get("type") or "").upper() == "MARKET":
        symbol = str(params.get("symbol") or "")
        if symbol and params.get("quantity") is not None:
            params["quantity"] = binance_futures.format_market_quantity(symbol, _float(params.get("quantity")))
        params.setdefault("newOrderRespType", "RESULT")
    return await binance_futures.new_order(**params)


def _assert_real_live_enabled() -> None:
    if settings.trade_mode != "live" or not settings.live_trading_enabled:
        raise RuntimeError("LIVE_TRADING_DISABLED")
    if settings.live_use_test_order:
        raise RuntimeError("LIVE_TEST_ORDER_ENABLED")


def _float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default
