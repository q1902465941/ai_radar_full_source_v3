import asyncio


def test_websocket_client_emits_market_tick():
    from data.websocket_client import WebSocketClient
    from runtime.event_bus import EventBus

    bus = EventBus()
    events = []
    bus.on("market_tick", events.append)

    client = WebSocketClient(bus)
    client.on_message({"symbol": "BTCUSDT", "price": 100.0})

    assert events == [{"symbol": "BTCUSDT", "price": 100.0}]


def test_strategy_universe_generates_all_non_empty_signals():
    from strategy.universe import StrategyUniverse

    class LongStrategy:
        name = "long_momentum"

        def generate_signal(self, market):
            return {"name": self.name, "symbol": market["symbol"], "side": "LONG", "score": 70, "volatility": 0.2}

    class WaitStrategy:
        name = "wait"

        def generate_signal(self, market):
            return None

    universe = StrategyUniverse()
    universe.add(LongStrategy())
    universe.add(WaitStrategy())

    assert universe.generate_all({"symbol": "ETHUSDT"}) == [
        {"name": "long_momentum", "symbol": "ETHUSDT", "side": "LONG", "score": 70, "volatility": 0.2}
    ]


def test_strategy_scorer_scores_trades_and_ranks_signals_with_weights():
    from meta.strategy_scorer import StrategyScorer

    scorer = StrategyScorer(weights={"mean_reversion": 1.5, "breakout": 1.0})
    assert scorer.score([{"pnl": 10}, {"pnl": -4}, {"pnl": 2}]) == 3.4

    ranked = scorer.rank(
        [
            {"name": "breakout", "score": 75, "trades": [{"pnl": 1}], "volatility": 0.4},
            {"name": "mean_reversion", "score": 60, "trades": [{"pnl": 10}], "volatility": 0.2},
        ]
    )

    assert [row["name"] for row in ranked] == ["mean_reversion", "breakout"]
    assert ranked[0]["strategy_score"] > ranked[1]["strategy_score"]


def test_risk_parity_allocation_and_leverage_controller():
    from portfolio.leverage import LeverageController
    from portfolio.risk_parity import RiskParityAllocator

    allocation = RiskParityAllocator().allocate(
        [
            {"name": "low_vol", "volatility": 0.1},
            {"name": "high_vol", "volatility": 0.3},
        ],
        capital=1200,
    )

    assert round(allocation["low_vol"], 2) == 900.0
    assert round(allocation["high_vol"], 2) == 300.0
    assert LeverageController().get("trend", 0.0) == 3
    assert LeverageController().get("volatile", 0.2) == 1


def test_execution_engine_places_market_order_with_dynamic_leverage():
    from execution.engine import ExecutionEngine

    class Broker:
        def __init__(self):
            self.orders = []
            self.leverage = []

        async def set_leverage(self, symbol, leverage):
            self.leverage.append((symbol, leverage))

        async def place_order(self, **kwargs):
            self.orders.append(kwargs)
            return {"orderId": 1, "executedQty": "6", "avgPrice": "100"}

    broker = Broker()
    engine = ExecutionEngine(broker)

    order = asyncio.run(
        engine.execute(
            {"symbol": "BTCUSDT", "side": "LONG", "price": 100},
            size=200,
            leverage=3,
        )
    )

    assert order["orderId"] == 1
    assert broker.leverage == [("BTCUSDT", 3)]
    assert broker.orders == [
        {"symbol": "BTCUSDT", "side": "BUY", "quantity": 6.0, "type": "MARKET"}
    ]


def test_binance_broker_rejects_non_live_mode_before_signed_calls(monkeypatch):
    from backend.config import settings
    import broker.binance as binance_module
    from broker.binance import BinanceBroker

    called = {"leverage": False}

    async def change_leverage(symbol, leverage):
        called["leverage"] = True

    monkeypatch.setattr(settings, "trade_mode", "paper")
    monkeypatch.setattr(settings, "live_trading_enabled", False)
    monkeypatch.setattr(settings, "live_use_test_order", False)
    monkeypatch.setattr(binance_module.binance_futures, "change_leverage", change_leverage)

    try:
        try:
            asyncio.run(BinanceBroker().set_leverage("BTCUSDT", 2))
        except RuntimeError as exc:
            assert str(exc) == "LIVE_TRADING_DISABLED"
        else:
            raise AssertionError("set_leverage should require live trading")

        assert called["leverage"] is False
    finally:
        monkeypatch.setattr(settings, "trade_mode", "paper")


def test_binance_broker_formats_market_quantity_for_real_order(monkeypatch):
    from backend.config import settings
    import broker.binance as binance_module
    from broker.binance import BinanceBroker

    calls = []

    async def new_order(**kwargs):
        calls.append(kwargs)
        return {"orderId": 42}

    monkeypatch.setattr(settings, "trade_mode", "live")
    monkeypatch.setattr(settings, "live_trading_enabled", True)
    monkeypatch.setattr(settings, "live_use_test_order", False)
    monkeypatch.setattr(binance_module.binance_futures, "format_market_quantity", lambda symbol, qty: "0.123")
    monkeypatch.setattr(binance_module.binance_futures, "new_order", new_order)

    order = asyncio.run(
        BinanceBroker().place_order(
            symbol="BTCUSDT",
            side="BUY",
            quantity=0.123456,
            type="MARKET",
        )
    )

    assert order == {"orderId": 42}
    assert calls == [
        {
            "symbol": "BTCUSDT",
            "side": "BUY",
            "quantity": "0.123",
            "type": "MARKET",
            "newOrderRespType": "RESULT",
        }
    ]


def test_learning_and_portfolio_update_from_trade_event():
    from learning.engine import LearningEngine
    from portfolio.manager import PortfolioManager

    trade = {"strategy": "breakout", "symbol": "BTCUSDT", "qty": 0.5, "pnl": 10}
    learning = LearningEngine()
    portfolio = PortfolioManager()

    learning.update(trade)
    portfolio.update(trade)

    assert learning.weights["breakout"] == 1.01
    assert portfolio.positions["BTCUSDT"] == 0.5


def test_runtime_tick_drives_strategy_execution_learning_and_portfolio():
    from execution.engine import ExecutionEngine
    from learning.engine import LearningEngine
    from meta.strategy_scorer import StrategyScorer
    from portfolio.leverage import LeverageController
    from portfolio.manager import PortfolioManager
    from portfolio.risk_parity import RiskParityAllocator
    from runtime.event_bus import EventBus
    from runtime.main import Runtime
    from strategy.universe import StrategyUniverse

    class Strategy:
        name = "breakout"

        def __init__(self):
            self.weights = {}

        def generate_signal(self, market):
            return {
                "name": self.name,
                "strategy": self.name,
                "symbol": market["symbol"],
                "side": "LONG",
                "price": 100,
                "score": 80,
                "volatility": 0.2,
                "trades": [{"pnl": 4}],
            }

        def update_weights(self, weights):
            self.weights = dict(weights)

    class Broker:
        def __init__(self):
            self.orders = []

        async def set_leverage(self, symbol, leverage):
            self.leverage = (symbol, leverage)

        async def place_order(self, **kwargs):
            self.orders.append(kwargs)
            return {"orderId": 7, "executedQty": str(kwargs["quantity"]), "avgPrice": "100"}

        async def fetch_trade(self, order, signal=None, size=0):
            return {
                "order_id": order["orderId"],
                "strategy": signal["name"],
                "symbol": signal["symbol"],
                "qty": float(order["executedQty"]),
                "pnl": 5.0,
            }

    bus = EventBus()
    trades = []
    bus.on("trade_event", trades.append)
    strategy = Strategy()
    universe = StrategyUniverse()
    universe.add(strategy)
    broker = Broker()

    runtime = Runtime(
        event_bus=bus,
        strategy=universe,
        scorer=StrategyScorer(),
        allocator=RiskParityAllocator(),
        leverage=LeverageController(),
        execution=ExecutionEngine(broker),
        broker=broker,
        learning=LearningEngine(),
        portfolio=PortfolioManager(),
        capital=1000,
    )

    result = asyncio.run(runtime.on_tick({"symbol": "BTCUSDT", "regime": "trend", "drawdown": 0.0}))

    assert result["executed"] == 1
    assert broker.orders[0]["side"] == "BUY"
    assert broker.orders[0]["quantity"] == 30.0
    assert trades[0]["pnl"] == 5.0
    assert runtime.learning.weights["breakout"] == 1.005
    assert strategy.weights == {"breakout": 1.005}
    assert runtime.portfolio.positions["BTCUSDT"] == 30.0


def test_runtime_records_execution_errors_without_trade_update():
    from execution.engine import ExecutionEngine
    from learning.engine import LearningEngine
    from meta.strategy_scorer import StrategyScorer
    from portfolio.leverage import LeverageController
    from portfolio.manager import PortfolioManager
    from portfolio.risk_parity import RiskParityAllocator
    from runtime.event_bus import EventBus
    from runtime.main import Runtime
    from strategy.universe import StrategyUniverse

    class Strategy:
        name = "breakout"

        def generate_signal(self, market):
            return {
                "name": self.name,
                "strategy": self.name,
                "symbol": market["symbol"],
                "side": "LONG",
                "price": 100,
                "score": 80,
                "volatility": 0.2,
            }

    class Broker:
        async def set_leverage(self, symbol, leverage):
            raise RuntimeError("LIVE_TRADING_DISABLED")

        async def place_order(self, **kwargs):
            raise AssertionError("place_order should not run after leverage rejection")

        async def fetch_trade(self, order, signal=None, size=0):
            raise AssertionError("fetch_trade should not run without order")

    universe = StrategyUniverse()
    universe.add(Strategy())
    broker = Broker()
    runtime = Runtime(
        event_bus=EventBus(),
        strategy=universe,
        scorer=StrategyScorer(),
        allocator=RiskParityAllocator(),
        leverage=LeverageController(),
        execution=ExecutionEngine(broker),
        broker=broker,
        learning=LearningEngine(),
        portfolio=PortfolioManager(),
        capital=1000,
    )

    result = asyncio.run(runtime.on_tick({"symbol": "BTCUSDT"}))

    assert result["executed"] == 0
    assert result["errors"] == [
        {"strategy": "breakout", "symbol": "BTCUSDT", "error": "RuntimeError:LIVE_TRADING_DISABLED"}
    ]
    assert runtime.learning.weights == {}
    assert runtime.portfolio.positions == {}
