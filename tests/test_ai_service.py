import asyncio
from types import SimpleNamespace

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from backend.app.db.models import AITaskRecord, Base
from backend.app.db.session import build_engine, session_scope
from backend.ai_strategy.strategy_contract import build_rule_contract
from backend.models import RadarItem, StrategyPlan


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


def _open_plan(item):
    plan = StrategyPlan(
        strategy_id="strategy-open",
        action="OPEN_LONG",
        symbol=item.symbol,
        side="LONG",
        entry_zone_low=99,
        entry_zone_high=101,
        ideal_entry_price=100,
        stop_loss=98,
        tp1=103,
        tp2=106,
        confidence=80,
        reason="test open",
        wait_type="",
        raw={"provider": "codex_cli", "model": "fake-model"},
    )
    plan.raw["strategy_contract"] = build_rule_contract(item, plan)
    plan.raw["strategy_contract_quality"] = {"ok": True, "reasons": []}
    return plan


def _radar_item(symbol="BTCUSDT"):
    return RadarItem(
        rank=1,
        symbol=symbol,
        base_asset=symbol.replace("USDT", ""),
        price=100,
        direction="LONG",
        stage="confirming",
        trigger_mode="score_acceleration",
        score=88,
        score_history=[60, 75, 88],
        rank_history=[5, 2, 1],
        heat_slope=10,
        slope_score=90,
        fake_breakout_risk="LOW",
        change_5m=1.2,
        change_15m=2.1,
        change_1h=2.8,
        oi_change=1.1,
        fund_confirm_count=3,
        fund_confirm_total=3,
        dealer_radar="long_extend",
        sm_position=62,
        sm_delta=0.8,
        volume_spike=2.4,
        funding_rate=0.0002,
        taker_buy_ratio=0.68,
        taker_sell_ratio=0.32,
        depth_imbalance=0.22,
        atr_pct=1.2,
        wick_ratio=0.25,
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
    assert row.validation_json["opens"] is False
    assert row.validation_json["tradable_strategy"] is False
    assert row.completed_at is not None


def test_ai_service_audits_open_strategy_validation_and_candidate_source(tmp_path):
    from backend.ai_strategy.ai_service import AIService

    status_calls = []

    class FakeStrategyClient:
        async def generate(self, item, position_context=None):
            return _open_plan(item)

        def status(self, **kwargs):
            status_calls.append(kwargs)
            return {"provider": "codex_cli", "codex_cli": {"model": "fake-model"}}

    db_path = tmp_path / "ai.db"
    engine = build_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)
    service = AIService(strategy_client=FakeStrategyClient(), session_factory=SessionLocal)
    item = _radar_item()

    plan = asyncio.run(
        service.generate_strategy(
            item,
            {"candidate_selection": {"source": "paper_top", "paper_validation": True}},
        )
    )

    with session_scope(SessionLocal) as session:
        row = session.execute(select(AITaskRecord)).scalar_one()

    assert plan.action == "OPEN_LONG"
    assert status_calls[0]["candidate_source"] == "paper_top"
    assert row.context_json["candidate_source"] == "paper_top"
    assert row.validation_json["valid"] is True
    assert row.validation_json["validator_reason"] == "ok"
    assert row.validation_json["opens"] is True
    assert row.validation_json["provider"] == "codex_cli"
    assert row.validation_json["tradable_strategy"] is True
    assert row.validation_json["contract_quality_ok"] is True


def test_ai_service_marks_invalid_open_strategy_not_tradable(tmp_path):
    from backend.ai_strategy.ai_service import AIService

    class FakeStrategyClient:
        async def generate(self, item, position_context=None):
            return StrategyPlan(
                strategy_id="strategy-invalid",
                action="OPEN_LONG",
                symbol=item.symbol,
                side="LONG",
                entry_zone_low=99,
                entry_zone_high=101,
                ideal_entry_price=100,
                stop_loss=99.95,
                tp1=100.1,
                tp2=100.2,
                confidence=80,
                reason="invalid open",
                raw={"provider": "codex_cli", "model": "fake-model"},
            )

        def status(self, **kwargs):
            return {"provider": "codex_cli", "codex_cli": {"model": "fake-model"}}

    db_path = tmp_path / "ai.db"
    engine = build_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)
    service = AIService(strategy_client=FakeStrategyClient(), session_factory=SessionLocal)

    asyncio.run(service.generate_strategy(_radar_item(), {"candidate_selection": {"source": "strict"}}))

    with session_scope(SessionLocal) as session:
        row = session.execute(select(AITaskRecord)).scalar_one()

    assert row.state == "succeeded"
    assert row.validation_json["valid"] is False
    assert row.validation_json["opens"] is False
    assert row.validation_json["tradable_strategy"] is False
    assert row.validation_json["validator_reason"] in {"sl_too_close", "tp1_too_close"}


def test_ai_service_audit_summary_counts_tradable_strategy_tasks(tmp_path):
    from backend.ai_strategy.ai_service import AIService

    class FakeStrategyClient:
        def __init__(self):
            self.plans = []

        async def generate(self, item, position_context=None):
            return self.plans.pop(0)

        def status(self, **kwargs):
            return {"provider": "codex_cli", "codex_cli": {"model": "fake-model"}}

    db_path = tmp_path / "ai.db"
    engine = build_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)
    client = FakeStrategyClient()
    item = _radar_item()
    invalid = StrategyPlan(
        strategy_id="strategy-invalid",
        action="OPEN_LONG",
        symbol=item.symbol,
        side="LONG",
        entry_zone_low=99,
        entry_zone_high=101,
        ideal_entry_price=100,
        stop_loss=99.95,
        tp1=100.1,
        tp2=100.2,
        confidence=80,
        reason="invalid open",
        raw={"provider": "codex_cli", "model": "fake-model"},
    )
    client.plans = [_plan(item.symbol), _open_plan(item), invalid]
    service = AIService(strategy_client=client, session_factory=SessionLocal)

    for _ in range(3):
        asyncio.run(service.generate_strategy(item, {"candidate_selection": {"source": "strict"}}))

    audit = service.status(candidate_count=1, candidate_source="strict")["audit"]

    assert audit["succeeded"] == 3
    assert audit["tradable_strategy_count"] == 1
    assert audit["non_tradable_strategy_count"] == 2
    assert audit["invalid_strategy_count"] == 1
    assert audit["open_strategy_count"] == 1
    assert audit["last_tradable_strategy"]["action"] == "OPEN_LONG"
    assert audit["last_tradable_strategy"]["provider"] == "codex_cli"
    assert audit["recent_strategy_tasks"][0]["valid"] is False
    assert audit["recent_strategy_tasks"][0]["validator_reason"] in {"sl_too_close", "tp1_too_close"}


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
