import pytest

from backend.config import settings
from backend.storage.db import db
from backend.trading.autotrader import autotrader
from backend.trading.exchange_reconciliation import exchange_reconciliation
from backend.trading.live_readiness import live_readiness
from backend.trading.live_executor import live_executor


def test_readiness_engine_scores_and_levels():
    from backend.trading.prg.readiness_engine import ReadinessEngine

    engine = ReadinessEngine()

    assert engine.evaluate({"sharpe": 1.1, "max_drawdown": 0.09, "winrate": 0.56, "profit_factor": 1.21}) == 100
    assert engine.level(40) == "BLOCK_LIVE"
    assert engine.level(41) == "PAPER_PROBE"
    assert engine.level(70) == "MICRO_LIVE_CANDIDATE"
    assert engine.level(85) == "MICRO_LIVE_ALLOWED"


def test_readiness_engine_forces_paper_probe_below_micro_live(monkeypatch):
    from backend.trading.prg.readiness_engine import ReadinessEngine

    monkeypatch.setattr(settings, "live_trading_enabled", True)
    report = ReadinessEngine().enforce(
        {"strategy_pool_score": 75},
        settings_obj=settings,
    )

    assert report["allowed"] is False
    assert report["reason"] == "PRG_MICRO_LIVE_CANDIDATE_NOT_LIVE_ELIGIBLE"
    assert report["mode"] == "MICRO_LIVE_CANDIDATE"
    assert settings.live_trading_enabled is False


def test_quality_gates_reject_low_pf_before_live():
    from backend.trading.prg.quality_gates import QualityGates

    ok, reason = QualityGates().check({"profit_factor": 0.99, "drawdown": 0.05, "winrate": 0.62})

    assert ok is False
    assert reason == "PF_TOO_LOW"


def test_risk_acceptance_rejects_position_desync():
    from backend.trading.prg.risk_acceptance import RiskAcceptance

    ok, reason = RiskAcceptance().verify({"BTCUSDT": 0.25}, {"BTCUSDT": 0.2})

    assert ok is False
    assert reason == "POSITION_DESYNC"


def test_micro_live_controller_caps_capital():
    from backend.trading.prg.micro_live_controller import MicroLiveController

    controller = MicroLiveController()

    with pytest.raises(RuntimeError, match="MICRO_LIVE_CAPITAL_TOO_HIGH"):
        controller.start(100.01)

    state = controller.start(100)
    assert state["capital"] == 100
    assert state["max_loss"] == 5


def test_autotrader_real_live_guard_blocks_when_prg_score_below_micro_live(monkeypatch):
    monkeypatch.setattr(settings, "trade_mode", "live")
    monkeypatch.setattr(settings, "live_trading_enabled", True)
    monkeypatch.setattr(settings, "live_use_test_order", False)
    db.set_kv("live_executor.trading_freeze", {"active": False})
    db.set_kv(
        "production_acceptance.last_report",
        {
            "ok": True,
            "mode": "real_order",
            "finished_ms": __import__("backend.models").models.now_ms(),
            "production_acceptance": {"passed": True},
        },
    )
    monkeypatch.setattr(
        live_readiness,
        "summary",
        lambda: {
            "current_stage": "micro_live",
            "phases": [{"name": "micro_live", "allowed": True, "blockers": []}],
            "metrics": {
                "prg": {
                    "sharpe": 0.0,
                    "max_drawdown": 0.3,
                    "winrate": 0.48,
                    "profit_factor": 0.8,
                }
            },
        },
    )

    ok, reason, evidence = autotrader._real_live_execution_guard()

    assert ok is False
    assert reason == "prg_blocked:PRG_SCORE_BELOW_MICRO_LIVE"
    assert evidence["prg"]["score"] < 70
    assert settings.live_trading_enabled is False


def test_exchange_reconciliation_accepts_replaced_protection_orders(monkeypatch):
    from backend.models import Position
    from backend.account.account_service import account_service
    from backend.exchange.binance_futures import binance_futures

    position = Position(
        position_id="livepos_replaced",
        strategy_id="strategy_replaced",
        source_signal_id="scan_1",
        symbol="BTCUSDT",
        side="LONG",
        status="OPEN",
        stage="Stage 1",
        score=90,
        entry_price=100,
        current_price=100,
        quantity=0.25,
        initial_quantity=0.25,
        margin=25,
        leverage=4,
        stop_loss=95,
        tp1=105,
        tp2=110,
        best_price=100,
        exchange_open_order={"orderId": 10, "clientOrderId": "hy_open_strategy_replaced"},
        exchange_stop_order={"orderId": 11, "clientOrderId": "hy_slr_livepos_replaced"},
        exchange_tp_order={"orderId": 12, "clientOrderId": "hy_tpr_livepos_replaced"},
    )
    monkeypatch.setattr(settings, "trade_mode", "live")
    monkeypatch.setattr(settings, "binance_testnet", False)
    monkeypatch.setattr(binance_futures, "configured", lambda: True)
    monkeypatch.setattr("backend.trading.exchange_reconciliation.position_registry.list_open", lambda: [position])

    async def fake_exchange_positions():
        return [{"symbol": "BTCUSDT", "side": "LONG", "positionAmt": 0.25, "entryPrice": 100}]

    async def fake_open_orders():
        return [
            {"symbol": "BTCUSDT", "orderId": 11, "clientOrderId": "hy_slr_livepos_replaced", "type": "STOP_MARKET"},
            {"symbol": "BTCUSDT", "orderId": 12, "clientOrderId": "hy_tpr_livepos_replaced", "type": "TAKE_PROFIT_MARKET"},
        ]

    monkeypatch.setattr(account_service, "get_exchange_positions", fake_exchange_positions)
    monkeypatch.setattr(account_service, "get_open_orders", fake_open_orders)

    report = __import__("asyncio").run(exchange_reconciliation.refresh(force=True))

    assert report["ok"] is True
    assert report["issues"] == []


def test_live_executor_blocks_real_order_when_prg_below_micro_live(monkeypatch):
    from backend.models import ExecutionPlan
    from backend.exchange.binance_futures import binance_futures

    monkeypatch.setattr(settings, "trade_mode", "live")
    monkeypatch.setattr(settings, "live_trading_enabled", True)
    monkeypatch.setattr(settings, "live_use_test_order", False)
    monkeypatch.setattr(settings, "attach_protection_orders", True)
    monkeypatch.setattr(binance_futures, "configured", lambda: True)
    monkeypatch.setattr(
        live_readiness,
        "summary",
        lambda: {
            "current_stage": "micro_live",
            "phases": [{"name": "micro_live", "allowed": True, "blockers": []}],
            "metrics": {
                "prg": {
                    "sharpe": 0.0,
                    "max_drawdown": 0.3,
                    "winrate": 0.48,
                    "profit_factor": 0.8,
                }
            },
        },
    )

    async def fail_exchange_info():
        raise AssertionError("PRG must block before exchange calls")

    monkeypatch.setattr(binance_futures, "exchange_info", fail_exchange_info)
    plan = ExecutionPlan(
        decision="OPEN",
        mode="live",
        symbol="BTCUSDT",
        side="LONG",
        dynamic_margin=25,
        dynamic_leverage=3,
        quantity=0.01,
        entry_price=100,
        stop_loss=95,
        tp1=105,
        tp2=110,
        tp1_close_ratio=0.5,
        tp2_close_ratio=0.5,
        management_mode="standard",
        cooldown_after_trade=60,
        reason="test",
    )

    with pytest.raises(RuntimeError, match="PRG_BLOCKED:PRG_SCORE_BELOW_MICRO_LIVE"):
        __import__("asyncio").run(live_executor.open_position("scan_1", "strategy_1", 90, plan))

    assert settings.live_trading_enabled is False


def test_live_readiness_micro_live_blocked_when_prg_rejects(monkeypatch):
    from backend.models import now_ms
    import backend.trading.live_readiness as readiness_module

    monkeypatch.setattr(settings, "paper_probe_enabled", True)
    monkeypatch.setattr(settings, "live_trading_enabled", False)
    monkeypatch.setattr(settings, "live_use_test_order", False)
    monkeypatch.setattr(settings, "attach_protection_orders", True)
    monkeypatch.setattr(settings, "auto_trading_use_performance_guard", True)
    monkeypatch.setattr(settings, "max_open_positions", 1)
    monkeypatch.setattr(settings, "trade_mode", "live")
    monkeypatch.setattr(settings, "trade_attribution_min_samples", 1)
    monkeypatch.setattr(
        readiness_module.performance_guard,
        "summary",
        lambda: {"win_rate": 0.60, "recent_win_rate": 0.60, "pnl": 1.0, "loss_streak": 0, "recovery_mode": False},
    )
    monkeypatch.setattr(
        readiness_module.trade_attributor,
        "summary",
        lambda: {"sample_count": 100, "global_win_rate": 0.60, "global_profit_factor": 1.20, "global_pnl": 1.0},
    )
    monkeypatch.setattr(
        readiness_module.learning_data_audit,
        "summary",
        lambda: {"production_grade": True, "trust_level": "production", "reasons": []},
    )
    monkeypatch.setattr(
        readiness_module.position_manager,
        "summary",
        lambda: {"open_count": 0, "floating_pnl": 0.0, "total_pnl": 1.0, "used_margin": 0.0},
    )
    monkeypatch.setattr(readiness_module.position_registry, "list_open", lambda: [])
    monkeypatch.setattr(readiness_module.binance_rest, "last_public_source", "mainnet")
    monkeypatch.setattr(readiness_module.binance_factor_source, "last_refresh_degraded", False)
    monkeypatch.setattr(readiness_module.binance_factor_source, "last_refresh_error", "")
    monkeypatch.setattr(readiness_module.binance_factor_source, "last_refresh_source", "mainnet")
    monkeypatch.setattr(readiness_module.binance_factor_source, "last_snapshot_count", 10)
    monkeypatch.setattr(readiness_module.market_service, "last_snapshots", [{}])
    monkeypatch.setattr(readiness_module.binance_futures, "configured", lambda: True)
    monkeypatch.setattr(
        readiness_module.exchange_reconciliation,
        "cached",
        lambda: {"ok": True, "ts_ms": now_ms(), "age_seconds": 1.0, "issues": []},
    )
    monkeypatch.setattr(readiness_module.strategy_alpha_registry, "strategy_pool_score", lambda: 0.0)
    monkeypatch.setattr(readiness_module.strategy_alpha_registry, "list", lambda limit=100: [])
    monkeypatch.setattr(readiness_module.strategy_alpha_registry, "top", lambda limit=3: [])

    summary = live_readiness.summary()
    micro_live = next(phase for phase in summary["phases"] if phase["name"] == "micro_live")

    assert summary["prg"]["allowed"] is False
    assert micro_live["allowed"] is False
    assert [blocker["code"] for blocker in micro_live["blockers"]] == ["prg_not_micro_live_eligible"]
