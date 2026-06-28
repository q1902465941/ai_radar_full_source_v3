import asyncio
from types import SimpleNamespace

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from backend.app.db.models import AITaskRecord, Base
from backend.app.db.session import build_engine, session_scope
from backend.models import StrategyPlan


def _plan(symbol="BTCUSDT"):
    return StrategyPlan(
        strategy_id="strategy-1",
        action="WAIT",
        symbol=symbol,
        side="NEUTRAL",
        entry_zone_low=100,
        entry_zone_high=100,
        ideal_entry_price=100,
        stop_loss=0,
        tp1=0,
        tp2=0,
        confidence=0,
        reason="test wait",
        wait_type="WAIT_FOR_CONFIRMATION",
        raw={"provider": "fake", "model": "fake-model"},
    )


def test_ai_service_records_successful_strategy_task(tmp_path):
    from backend.ai_strategy.ai_service import AIService

    class FakeStrategyClient:
        async def generate(self, item, position_context=None):
            return _plan(item.symbol)

        def status(self, **kwargs):
            return {"provider": "fake", "fake": {"model": "fake-model"}}

    db_path = tmp_path / "ai.db"
    engine = build_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)
    service = AIService(strategy_client=FakeStrategyClient(), session_factory=SessionLocal)
    item = SimpleNamespace(symbol="BTCUSDT", direction="LONG", price=100, score=88)

    plan = asyncio.run(service.generate_strategy(item, {"source": "test"}))

    with session_scope(SessionLocal) as session:
        row = session.execute(select(AITaskRecord)).scalar_one()

    assert plan.symbol == "BTCUSDT"
    assert row.state == "succeeded"
    assert row.provider == "fake"
    assert row.model == "fake-model"
    assert row.context_json["symbol"] == "BTCUSDT"
    assert row.output_json["action"] == "WAIT"
    assert row.validation_json["valid"] is True
    assert row.completed_at is not None


def test_ai_service_records_failed_strategy_task(tmp_path):
    from backend.ai_strategy.ai_service import AIService

    class FakeStrategyClient:
        async def generate(self, item, position_context=None):
            raise TimeoutError("provider_timeout")

        def status(self, **kwargs):
            return {"provider": "fake", "fake": {"model": "fake-model"}}

    db_path = tmp_path / "ai.db"
    engine = build_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)
    service = AIService(strategy_client=FakeStrategyClient(), session_factory=SessionLocal)
    item = SimpleNamespace(symbol="ETHUSDT", direction="SHORT", price=200, score=77)

    try:
        asyncio.run(service.generate_strategy(item, {"source": "test"}))
    except TimeoutError:
        pass

    with session_scope(SessionLocal) as session:
        row = session.execute(select(AITaskRecord)).scalar_one()

    assert row.state == "failed"
    assert row.provider == "fake"
    assert "provider_timeout" in row.error
    assert row.completed_at is not None
