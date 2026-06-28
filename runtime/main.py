from __future__ import annotations

import asyncio
from typing import Any


class Runtime:
    def __init__(
        self,
        *,
        event_bus,
        strategy,
        scorer,
        allocator,
        leverage,
        execution,
        broker,
        learning,
        portfolio,
        capital: float,
    ):
        self.bus = event_bus
        self.strategy = strategy
        self.scorer = scorer
        self.allocator = allocator
        self.leverage = leverage
        self.execution = execution
        self.broker = broker
        self.learning = learning
        self.portfolio = portfolio
        self.capital = float(capital)
        self._tick_lock = asyncio.Lock()

    async def on_tick(self, market):
        async with self._tick_lock:
            signals = self.strategy.generate_all(market)

            scored = self.scorer.rank(signals)

            allocation = self.allocator.allocate(scored, self.capital)
            executed = []
            errors = []

            for s in scored:
                size = allocation.get(s["name"], 0.0)
                if size <= 0:
                    continue
                dynamic_leverage = self.leverage.get(_regime(market), _drawdown(market))
                s = {**s, "leverage": dynamic_leverage}

                try:
                    order = await self.execution.execute(s, size, leverage=dynamic_leverage)
                    trade = await self.broker.fetch_trade(order, signal=s, size=size)
                except Exception as exc:
                    errors.append(
                        {
                            "strategy": s.get("name", ""),
                            "symbol": s.get("symbol", ""),
                            "error": f"{type(exc).__name__}:{exc}",
                        }
                    )
                    continue

                self.bus.emit("trade_event", trade)

                self.learning.update(trade)

                self.portfolio.update(trade)
                if hasattr(self.strategy, "update_weights"):
                    self.strategy.update_weights(self.learning.weights)
                executed.append({"signal": s, "order": order, "trade": trade})

            return {
                "signals": len(signals),
                "scored": len(scored),
                "executed": len(executed),
                "errors": errors,
                "orders": executed,
            }


def _regime(market: Any) -> str:
    value = _get(market, "regime", None) or _get(market, "volatility_regime", None) or "normal"
    if value == "high":
        return "volatile"
    if value == "extreme":
        return "volatile"
    return str(value)


def _drawdown(market: Any) -> float:
    try:
        return float(_get(market, "drawdown", 0.0) or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _get(market: Any, key: str, default: Any = None) -> Any:
    if isinstance(market, dict):
        return market.get(key, default)
    return getattr(market, key, default)
