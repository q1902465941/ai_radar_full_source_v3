from __future__ import annotations

import asyncio
from typing import Any

from broker.binance import BinanceBroker
from data.websocket_client import WebSocketClient
from execution.engine import ExecutionEngine
from learning.engine import LearningEngine
from meta.strategy_scorer import StrategyScorer
from portfolio.leverage import LeverageController
from portfolio.manager import PortfolioManager
from portfolio.risk_parity import RiskParityAllocator
from runtime.event_bus import EventBus
from runtime.main import Runtime
from strategy.universe import StrategyUniverse


class HedgeFundRuntimeController:
    def __init__(
        self,
        *,
        event_bus: EventBus | None = None,
        runtime: Runtime | None = None,
        websocket: WebSocketClient | None = None,
        symbols: list[str] | None = None,
    ):
        self.bus = event_bus or EventBus()
        self.runtime = runtime or build_runtime(self.bus)
        self.websocket = websocket or WebSocketClient(self.bus)
        self.symbols = symbols or _runtime_symbols()
        self._tasks: set[asyncio.Task] = set()
        self._started = False

    def start(self) -> None:
        if self._started:
            return
        self.bus.on("market_tick", self._schedule_tick)
        self.websocket.start(self.symbols or None)
        self._started = True

    def stop(self) -> None:
        self.websocket.stop()
        for task in list(self._tasks):
            if not task.done():
                task.cancel()
        self._tasks.clear()
        self.bus.off("market_tick", self._schedule_tick)
        self._started = False

    def _schedule_tick(self, market: dict[str, Any]) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            asyncio.run(self.runtime.on_tick(market))
            return
        task = loop.create_task(self.runtime.on_tick(market), name="hedge-fund-runtime-tick")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)


def build_runtime(event_bus: EventBus | None = None) -> Runtime:
    from backend.config import settings

    bus = event_bus or EventBus()
    universe = StrategyUniverse()
    universe.load_registry()
    broker = BinanceBroker()
    return Runtime(
        event_bus=bus,
        strategy=universe,
        scorer=StrategyScorer(),
        allocator=RiskParityAllocator(),
        leverage=LeverageController(),
        execution=ExecutionEngine(broker),
        broker=broker,
        learning=LearningEngine(),
        portfolio=PortfolioManager(),
        capital=float(settings.hedge_runtime_capital_usdt or settings.paper_account_equity_usdt),
    )


def create_hedge_fund_runtime() -> HedgeFundRuntimeController:
    return HedgeFundRuntimeController()


def _runtime_symbols() -> list[str]:
    from backend.config import settings

    return [
        symbol.strip().upper()
        for symbol in str(settings.hedge_runtime_symbols or "").split(",")
        if symbol.strip()
    ]
