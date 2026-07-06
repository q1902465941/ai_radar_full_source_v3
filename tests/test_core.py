import asyncio
from backend.models import ClosedPosition, ExecutionPlan, MarketSnapshot, Position, PositionPolicyReview, RadarItem, StrategyPlan, now_ms
from backend.radar.score_engine import SCORE_WEIGHTS, direction, score_engine
from backend.radar.fund_confirm import fund_confirm, fund_confirm_components
from backend.radar.fake_breakout import fake_breakout
from backend.ai_strategy.codex_cli_strategy_client import CODEX_PROMPT, CodexCLIStrategyClient, normalized_codex_reasoning_effort
from backend.ai_strategy.context_compressor import context_compressor
from backend.ai_strategy.dynamic_trade_model import auto_trading_risk_model
from backend.ai_strategy.openai_strategy_client import openai_strategy_client
from backend.ai_strategy.rule_strategy import rule_strategy_generator
from backend.ai_strategy.strategy_qa import SYSTEM_PROMPT as STRATEGY_QA_SYSTEM_PROMPT
from backend.ai_strategy.strategy_contract import build_rule_contract, contract_quality
from backend.ai_strategy.strategy_validator import strategy_validator
from backend.ai_strategy.strategy_quality_gate import strategy_quality_gate
from backend.config import settings
from backend.config_env import update_env_values
from backend.learning.ai_strategy_feedback import ai_strategy_feedback
from backend.learning.backtest_engine import backtest_engine
from backend.learning.strategy_evolver import strategy_evolver
from backend.learning.strategy_filter import strategy_matches
from backend.learning.strategy_registry import strategy_registry
from backend.learning.trade_memory import trade_memory
from backend.learning.radar_score_auditor import radar_score_auditor
from backend.learning.radar_weight_calibrator import radar_weight_calibrator
from backend.learning.replay_memory import replay_memory
from backend.learning.event_calibrator import event_calibrator
from backend.learning.learning_data_audit import learning_data_audit
from backend.learning.learned_risk_guard import learned_risk_guard
from backend.learning.trade_attributor import trade_attributor
from backend.market.binance_factor_source import BinanceFactorSource, binance_factor_source, _kline_features
from backend.market.market_service import MarketService, PriceQuote, market_service
from backend.positions.position_manager import position_manager
from backend.positions.position_registry import position_registry
from backend.radar.candidate_feature_enhancer import candidate_feature_enhancer
from backend.radar.active_coins import ActiveCoinRegistry, active_coin_registry
from backend.market.dynamic_symbol_stream import DynamicSymbolStream, dynamic_symbol_stream
from backend.radar.market_classifier import market_classifier
from backend.radar.universal_anomaly_auto_trainer import UniversalAnomalyAutoTrainer
from backend.radar.universal_anomaly_model import UniversalAnomalyModel, universal_anomaly_model
from backend.radar.universal_anomaly_trainer import UniversalAnomalyClassifierTrainer
from backend.radar.universal_anomaly_training import UniversalAnomalyTrainingBuilder
from backend.radar.radar_engine import RadarEngine, radar_engine
from backend.storage.db import DB, db
from backend.trading.paper_executor import paper_executor
from backend.trading.performance_guard import PerformanceGuardReport, performance_guard
from backend.trading.ai_trade_director import ai_trade_director
from backend.trading.autotrader import autotrader
from backend.trading.trade_acceptance import trade_acceptance_runner
from backend.trading.production_acceptance import production_acceptance_runner
from backend.trading.exchange_reconciliation import exchange_reconciliation
from backend.trading.live_readiness import live_readiness
from backend.trading.live_executor import live_executor
from backend.account.account_service import account_service
from backend.market.binance_rest import binance_rest
from backend.exchange.binance_futures import binance_futures
import json
import logging
import sqlite3
import subprocess
import threading
from pathlib import Path
from types import SimpleNamespace
import pytest
from fastapi.testclient import TestClient
import backend.radar.radar_engine as radar_engine_module
import backend.main as main_module


@pytest.fixture(autouse=True)
def default_disable_event_calibration(monkeypatch):
    previous_acceptance = db.get_kv("production_acceptance.last_report", None)
    previous_trading_freeze = db.get_kv("live_executor.trading_freeze", None)
    monkeypatch.setattr(settings, "event_calibration_enabled", True)
    monkeypatch.setattr(settings, "trade_attribution_enabled", True)
    monkeypatch.setattr(settings, "trade_learning_guard_enabled", True)
    monkeypatch.setattr(settings, "radar_weight_calibration_enabled", False)
    monkeypatch.setattr(settings, "ai_position_review_enabled", False)
    binance_factor_source.last_refresh_degraded = False
    binance_factor_source.last_refresh_error = ""
    binance_factor_source.last_refresh_source = "test"
    binance_factor_source.last_symbol_count = 1
    binance_factor_source.last_snapshot_count = 1
    binance_factor_source.last_failed_symbols = []
    active_coin_registry.reset()
    dynamic_symbol_stream.reset()
    monkeypatch.setattr(
        exchange_reconciliation,
        "last_report",
        {
            "ok": True,
            "ts_ms": now_ms(),
            "age_seconds": 0.0,
            "skipped": False,
            "reason": "",
            "mode": "live",
            "testnet": False,
            "local_live_positions": [],
            "exchange_positions": [],
            "open_order_count": 0,
            "issues": [],
        },
    )
    monkeypatch.setattr(
        binance_futures,
        "_symbol_filters",
        {
            "BTCUSDT": {
                "raw": {"symbol": "BTCUSDT"},
                "filters": {
                    "LOT_SIZE": {"minQty": "0.001", "maxQty": "1000", "stepSize": "0.001"},
                    "MARKET_LOT_SIZE": {"minQty": "0.001", "maxQty": "1000", "stepSize": "0.001"},
                    "PRICE_FILTER": {"tickSize": "0.01"},
                    "MIN_NOTIONAL": {"notional": "0.01"},
                },
            }
        },
    )

    async def default_position_side_dual():
        return False

    monkeypatch.setattr(binance_futures, "position_side_dual", default_position_side_dual)

    async def default_price_quote(symbol_arg, side_arg=None):
        return PriceQuote(
            symbol=symbol_arg,
            price=100.0,
            source="book_ticker_mid",
            ts_ms=now_ms(),
            age_seconds=0.0,
            stale=False,
            bid=100.0,
            ask=100.0,
        )

    monkeypatch.setattr(market_service, "price_quote", default_price_quote)
    radar_weight_calibrator.clear_cache()
    learning_data_audit.clear_cache()
    monkeypatch.setattr(event_calibrator, "_samples", lambda: [])
    monkeypatch.setattr(trade_attributor, "_samples", lambda: [])
    monkeypatch.setattr(
        radar_engine_module,
        "learning_data_audit",
        SimpleNamespace(summary=lambda: {"market_backtest": {}}),
    )
    autotrader.ai_candidate_lock.clear()
    autotrader.ai_candidate_wait_cooldowns.clear()
    autotrader._candidate_geometry_samples.clear()
    ai_trade_director.last_cycle.clear()
    ai_trade_director.cycle_log.clear()
    universal_anomaly_model.clear_trained_artifact()
    yield
    universal_anomaly_model.clear_trained_artifact()
    db.set_kv("production_acceptance.last_report", previous_acceptance)
    db.set_kv("live_executor.trading_freeze", previous_trading_freeze)


def test_state_uses_realtime_quotes_for_major_prices_when_snapshots_missing(monkeypatch):
    market_service.last_snapshots = {}
    monkeypatch.setattr(binance_rest, "last_public_source", "mainnet")

    async def fake_price_quote(symbol_arg, side_arg=None):
        prices = {
            "BTCUSDT": 62661.9,
            "ETHUSDT": 3100.5,
            "BNBUSDT": 720.25,
            "SOLUSDT": 182.75,
        }
        return PriceQuote(
            symbol=symbol_arg,
            price=prices[symbol_arg],
            source="book_ticker_mid",
            ts_ms=now_ms(),
            age_seconds=0.0,
            stale=False,
            bid=prices[symbol_arg] - 0.01,
            ask=prices[symbol_arg] + 0.01,
        )

    monkeypatch.setattr(market_service, "price_quote", fake_price_quote)

    out = asyncio.run(main_module.state())
    majors = {row["symbol"]: row for row in out["major"]}

    assert out["market_data_source"] == "mainnet"
    assert majors["BTCUSDT"]["price"] == 62661.9
    assert majors["BTCUSDT"]["source"] == "book_ticker_mid"
    assert majors["BTCUSDT"]["stale"] is False


def test_state_uses_ws_ticker_change_for_major_when_snapshots_missing(monkeypatch):
    market_service.last_snapshots = {}
    monkeypatch.setattr(binance_rest, "last_public_source", "mainnet")
    monkeypatch.setattr(
        main_module.binance_ticker_stream,
        "snapshot_rows",
        lambda: [
            {"symbol": "BTCUSDT", "priceChangePercent": "1.23", "quoteVolume": "1234567"},
            {"symbol": "ETHUSDT", "priceChangePercent": "-0.45", "quoteVolume": "7654321"},
        ],
    )

    async def fake_price_quote(symbol_arg, side_arg=None):
        return PriceQuote(
            symbol=symbol_arg,
            price=62661.9 if symbol_arg == "BTCUSDT" else 100.0,
            source="book_ticker_mid",
            ts_ms=now_ms(),
            age_seconds=0.0,
            stale=False,
        )

    monkeypatch.setattr(market_service, "price_quote", fake_price_quote)

    out = asyncio.run(main_module.state())
    majors = {row["symbol"]: row for row in out["major"]}

    assert majors["BTCUSDT"]["price"] == 62661.9
    assert majors["BTCUSDT"]["change"] == 1.23
    assert majors["BTCUSDT"]["change_24h"] == 1.23
    assert majors["BTCUSDT"]["change_source"] == "ws_ticker_24h"
    assert majors["BTCUSDT"]["quote_volume_24h"] == 1234567.0
    assert majors["ETHUSDT"]["change"] == -0.45


def test_state_prefers_realtime_quote_over_cached_major_snapshot(monkeypatch):
    market_service.last_snapshots = {
        "BTCUSDT": MarketSnapshot("BTCUSDT", 1.0, 0.7, 0.8, 0.9, 1, 0, 0, 0.5, 0.5, 0, 0.1, 0.1)
    }
    monkeypatch.setattr(binance_rest, "last_public_source", "mainnet")

    async def fake_price_quote(symbol_arg, side_arg=None):
        price = 62661.9 if symbol_arg == "BTCUSDT" else 100.0
        return PriceQuote(
            symbol=symbol_arg,
            price=price,
            source="book_ticker_mid",
            ts_ms=now_ms(),
            age_seconds=0.0,
            stale=False,
        )

    monkeypatch.setattr(market_service, "price_quote", fake_price_quote)

    out = asyncio.run(main_module.state())
    majors = {row["symbol"]: row for row in out["major"]}

    assert majors["BTCUSDT"]["price"] == 62661.9
    assert majors["BTCUSDT"]["change"] == 0.7
    assert majors["BTCUSDT"]["source"] == "book_ticker_mid"


def test_state_exposes_market_data_contract_for_monitor_debugging(monkeypatch):
    monkeypatch.setattr(settings, "market_data_mode", "binance")
    monkeypatch.setattr(binance_rest, "last_public_source", "mainnet")
    monkeypatch.setattr(
        market_service,
        "last_snapshots",
        {
            "BTCUSDT": MarketSnapshot("BTCUSDT", 62661.9, 0.1, 0.2, 0.3, 1, 0, 0, 0.5, 0.5, 0, 0.1, 0.1),
            "ETHUSDT": MarketSnapshot("ETHUSDT", 3100.5, 0.1, 0.2, 0.3, 1, 0, 0, 0.5, 0.5, 0, 0.1, 0.1),
        },
    )
    monkeypatch.setattr(
        main_module.radar_engine,
        "scan_status",
        lambda compact=True: {
            "in_progress": False,
            "top50_count": 12,
            "last_error": "",
            "market_refresh": {
                "source": "ws_ticker",
                "degraded": False,
                "error": "",
                "snapshot_count": 80,
                "symbol_count": 80,
            },
            "active_coins": {"active_count": 7, "active_symbols": ["BTCUSDT"]},
            "dynamic_stream": {"active_count": 3, "running": True, "last_error": ""},
        },
    )

    out = asyncio.run(main_module.state())

    assert out["major_markets"] == out["major"]
    assert out["scan_status"]["top50_count"] == 12
    assert out["market_data"]["mode"] == "binance"
    assert out["market_data"]["public_source"] == "mainnet"
    assert out["market_data"]["refresh_source"] == "ws_ticker"
    assert out["market_data"]["degraded"] is False
    assert out["market_data"]["snapshot_count"] == 80
    assert out["market_data"]["active_coin_count"] == 7
    assert out["market_data"]["dynamic_stream_count"] == 3


def test_direction_short():
    s=MarketSnapshot("XUSDT",1,-1,-2,-3,2.5,1.0,0,0.3,0.7,-.2,.5,.1)
    assert direction(s)=="SHORT"
    assert fund_confirm(s,"SHORT")[0] >= 2


def test_radar_scan_helper_uses_current_event_loop(monkeypatch):
    calls = {}

    class FakeRadarEngine:
        async def scan(self, force_refresh=False):
            calls["scan_thread"] = threading.get_ident()
            calls["scan_loop"] = id(asyncio.get_running_loop())
            calls["force_refresh"] = force_refresh
            return ["scan-ok"]

    monkeypatch.setattr(main_module, "radar_engine", FakeRadarEngine())

    async def run_scan():
        calls["caller_thread"] = threading.get_ident()
        calls["caller_loop"] = id(asyncio.get_running_loop())
        return await main_module._radar_scan_offloop(force_refresh=True)

    result = asyncio.run(run_scan())

    assert result == ["scan-ok"]
    assert calls["force_refresh"] is True
    assert calls["scan_thread"] == calls["caller_thread"]
    assert calls["scan_loop"] == calls["caller_loop"]

def test_fund_confirm_uses_composite_current_market_evidence():
    s = MarketSnapshot("QUALITYUSDT", 1, 1.7, 2.1, 5.4, 1.74, 0.04, 0, 0.515, 0.485, -0.09, 1.2, 0.52)
    components = fund_confirm_components(s, "LONG")
    count, total = fund_confirm(s, "LONG")

    assert total == 5
    assert count >= 3
    assert components["volume_expansion"] is True
    assert components["oi_alignment"] is True
    assert components["timeframe_quality"] is True
    assert components["flow_or_book_alignment"] is False

def test_direction_does_not_treat_flat_change_as_short():
    s=MarketSnapshot("FLATUSDT",1,0,0,0,1,0,0,0.5,0.5,0,0.1,0.1)
    assert direction(s)=="NEUTRAL"

def test_fake_breakout_low_or_medium():
    s=MarketSnapshot("XUSDT",1,-.2,-.3,-.1,2.5,1.0,0,0.3,0.7,-.2,.5,.1)
    risk,score=fake_breakout(s,"SHORT")
    assert risk in ["LOW","MEDIUM","HIGH"]

def test_score_engine_explain_matches_total_score():
    features = {
        "trend_score": 80,
        "volume_score": 70,
        "volatility_score": 40,
        "oi_score": 90,
        "taker_score": 60,
        "timeframe_score": 80,
        "sm_score": 50,
        "heat_score": 30,
        "fake_penalty": 20,
    }
    explained = score_engine.explain(features)
    assert explained["score"] == score_engine.total(features)
    assert explained["components"]["fake_penalty"]["contribution"] < 0
    assert explained["caveat"] == "radar_score is an anomaly score, not a direct win-rate score"

def test_score_engine_explain_uses_custom_weights():
    features = {
        "trend_score": 80,
        "volume_score": 70,
        "volatility_score": 40,
        "oi_score": 90,
        "taker_score": 60,
        "timeframe_score": 80,
        "sm_score": 50,
        "heat_score": 30,
        "fake_penalty": 20,
    }
    weights = {**SCORE_WEIGHTS, "trend_score": 0.20, "fake_penalty": -0.20}
    explained = score_engine.explain(features, weights=weights, calibration={"active": True, "reason": "test"})
    assert explained["score"] == score_engine.total(features, weights=weights)
    assert explained["weights"]["trend_score"] == 0.20
    assert explained["calibration"]["active"] is True

def test_codex_cli_client_generates_plan(monkeypatch):
    monkeypatch.setattr(settings, "codex_model_provider", "chatgpt_http")
    monkeypatch.setattr(settings, "codex_provider_name", "ChatGPT HTTP")
    monkeypatch.setattr(settings, "codex_provider_requires_openai_auth", True)
    monkeypatch.setattr(settings, "codex_provider_supports_websockets", False)
    item = RadarItem(
        rank=1,
        symbol="XUSDT",
        base_asset="X",
        price=1,
        direction="SHORT",
        stage="confirming",
        trigger_mode="score_acceleration",
        score=74,
        score_history=[20, 40, 74],
        rank_history=[5, 1],
        heat_slope=12,
        slope_score=80,
        fake_breakout_risk="LOW",
        change_5m=-1,
        change_15m=-2,
        change_1h=-3,
        oi_change=1,
        fund_confirm_count=3,
        fund_confirm_total=3,
        dealer_radar="short_extend",
        sm_position=55,
        sm_delta=0.5,
    )
    payload = {
        "action": "WAIT",
        "symbol": "XUSDT",
        "side": "NEUTRAL",
        "entry_zone_low": 0,
        "entry_zone_high": 0,
        "ideal_entry_price": 1,
        "stop_loss": 0,
        "tp1": 0,
        "tp2": 0,
        "confidence": 0,
        "reason": "wait",
        "wait_type": "WAIT_FOR_CONFIRMATION",
        "expire_after_seconds": 180,
        "upgrade_condition": {
            "description": None,
            "fund_confirm_min": None,
            "score_min": None,
            "rank_max": None,
            "price_level": None,
            "timeout_seconds": None
        }
    }
    runner = FakeRunner(payload)
    client = CodexCLIStrategyClient(runner=runner, codex_command="codex")
    plan = __import__("asyncio").run(client.generate(item))
    assert plan.action == "WAIT"
    assert plan.raw["provider"] == "codex_cli"
    assert 'model_provider="chatgpt_http"' in runner.cmd
    assert "model_providers.chatgpt_http.requires_openai_auth=true" in runner.cmd
    assert "model_providers.chatgpt_http.supports_websockets=false" in runner.cmd
    assert f"model_reasoning_effort={normalized_codex_reasoning_effort()}" in runner.cmd
    assert "service_tier=fast" in runner.cmd
    status = client.status()
    assert status["invocation_count"] == 1
    assert status["last_status"] == "ok"
    assert status["last_symbol"] == "XUSDT"

def test_codex_generation_acceptance_fixture_is_open_long_contract_candidate():
    from backend.ai_strategy.codex_generation_acceptance import (
        build_acceptance_context,
        build_acceptance_item,
    )

    item = build_acceptance_item()
    context = build_acceptance_context(item)
    geometry = context["strategy_geometry_sample"]["selected_geometry"]
    generation_gate = context["ai_strategy_quality_feedback"]["candidate_feedback"]["generation_gate"]

    assert item.symbol == "BTCUSDT"
    assert item.direction == "LONG"
    assert item.score >= 95
    assert item.fund_confirm_count == item.fund_confirm_total >= 5
    assert item.fake_breakout_risk == "LOW"
    assert item.market_structure["action"] == "OPEN_LONG"
    assert geometry["side"] == "LONG"
    assert geometry["stop_loss"] < geometry["entry"] < geometry["tp1"] < geometry["tp2"]
    assert context["candidate_selection"]["source"] == "production_acceptance"
    assert context["candidate_selection"]["acceptance_mode"] is True
    assert generation_gate["allow_open_plan"] is True
    assert context["required_acceptance"]["expected_action"] == "OPEN_LONG"

def test_codex_generation_acceptance_context_overrides_learning_generation_gate():
    from backend.ai_strategy.codex_generation_acceptance import (
        build_acceptance_context,
        build_acceptance_item,
    )

    item = build_acceptance_item()
    context = build_acceptance_context(item)
    compressed = context_compressor.build_strategy_context(item, context)
    generation_gate = compressed["ai_strategy_quality_feedback"]["candidate_feedback"]["generation_gate"]
    cyqnt = compressed["cyqnt_feature_enhancement"]
    attribution = compressed["trade_attribution"]["current_signal_attribution"]
    event = compressed["event_calibration"]["similar_current_event"]

    assert compressed["position_context"]["candidate_selection"]["source"] == "production_acceptance"
    assert generation_gate["allow_open_plan"] is True
    assert generation_gate["review_required"] is False
    assert cyqnt["estimated_win_rate"] >= 0.60
    assert cyqnt["failure_risks"] == []
    assert attribution["paper_ok"] is True
    assert attribution["profit_factor"] >= 1.5
    assert event["win_rate"] >= 0.60

def test_codex_cli_uses_fast_model_for_strict_review(monkeypatch):
    monkeypatch.setattr(settings, "codex_model", "gpt-5.5")
    monkeypatch.setattr(settings, "codex_reasoning_effort", "medium")
    monkeypatch.setattr(settings, "codex_service_tier", "fast")
    monkeypatch.setattr(settings, "codex_fast_model", "gpt-5.5")
    monkeypatch.setattr(settings, "codex_fast_reasoning_effort", "medium")
    monkeypatch.setattr(settings, "codex_fast_timeout_seconds", 180)
    monkeypatch.setattr(settings, "codex_fast_service_tier", "fast")
    payload = {
        "action": "WAIT",
        "symbol": "FASTREVIEWUSDT",
        "side": "NEUTRAL",
        "entry_zone_low": 0,
        "entry_zone_high": 0,
        "ideal_entry_price": 100,
        "stop_loss": 0,
        "tp1": 0,
        "tp2": 0,
        "confidence": 0,
        "reason": "wait",
        "wait_type": "WAIT_FOR_CONFIRMATION",
        "expire_after_seconds": 180,
        "upgrade_condition": {},
    }
    runner = FakeRunner(payload)
    client = CodexCLIStrategyClient(runner=runner, codex_command="codex")
    item = high_quality_item(symbol="FASTREVIEWUSDT", side="LONG", price=100)

    plan = __import__("asyncio").run(
        client.generate(
            item,
            {
                "candidate_selection": {
                    "source": "strict_review",
                    "paper_validation": True,
                }
            },
        )
    )

    assert plan.raw["model"] == "gpt-5.5"
    assert plan.raw["model_route"] == "fast_validation"
    assert "model_reasoning_effort=medium" in runner.cmd
    assert runner.cmd[runner.cmd.index("-m") + 1] == "gpt-5.5"
    assert "service_tier=fast" in runner.cmd
    status = client.status()
    assert status["last_model"] == "gpt-5.5"
    assert status["last_route"] == "fast_validation"
    assert status["last_reasoning_effort"] == "medium"

def test_codex_cli_failure_waits_instead_of_rule_open(monkeypatch):
    item = high_quality_item(symbol="CODEXFAILUSDT", side="LONG", price=100)

    def invalid_json_runner(cmd, **kwargs):
        output_path = Path(cmd[cmd.index("--output-last-message") + 1])
        output_path.write_text("{bad json", encoding="utf-8")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    client = CodexCLIStrategyClient(runner=invalid_json_runner, codex_command="codex")
    plan = __import__("asyncio").run(client.generate(item))

    assert plan.action == "WAIT"
    assert plan.side == "NEUTRAL"
    assert plan.raw["provider"] == "codex_cli_unavailable"
    assert plan.raw["fallback_reason"] == "codex_invalid_json"
    status = client.status()
    assert status["invocation_count"] == 1
    assert status["last_status"] == "fallback_wait"
    assert status["last_action"] == "WAIT"
    assert status["last_error"] == "codex_invalid_json"

def test_codex_cli_status_reports_generation_unavailable_when_command_missing(monkeypatch):
    monkeypatch.setattr("backend.ai_strategy.codex_cli_strategy_client.shutil.which", lambda _command: "")

    client = CodexCLIStrategyClient(codex_command="missing-codex")
    status = client.status()

    assert status["command_found"] is False
    assert status["ready_for_generation"] is False
    assert status["availability_reason"] == "codex_command_missing"


def test_codex_cli_status_requires_auth_when_command_and_schema_exist(monkeypatch, tmp_path):
    schema = tmp_path / "strategy_plan.schema.json"
    schema.write_text("{}", encoding="utf-8")
    codex_home = tmp_path / "empty-codex-home"
    codex_home.mkdir()
    monkeypatch.setattr("backend.ai_strategy.codex_cli_strategy_client.shutil.which", lambda _command: "/usr/bin/codex")
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    client = CodexCLIStrategyClient(codex_command="codex", schema_path=schema)
    status = client.status()

    assert status["command_found"] is True
    assert status["schema_exists"] is True
    assert status["auth_required"] is True
    assert status["auth_available"] is False
    assert status["auth_source"] == ""
    assert status["ready_for_generation"] is False
    assert status["availability_reason"] == "codex_auth_missing"


def test_codex_cli_status_accepts_codex_home_auth_json(monkeypatch, tmp_path):
    schema = tmp_path / "strategy_plan.schema.json"
    schema.write_text("{}", encoding="utf-8")
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    (codex_home / "auth.json").write_text('{"tokens":"present"}', encoding="utf-8")
    monkeypatch.setattr("backend.ai_strategy.codex_cli_strategy_client.shutil.which", lambda _command: "/usr/bin/codex")
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    client = CodexCLIStrategyClient(codex_command="codex", schema_path=schema)
    status = client.status()

    assert status["auth_available"] is True
    assert status["auth_source"] == "codex_home_auth_json"
    assert status["ready_for_generation"] is True
    assert status["availability_reason"] == "ok"


def test_ai_strategy_status_does_not_claim_codex_invocation_when_command_missing(monkeypatch):
    import backend.ai_strategy.openai_strategy_client as strategy_client_module

    monkeypatch.setattr(settings, "ai_strategy_provider", "codex_cli")
    monkeypatch.setattr(settings, "ai_enabled", True)
    monkeypatch.setattr(settings, "require_codex_strategy_for_entry", True)
    monkeypatch.setattr(settings, "max_open_positions", 1)
    position_registry.open.clear()
    monkeypatch.setattr(
        strategy_client_module.codex_cli_strategy_client,
        "status",
        lambda: {
            "command_found": False,
            "ready_for_generation": False,
            "availability_reason": "codex_command_missing",
            "schema_exists": True,
        },
    )

    status = strategy_client_module.openai_strategy_client.status(candidate_count=1, candidate_source="paper_top")

    assert status["provider"] == "codex_cli"
    assert status["will_invoke_for_current_candidates"] is False
    assert status["not_invoked_reason"] == "codex_command_missing"

def test_codex_open_requires_valid_strategy_contract(monkeypatch):
    item = high_quality_item(symbol="NOCONTRACTOPENUSDT", side="LONG", price=100)
    payload = {
        "action": "OPEN_LONG",
        "symbol": item.symbol,
        "side": "LONG",
        "entry_zone_low": 99.8,
        "entry_zone_high": 100.2,
        "ideal_entry_price": 100,
        "stop_loss": 99,
        "tp1": 101.2,
        "tp2": 103,
        "confidence": 75,
        "reason": "open without contract",
        "strategy_contract": {},
    }
    runner = FakeRunner(payload)
    client = CodexCLIStrategyClient(runner=runner, codex_command="codex")

    plan = __import__("asyncio").run(client.generate(item))

    assert plan.action == "WAIT"
    assert plan.raw["provider"] == "codex_cli_unavailable"
    assert "codex_open_missing_valid_strategy_contract" in plan.raw["fallback_reason"]

def test_codex_open_contract_repair_uses_codex_second_pass(monkeypatch):
    item = high_quality_item(symbol="REPAIRCONTRACTUSDT", side="LONG", price=100)
    repaired_plan = StrategyPlan(
        strategy_id="repaired",
        action="OPEN_LONG",
        symbol=item.symbol,
        side="LONG",
        entry_zone_low=99.8,
        entry_zone_high=100.2,
        ideal_entry_price=100,
        stop_loss=98,
        tp1=101,
        tp2=103,
        confidence=72,
        reason="repaired by codex",
        wait_type="",
        expire_after_seconds=180,
        raw={"provider": "codex_cli"},
    )
    bad_payload = {
        "action": "OPEN_LONG",
        "symbol": item.symbol,
        "side": "LONG",
        "entry_zone_low": 99.8,
        "entry_zone_high": 100.2,
        "ideal_entry_price": 100,
        "stop_loss": 98,
        "tp1": 101,
        "tp2": 103,
        "confidence": 72,
        "reason": "missing contract",
        "wait_type": "",
        "expire_after_seconds": 180,
        "upgrade_condition": {},
        "strategy_contract": {},
    }
    repaired_payload = {
        **bad_payload,
        "reason": "repaired by codex",
        "strategy_contract": build_rule_contract(item, repaired_plan),
    }

    class SequenceRunner:
        def __init__(self, payloads):
            self.payloads = list(payloads)
            self.calls = 0
            self.cmd = []
            self.inputs = []

        def __call__(self, cmd, **kwargs):
            self.calls += 1
            self.cmd = cmd
            self.inputs.append(kwargs.get("input", ""))
            payload = self.payloads.pop(0)
            output_path = Path(cmd[cmd.index("--output-last-message") + 1])
            output_path.write_text(json.dumps(payload), encoding="utf-8")
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    runner = SequenceRunner([bad_payload, repaired_payload])
    client = CodexCLIStrategyClient(runner=runner, codex_command="codex")

    plan = __import__("asyncio").run(client.generate(item))

    assert runner.calls == 2
    assert "failed local validation" in runner.inputs[1]
    assert plan.action == "OPEN_LONG"
    assert plan.raw["strategy_contract_quality"]["ok"] is True
    status = client.status()
    assert status["last_repair_attempted"] is True
    assert "codex_open_missing_valid_strategy_contract" in status["last_repair_reason"]


def test_codex_generation_gate_locally_blocks_open_even_if_model_ignores_it(monkeypatch):
    item = high_quality_item(symbol="GATEBLOCKUSDT", side="LONG", price=100)
    draft_plan = StrategyPlan(
        strategy_id="model_ignored_gate",
        action="OPEN_LONG",
        symbol=item.symbol,
        side="LONG",
        entry_zone_low=99.8,
        entry_zone_high=100.2,
        ideal_entry_price=100,
        stop_loss=98,
        tp1=101.2,
        tp2=103,
        confidence=78,
        reason="model ignored generation gate",
        wait_type="",
        expire_after_seconds=180,
        raw={"provider": "codex_cli"},
    )
    payload = {
        "action": "OPEN_LONG",
        "symbol": item.symbol,
        "side": "LONG",
        "entry_zone_low": 99.8,
        "entry_zone_high": 100.2,
        "ideal_entry_price": 100,
        "stop_loss": 98,
        "tp1": 101.2,
        "tp2": 103,
        "confidence": 78,
        "reason": "model ignored generation gate",
        "wait_type": "",
        "expire_after_seconds": 180,
        "upgrade_condition": {},
        "strategy_contract": build_rule_contract(item, draft_plan),
    }
    monkeypatch.setattr(
        "backend.ai_strategy.context_compressor.ai_strategy_feedback.compact_context",
        lambda _item: {
            "candidate_feedback": {
                "generation_gate": {
                    "allow_open_plan": False,
                    "reasons": ["avoid_repeating", "hard_failure_risk:market_stale"],
                    "instruction": "If allow_open_plan is false, Codex must return WAIT.",
                }
            }
        },
    )
    runner = FakeRunner(payload)
    client = CodexCLIStrategyClient(runner=runner, codex_command="codex")

    plan = __import__("asyncio").run(client.generate(item))

    assert plan.action == "WAIT"
    assert plan.wait_type == "WAIT_FOR_STRATEGY_QUALITY_GATE"
    assert plan.raw["provider"] == "codex_cli"
    assert plan.raw["generation_gate"]["allow_open_plan"] is False
    assert plan.raw["generation_gate"]["reasons"] == ["avoid_repeating", "hard_failure_risk:market_stale"]
    assert plan.raw["upgrade_condition"]["generation_gate_reasons"] == [
        "avoid_repeating",
        "hard_failure_risk:market_stale",
    ]
    assert client.status()["last_status"] == "quality_wait"


def test_codex_prompt_includes_strategy_geometry_sample(monkeypatch):
    monkeypatch.setattr(settings, "codex_model", "gpt-5.5")
    item = high_quality_item(symbol="GEOMETRYPROMPTUSDT", side="LONG", price=100)

    async def fake_geometry(item_arg):
        return {
            "status": "ok",
            "sample_model": "first_touch_geometry_v1",
            "selected_geometry": {"side": "LONG", "entry": 100, "stop_loss": 99, "tp1": 101.2, "tp2": 102.4},
            "samples": {"sample_count": 90, "win_rate": 0.62, "expected_r": 0.31, "profit_factor": 1.44},
        }

    monkeypatch.setattr(
        "backend.ai_strategy.codex_cli_strategy_client.strategy_geometry_sampler.evaluate",
        fake_geometry,
    )
    payload = {
        "action": "WAIT",
        "symbol": item.symbol,
        "side": "NEUTRAL",
        "entry_zone_low": 0,
        "entry_zone_high": 0,
        "ideal_entry_price": 100,
        "stop_loss": 0,
        "tp1": 0,
        "tp2": 0,
        "confidence": 0,
        "reason": "wait",
        "wait_type": "WAIT_FOR_CONFIRMATION",
        "expire_after_seconds": 180,
        "upgrade_condition": {},
    }

    class CaptureRunner:
        def __init__(self, payload):
            self.payload = payload
            self.inputs = []

        def __call__(self, cmd, **kwargs):
            self.inputs.append(kwargs.get("input", ""))
            output_path = Path(cmd[cmd.index("--output-last-message") + 1])
            output_path.write_text(json.dumps(self.payload), encoding="utf-8")
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    runner = CaptureRunner(payload)
    client = CodexCLIStrategyClient(runner=runner, codex_command="codex")
    __import__("asyncio").run(client.generate(item))

    assert runner.inputs
    assert "strategy_geometry_sample" in runner.inputs[0]
    assert "first_touch_geometry_v1" in runner.inputs[0]
    assert "selected_geometry" in runner.inputs[0]

def test_codex_client_reuses_preselected_strategy_geometry_sample(monkeypatch):
    item = high_quality_item(symbol="GEOMETRYREUSEUSDT", side="LONG", price=100)
    preselected_sample = {
        "status": "ok",
        "sample_model": "preselected_geometry_v1",
        "selected_geometry": {"side": "LONG", "entry": 100, "stop_loss": 99, "tp1": 101, "tp2": 103},
        "samples": {"sample_count": 88, "win_rate": 0.63, "expected_r": 0.36, "profit_factor": 1.7},
    }

    async def fail_geometry(_item):
        raise AssertionError("preselected geometry should be reused")

    monkeypatch.setattr(
        "backend.ai_strategy.codex_cli_strategy_client.strategy_geometry_sampler.evaluate",
        fail_geometry,
    )
    payload = {
        "action": "WAIT",
        "symbol": item.symbol,
        "side": "NEUTRAL",
        "entry_zone_low": 0,
        "entry_zone_high": 0,
        "ideal_entry_price": 100,
        "stop_loss": 0,
        "tp1": 0,
        "tp2": 0,
        "confidence": 0,
        "reason": "wait",
        "wait_type": "WAIT_FOR_CONFIRMATION",
        "expire_after_seconds": 180,
        "upgrade_condition": {},
    }

    class CaptureRunner:
        def __init__(self, payload):
            self.payload = payload
            self.inputs = []

        def __call__(self, cmd, **kwargs):
            self.inputs.append(kwargs.get("input", ""))
            output_path = Path(cmd[cmd.index("--output-last-message") + 1])
            output_path.write_text(json.dumps(self.payload), encoding="utf-8")
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    runner = CaptureRunner(payload)
    client = CodexCLIStrategyClient(runner=runner, codex_command="codex")
    plan = __import__("asyncio").run(client.generate(item, {"strategy_geometry_sample": preselected_sample}))

    assert plan.raw["strategy_geometry_sample"] == preselected_sample
    assert "preselected_geometry_v1" in runner.inputs[0]

def test_binance_factor_source_builds_real_factor_snapshot(monkeypatch):
    monkeypatch.setattr(settings, "radar_exclude_major_symbols_from_anomaly", False)
    monkeypatch.setattr(settings, "binance_symbol_limit", 2)
    monkeypatch.setattr(settings, "binance_factor_concurrency", 2)
    monkeypatch.setattr(settings, "binance_factor_ttl_seconds", 0)
    monkeypatch.setattr(settings, "binance_use_open_interest_hist", True)
    monkeypatch.setattr(settings, "binance_use_taker_ratio_endpoint", True)
    source = BinanceFactorSource(client=FakeBinanceMarketClient())
    snaps = __import__("asyncio").run(source.get_snapshots())
    assert len(snaps) == 2
    btc = next(s for s in snaps if s.symbol == "BTCUSDT")
    assert btc.price == 129
    assert btc.change_5m > 0
    assert btc.change_15m > 0
    assert btc.change_1h > 0
    assert btc.volume_spike > 1
    assert btc.oi_change > 0
    assert btc.funding_rate > 0
    assert btc.taker_buy_ratio > 0.5
    assert btc.taker_sell_ratio < 0.5
    assert btc.depth_imbalance > 0
    assert btc.atr_pct > 0
    assert 0 <= btc.wick_ratio <= 1


def test_binance_factor_source_prefers_last_price_over_mark_price(monkeypatch):
    monkeypatch.setattr(settings, "radar_exclude_major_symbols_from_anomaly", False)
    monkeypatch.setattr(settings, "binance_symbol_limit", 1)
    monkeypatch.setattr(settings, "binance_factor_concurrency", 1)
    monkeypatch.setattr(settings, "binance_factor_ttl_seconds", 0)
    monkeypatch.setattr(settings, "binance_use_open_interest_hist", False)
    monkeypatch.setattr(settings, "binance_use_taker_ratio_endpoint", False)

    class DivergedPriceClient(FakeBinanceMarketClient):
        async def exchange_info(self):
            return {"symbols": [_exchange_symbol_meta("BTCUSDT")]}

        async def premium_index(self):
            return [{"symbol": "BTCUSDT", "markPrice": "125", "lastFundingRate": "0.0001"}]

        async def ticker_24hr(self, symbol=None):
            return [{"symbol": "BTCUSDT", "lastPrice": "129", "quoteVolume": "2000000"}]

    source = BinanceFactorSource(client=DivergedPriceClient())
    snaps = __import__("asyncio").run(source.get_snapshots(force_refresh=True))

    assert len(snaps) == 1
    assert snaps[0].symbol == "BTCUSDT"
    assert snaps[0].price == 129


def test_binance_factor_source_keeps_snapshot_when_kline_missing(monkeypatch):
    monkeypatch.setattr(settings, "radar_exclude_major_symbols_from_anomaly", False)
    monkeypatch.setattr(settings, "binance_symbol_limit", 1)
    monkeypatch.setattr(settings, "binance_factor_concurrency", 1)
    monkeypatch.setattr(settings, "binance_factor_ttl_seconds", 0)
    monkeypatch.setattr(settings, "binance_use_open_interest_hist", False)
    monkeypatch.setattr(settings, "binance_use_taker_ratio_endpoint", False)

    class MissingKlineClient(FakeBinanceMarketClient):
        async def klines(self, symbol, interval="5m", limit=30):
            return []

    source = BinanceFactorSource(client=MissingKlineClient())
    snaps = __import__("asyncio").run(source.get_snapshots(force_refresh=True))

    assert len(snaps) == 1
    assert snaps[0].symbol == "BTCUSDT"
    assert snaps[0].price > 0
    assert "kline_missing" in (snaps[0].structure_metrics.get("quality_blockers") or [])
    assert source.last_failed_symbols == []


def test_binance_factor_source_does_not_degrade_for_sparse_kline_missing(monkeypatch):
    monkeypatch.setattr(settings, "radar_exclude_major_symbols_from_anomaly", False)
    monkeypatch.setattr(settings, "binance_symbol_limit", 10)
    monkeypatch.setattr(settings, "binance_factor_concurrency", 2)
    monkeypatch.setattr(settings, "binance_factor_ttl_seconds", 0)
    monkeypatch.setattr(settings, "binance_use_open_interest_hist", False)
    monkeypatch.setattr(settings, "binance_use_taker_ratio_endpoint", False)

    class SparseMissingKlineClient(FakeBinanceMarketClient):
        symbols = [f"SPARSE{i}USDT" for i in range(10)]

        async def exchange_info(self):
            return {"symbols": [_exchange_symbol_meta(symbol) for symbol in self.symbols]}

        async def premium_index(self):
            return [{"symbol": symbol, "markPrice": str(100 + idx), "lastFundingRate": "0"} for idx, symbol in enumerate(self.symbols)]

        async def ticker_24hr(self, symbol=None):
            return [{"symbol": symbol, "lastPrice": str(100 + idx), "quoteVolume": "1000000"} for idx, symbol in enumerate(self.symbols)]

        async def klines(self, symbol, interval="5m", limit=30):
            if symbol == "SPARSE0USDT":
                return []
            return await super().klines("BTCUSDT", interval, limit)

    source = BinanceFactorSource(client=SparseMissingKlineClient())
    snaps = __import__("asyncio").run(source.get_snapshots(force_refresh=True))

    assert len(snaps) == 10
    assert source.last_refresh_degraded is False
    assert source.last_kline_missing_symbols == ["SPARSE0USDT"]
    sparse = next(row for row in snaps if row.symbol == "SPARSE0USDT")
    assert "kline_missing" in (sparse.structure_metrics.get("quality_blockers") or [])


def test_binance_factor_source_degrades_for_excessive_kline_missing(monkeypatch):
    monkeypatch.setattr(settings, "radar_exclude_major_symbols_from_anomaly", False)
    monkeypatch.setattr(settings, "binance_symbol_limit", 10)
    monkeypatch.setattr(settings, "binance_factor_concurrency", 2)
    monkeypatch.setattr(settings, "binance_factor_ttl_seconds", 0)
    monkeypatch.setattr(settings, "binance_use_open_interest_hist", False)
    monkeypatch.setattr(settings, "binance_use_taker_ratio_endpoint", False)

    class ExcessiveMissingKlineClient(FakeBinanceMarketClient):
        symbols = [f"EXMISS{i}USDT" for i in range(10)]

        async def exchange_info(self):
            return {"symbols": [_exchange_symbol_meta(symbol) for symbol in self.symbols]}

        async def premium_index(self):
            return [{"symbol": symbol, "markPrice": str(100 + idx), "lastFundingRate": "0"} for idx, symbol in enumerate(self.symbols)]

        async def ticker_24hr(self, symbol=None):
            return [{"symbol": symbol, "lastPrice": str(100 + idx), "quoteVolume": "1000000"} for idx, symbol in enumerate(self.symbols)]

        async def klines(self, symbol, interval="5m", limit=30):
            if symbol in set(self.symbols[:3]):
                return []
            return await super().klines("BTCUSDT", interval, limit)

    source = BinanceFactorSource(client=ExcessiveMissingKlineClient())
    snaps = __import__("asyncio").run(source.get_snapshots(force_refresh=True))

    assert len(snaps) == 10
    assert source.last_refresh_degraded is True
    assert "snapshot_quality:kline_missing:3/10" in source.last_refresh_error
    assert source.last_kline_missing_symbols[:3] == ["EXMISS0USDT", "EXMISS1USDT", "EXMISS2USDT"]


def test_binance_symbol_selection_includes_movers_after_priority(monkeypatch):
    monkeypatch.setattr(settings, "radar_exclude_major_symbols_from_anomaly", False)
    monkeypatch.setattr(settings, "binance_symbol_limit", 4)
    monkeypatch.setattr(settings, "binance_mover_share", 0.5)
    source = BinanceFactorSource(client=FakeBinanceMarketClient())
    premiums = {
        "BTCUSDT": {"markPrice": "100"},
        "ETHUSDT": {"markPrice": "100"},
        "BIGUSDT": {"markPrice": "100"},
        "MOVEUSDT": {"markPrice": "100"},
        "QUIETUSDT": {"markPrice": "100"},
    }
    tickers = {
        "BTCUSDT": {"quoteVolume": "10", "priceChangePercent": "0.1"},
        "ETHUSDT": {"quoteVolume": "9", "priceChangePercent": "0.2"},
        "BIGUSDT": {"quoteVolume": "1000", "priceChangePercent": "0.3"},
        "MOVEUSDT": {"quoteVolume": "1", "priceChangePercent": "-19"},
        "QUIETUSDT": {"quoteVolume": "800", "priceChangePercent": "0.1"},
    }
    selected = source._select_symbols(premiums, tickers)
    assert selected == ["BTCUSDT", "ETHUSDT", "BIGUSDT", "MOVEUSDT"]


def test_binance_symbol_selection_preserves_anomaly_slots_when_major_symbols_excluded(monkeypatch):
    monkeypatch.setattr(settings, "radar_exclude_major_symbols_from_anomaly", True)
    monkeypatch.setattr(settings, "radar_major_symbols", "BTCUSDT,ETHUSDT,BNBUSDT,SOLUSDT")
    monkeypatch.setattr(settings, "binance_symbol_limit", 4)
    monkeypatch.setattr(settings, "binance_mover_share", 0.5)
    source = BinanceFactorSource(client=FakeBinanceMarketClient())
    premiums = {
        "BTCUSDT": {"markPrice": "100"},
        "ETHUSDT": {"markPrice": "100"},
        "BNBUSDT": {"markPrice": "100"},
        "SOLUSDT": {"markPrice": "100"},
        "BIGALTUSDT": {"markPrice": "100"},
        "MOVEALTUSDT": {"markPrice": "100"},
        "ACTALTUSDT": {"markPrice": "100"},
        "QUIETALTUSDT": {"markPrice": "100"},
    }
    tickers = {
        "BTCUSDT": {"quoteVolume": "9999", "priceChangePercent": "0.1"},
        "ETHUSDT": {"quoteVolume": "9000", "priceChangePercent": "0.2"},
        "BNBUSDT": {"quoteVolume": "8000", "priceChangePercent": "0.3"},
        "SOLUSDT": {"quoteVolume": "7000", "priceChangePercent": "0.4"},
        "BIGALTUSDT": {"quoteVolume": "6000", "priceChangePercent": "0.5"},
        "MOVEALTUSDT": {"quoteVolume": "10", "priceChangePercent": "18"},
        "ACTALTUSDT": {"quoteVolume": "500", "priceChangePercent": "8"},
        "QUIETALTUSDT": {"quoteVolume": "3000", "priceChangePercent": "0.1"},
    }

    selected = source._select_symbols(premiums, tickers)

    assert len(selected) == 4
    assert not (set(selected) & {"BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT"})
    assert "MOVEALTUSDT" in selected
    assert "BIGALTUSDT" in selected


def test_binance_symbol_selection_excludes_tradifi_perpetuals(monkeypatch):
    monkeypatch.setattr(settings, "radar_exclude_major_symbols_from_anomaly", False)
    monkeypatch.setattr(settings, "binance_symbol_limit", 4)
    monkeypatch.setattr(settings, "binance_mover_share", 0.5)
    source = BinanceFactorSource(client=FakeBinanceMarketClient())
    source._exchange_symbol_meta = {
        "BTCUSDT": {
            "symbol": "BTCUSDT",
            "status": "TRADING",
            "contractType": "PERPETUAL",
            "quoteAsset": "USDT",
            "marginAsset": "USDT",
            "underlyingType": "COIN",
            "underlyingSubType": ["PoW"],
        },
        "GUAUSDT": {
            "symbol": "GUAUSDT",
            "status": "TRADING",
            "contractType": "PERPETUAL",
            "quoteAsset": "USDT",
            "marginAsset": "USDT",
            "underlyingType": "COIN",
            "underlyingSubType": ["Alpha"],
        },
        "SKHYNIXUSDT": {
            "symbol": "SKHYNIXUSDT",
            "status": "TRADING",
            "contractType": "TRADIFI_PERPETUAL",
            "quoteAsset": "USDT",
            "marginAsset": "USDT",
            "underlyingType": "KR_EQUITY",
            "underlyingSubType": ["TradFi"],
        },
        "XAUUSDT": {
            "symbol": "XAUUSDT",
            "status": "TRADING",
            "contractType": "TRADIFI_PERPETUAL",
            "quoteAsset": "USDT",
            "marginAsset": "USDT",
            "underlyingType": "COMMODITY",
            "underlyingSubType": ["TradFi"],
        },
    }
    premiums = {
        "BTCUSDT": {"markPrice": "100"},
        "GUAUSDT": {"markPrice": "1"},
        "SKHYNIXUSDT": {"markPrice": "300000"},
        "XAUUSDT": {"markPrice": "3300"},
    }
    tickers = {
        "BTCUSDT": {"quoteVolume": "10", "priceChangePercent": "0.1"},
        "GUAUSDT": {"quoteVolume": "800", "priceChangePercent": "4"},
        "SKHYNIXUSDT": {"quoteVolume": "900000", "priceChangePercent": "80"},
        "XAUUSDT": {"quoteVolume": "700000", "priceChangePercent": "8"},
    }

    selected = source._select_symbols(premiums, tickers)

    assert selected == ["BTCUSDT", "GUAUSDT"]

def test_binance_factor_source_skips_discontinuous_kline_symbols(monkeypatch):
    monkeypatch.setattr(settings, "binance_symbol_limit", 2)
    monkeypatch.setattr(settings, "binance_factor_concurrency", 2)
    monkeypatch.setattr(settings, "binance_factor_ttl_seconds", 0)
    monkeypatch.setattr(settings, "binance_use_open_interest_hist", False)
    monkeypatch.setattr(settings, "binance_use_taker_ratio_endpoint", False)

    class GapClient(FakeBinanceMarketClient):
        async def exchange_info(self):
            return {
                "symbols": [
                    {
                        "symbol": "GOODUSDT",
                        "status": "TRADING",
                        "contractType": "PERPETUAL",
                        "quoteAsset": "USDT",
                        "marginAsset": "USDT",
                        "underlyingType": "COIN",
                        "underlyingSubType": ["Layer-1"],
                    },
                    {
                        "symbol": "GAPUSDT",
                        "status": "TRADING",
                        "contractType": "PERPETUAL",
                        "quoteAsset": "USDT",
                        "marginAsset": "USDT",
                        "underlyingType": "COIN",
                        "underlyingSubType": ["Meme"],
                    },
                ]
            }

        async def premium_index(self):
            return [
                {"symbol": "GOODUSDT", "markPrice": "109", "lastFundingRate": "0.0001"},
                {"symbol": "GAPUSDT", "markPrice": "0.7", "lastFundingRate": "0.0001"},
            ]

        async def ticker_24hr(self, symbol=None):
            return [
                {"symbol": "GOODUSDT", "lastPrice": "109", "quoteVolume": "1000", "priceChangePercent": "2"},
                {"symbol": "GAPUSDT", "lastPrice": "0.7", "quoteVolume": "9000", "priceChangePercent": "80"},
            ]

        async def klines(self, symbol, interval="5m", limit=30):
            if symbol == "GAPUSDT":
                closes = [2.6] * 27 + [2.55, 0.63, 0.7]
            else:
                closes = [100 + idx * 0.3 for idx in range(30)]
            rows = []
            for idx, close in enumerate(closes):
                open_price = closes[idx - 1] if idx else close
                high = max(open_price, close) * 1.002
                low = min(open_price, close) * 0.998
                rows.append([
                    idx,
                    str(open_price),
                    str(high),
                    str(low),
                    str(close),
                    "10",
                    idx + 1,
                    "1000",
                    100,
                    "7",
                    "600",
                    "0",
                ])
            return rows

    source = BinanceFactorSource(client=GapClient())
    snaps = __import__("asyncio").run(source.get_snapshots(force_refresh=True))

    assert [snap.symbol for snap in snaps] == ["GOODUSDT"]
    assert any("GAPUSDT:ValueError" in item for item in source.last_failed_symbols)

def test_binance_factor_force_refresh_bypasses_cache(monkeypatch):
    monkeypatch.setattr(settings, "binance_symbol_limit", 1)
    monkeypatch.setattr(settings, "binance_factor_concurrency", 1)
    monkeypatch.setattr(settings, "binance_factor_ttl_seconds", 999)
    monkeypatch.setattr(settings, "binance_use_open_interest_hist", False)
    monkeypatch.setattr(settings, "binance_use_taker_ratio_endpoint", False)
    client = FakeBinanceMarketClient()
    client.kline_calls = 0
    source = BinanceFactorSource(client=client)
    __import__("asyncio").run(source.get_snapshots())
    __import__("asyncio").run(source.get_snapshots())
    assert client.kline_calls == 1
    __import__("asyncio").run(source.get_snapshots(force_refresh=True))
    assert client.kline_calls == 2

def test_binance_factor_source_degrades_to_ticker_prices_when_premium_fails(monkeypatch):
    monkeypatch.setattr(settings, "binance_symbol_limit", 1)
    monkeypatch.setattr(settings, "binance_factor_concurrency", 1)
    monkeypatch.setattr(settings, "binance_factor_ttl_seconds", 0)
    monkeypatch.setattr(settings, "binance_use_open_interest_hist", False)
    monkeypatch.setattr(settings, "binance_use_taker_ratio_endpoint", False)
    monkeypatch.setattr("backend.market.binance_factor_source.binance_ticker_stream.snapshot_rows", lambda: [])

    class PremiumFailClient(FakeBinanceMarketClient):
        async def premium_index(self):
            raise TimeoutError("pool")

    source = BinanceFactorSource(client=PremiumFailClient())
    snaps = __import__("asyncio").run(source.get_snapshots(force_refresh=True))

    assert len(snaps) == 1
    assert snaps[0].price > 0
    assert source.last_refresh_degraded
    assert "premium_index:TimeoutError" in source.last_refresh_error
    assert "premium_index_missing_using_ticker_prices" in source.last_refresh_error

def test_binance_factor_source_uses_cache_when_market_rows_fail(monkeypatch):
    monkeypatch.setattr(settings, "binance_symbol_limit", 1)
    monkeypatch.setattr(settings, "binance_factor_concurrency", 1)
    monkeypatch.setattr(settings, "binance_factor_ttl_seconds", 0)
    monkeypatch.setattr(settings, "binance_use_open_interest_hist", False)
    monkeypatch.setattr(settings, "binance_use_taker_ratio_endpoint", False)
    monkeypatch.setattr("backend.market.binance_factor_source.binance_ticker_stream.snapshot_rows", lambda: [])

    class MarketRowsFailClient(FakeBinanceMarketClient):
        async def premium_index(self):
            raise TimeoutError("pool")

        async def ticker_24hr(self, symbol=None):
            raise TimeoutError("pool")

    source = BinanceFactorSource(client=FakeBinanceMarketClient())
    cached = __import__("asyncio").run(source.get_snapshots(force_refresh=True))
    source.client = MarketRowsFailClient()
    degraded = __import__("asyncio").run(source.get_snapshots(force_refresh=True))

    assert [row.symbol for row in degraded] == [row.symbol for row in cached]
    assert source.last_refresh_degraded
    assert "market_rows_unavailable_using_cache" in source.last_refresh_error


def test_binance_factor_source_records_market_row_exception_detail(monkeypatch):
    monkeypatch.setattr(settings, "binance_symbol_limit", 1)
    monkeypatch.setattr(settings, "binance_factor_concurrency", 1)
    monkeypatch.setattr(settings, "binance_factor_ttl_seconds", 0)
    monkeypatch.setattr(settings, "binance_use_open_interest_hist", False)
    monkeypatch.setattr(settings, "binance_use_taker_ratio_endpoint", False)
    monkeypatch.setattr("backend.market.binance_factor_source.binance_ticker_stream.snapshot_rows", lambda: [])

    class MarketRowsFailClient(FakeBinanceMarketClient):
        async def premium_index(self):
            raise RuntimeError("read timeout after 5s")

        async def ticker_24hr(self, symbol=None):
            raise RuntimeError("pool exhausted")

    source = BinanceFactorSource(client=MarketRowsFailClient())
    degraded = __import__("asyncio").run(source.get_snapshots(force_refresh=True))

    assert degraded == []
    assert "premium_index:RuntimeError:read timeout after 5s" in source.last_refresh_error
    assert "ticker_24hr:RuntimeError:pool exhausted" in source.last_refresh_error


def test_binance_factor_source_records_exchange_info_exception_detail(monkeypatch):
    monkeypatch.setattr(settings, "binance_symbol_limit", 1)
    monkeypatch.setattr(settings, "binance_factor_concurrency", 1)
    monkeypatch.setattr(settings, "binance_factor_ttl_seconds", 0)
    monkeypatch.setattr(settings, "binance_crypto_perpetual_only", True)
    monkeypatch.setattr("backend.market.binance_factor_source.binance_ticker_stream.snapshot_rows", lambda: [])

    class ExchangeInfoFailClient(FakeBinanceMarketClient):
        async def exchange_info(self):
            raise RuntimeError("exchange info timed out")

    source = BinanceFactorSource(client=ExchangeInfoFailClient())
    degraded = __import__("asyncio").run(source.get_snapshots(force_refresh=True))

    assert degraded == []
    assert "exchange_info:RuntimeError:exchange info timed out" in source.last_refresh_error
    assert "exchange_info_unavailable_no_cache" in source.last_refresh_error


def test_binance_public_http_timeout_allows_slow_market_row_endpoints(monkeypatch):
    from backend.exchange.binance_futures import binance_http_timeout

    monkeypatch.setattr(settings, "binance_http_timeout", 5.0)

    timeout = binance_http_timeout()

    assert timeout.read >= 10.0

def test_market_service_uses_book_ticker_for_position_valuation(monkeypatch):
    monkeypatch.setattr(settings, "market_data_mode", "binance")

    class FakeRest:
        async def book_ticker(self, symbol=None):
            return {
                "symbol": symbol,
                "bidPrice": "1845.22000",
                "askPrice": "1845.25000",
                "time": now_ms(),
            }

    monkeypatch.setattr("backend.market.market_service.binance_rest", FakeRest())
    service = MarketService()
    quote = __import__("asyncio").run(service.price_quote("SKHYNIXUSDT", "SHORT"))

    assert quote.price == 1845.25
    assert quote.source == "book_ticker_ask_close_short"
    assert quote.bid == 1845.22
    assert quote.ask == 1845.25
    assert quote.stale is False

def test_market_service_does_not_use_weighted_average_as_current_price(monkeypatch):
    monkeypatch.setattr(settings, "market_data_mode", "binance")

    class FakeRest:
        async def book_ticker(self, symbol=None):
            raise TimeoutError("book unavailable")

        async def ticker_price(self, symbol=None):
            raise TimeoutError("price unavailable")

        async def public_get(self, path, params=None):
            raise TimeoutError("mark unavailable")

        async def ticker_24hr(self, symbol=None):
            return {
                "symbol": symbol,
                "weightedAvgPrice": "1903.62662",
                "lastPrice": "1845.03000",
                "closeTime": now_ms(),
            }

    monkeypatch.setattr("backend.market.market_service.binance_rest", FakeRest())
    service = MarketService()
    quote = __import__("asyncio").run(service.price_quote("SKHYNIXUSDT", "SHORT"))

    assert quote.price == 1845.03
    assert quote.source == "ticker_24hr_last_price"
    assert quote.price != 1903.62662

def test_strict_ai_candidates_use_cyqnt_quality_not_raw_score_only(monkeypatch):
    strong = high_quality_item(symbol="CYQNTGOODUSDT", side="LONG", price=100)
    strong.score = 50
    partial = high_quality_item(symbol="PARTIALUSDT", side="LONG", price=100)
    partial.score = 80
    partial.fund_confirm_count = 2
    noisy = high_quality_item(symbol="NOISYUSDT", side="LONG", price=100)
    noisy.score = 80
    noisy.wick_ratio = 0.95

    def fake_feature(item):
        return SimpleNamespace(
            feature_score=72.0,
            estimated_win_rate=0.61,
            selection_score=78.0,
            reasons=["test_cyqnt_strong"],
        )

    monkeypatch.setattr("backend.radar.radar_engine.candidate_feature_enhancer.evaluate", fake_feature)

    selected = radar_engine.select_ai_candidates([partial, noisy, strong])

    assert [item.symbol for item in selected] == ["CYQNTGOODUSDT"]

def test_kline_features_use_rolling_close_to_close_change():
    rows = []
    for i, close in enumerate([100, 101, 103, 106]):
        rows.append([
            i,
            str(close - 10),
            str(close + 1),
            str(close - 1),
            str(close),
            "10",
            i + 1,
            "1000",
            100,
            "7",
            "500",
            "0",
        ])
    features = _kline_features(rows)
    assert round(features["change_5m"], 4) == round((106 - 103) / 103 * 100, 4)


def test_kline_features_separate_current_wick_from_recent_max_wick():
    def row(i, open_price, high, low, close):
        return [
            i,
            str(open_price),
            str(high),
            str(low),
            str(close),
            "10",
            i + 1,
            "1000",
            100,
            "7",
            "500",
            "0",
        ]

    rows = [row(i, 100 + i * 0.1, 101 + i * 0.1, 99 + i * 0.1, 100.3 + i * 0.1) for i in range(20)]
    rows[-6] = row(14, 102, 121, 101, 102.5)
    rows[-1] = row(19, 104.8, 105.8, 104.3, 105.2)

    features = _kline_features(rows)
    metrics = features["structure_metrics"]

    assert metrics["max_wick_ratio_14"] >= 0.90
    assert metrics["current_wick_ratio"] < 0.41
    assert features["wick_ratio"] == metrics["max_wick_ratio_14"]


def test_market_classifier_does_not_block_clean_current_candle_for_old_wick():
    item = high_quality_item(symbol="OLDWICKUSDT", side="LONG", price=100)
    item.wick_ratio = 0.82
    item.score_features = {
        "structure_metrics": {
            "current_wick_ratio": 0.22,
            "max_wick_ratio_14": 0.82,
            "bars_since_max_wick": 8,
            "range_position": 0.52,
            "breakout_up": False,
            "breakout_down": False,
        }
    }

    setup = market_classifier.classify(item)

    assert setup["action"] == "OPEN_LONG"
    assert "wick_too_high" not in setup["no_trade_reasons"]
    assert "wick_noise_extreme" not in setup["no_trade_reasons"]


def test_market_classifier_only_treats_fast_move_as_chase_at_range_extreme():
    item = high_quality_item(symbol="MIDRANGEFASTUSDT", side="LONG", price=100)
    item.change_5m = 3.4
    item.atr_pct = 1.0
    item.score_features = {
        "structure_metrics": {
            "current_wick_ratio": 0.18,
            "max_wick_ratio_14": 0.24,
            "range_position": 0.50,
            "distance_to_resistance_pct": 2.4,
            "distance_to_support_pct": 2.4,
            "breakout_up": False,
            "breakout_down": False,
        }
    }

    setup = market_classifier.classify(item)

    assert setup["regime"] != "exhaustion"
    assert setup["action"] == "OPEN_LONG"
    assert "chase_displacement_high" not in setup["no_trade_reasons"]


def test_universal_anomaly_features_keep_symbol_as_categorical_context():
    left = high_quality_item(symbol="PEOPLEUSDT", side="LONG", price=100)
    right = high_quality_item(symbol="NEWCOINUSDT", side="LONG", price=100)
    metrics = {
        "current_wick_ratio": 0.18,
        "current_body_ratio": 0.72,
        "range_position": 0.58,
        "range_width_pct": 4.5,
        "distance_to_resistance_pct": 1.2,
        "distance_to_support_pct": 2.9,
        "breakout_up": True,
        "breakout_down": False,
    }
    left.score_features = {"structure_metrics": metrics}
    right.score_features = {"structure_metrics": dict(metrics)}

    left_features = universal_anomaly_model.extract_features(left)
    right_features = universal_anomaly_model.extract_features(right)

    assert left_features["symbol_key"] == "PEOPLEUSDT"
    assert right_features["symbol_key"] == "NEWCOINUSDT"
    assert "symbol" not in left_features
    assert "base_asset" not in left_features
    assert {k: v for k, v in left_features.items() if k != "symbol_key"} == {
        k: v for k, v in right_features.items() if k != "symbol_key"
    }


def test_universal_anomaly_model_outputs_direction_probabilities():
    item = high_quality_item(symbol="ZEROLAGUSDT", side="LONG", price=100)
    item.change_5m = 1.4
    item.change_15m = 2.0
    item.volume_spike = 3.2
    item.oi_change = 1.1
    item.taker_buy_ratio = 0.72
    item.taker_sell_ratio = 0.28
    item.depth_imbalance = 0.24
    item.score_features = {
        "structure_metrics": {
            "current_wick_ratio": 0.16,
            "current_body_ratio": 0.78,
            "range_position": 0.62,
            "breakout_up": True,
            "breakout_down": False,
        }
    }

    prediction = universal_anomaly_model.predict(item)

    assert prediction["model"].startswith("universal_anomaly")
    assert prediction["direction"] == "LONG"
    assert prediction["probabilities"]["LONG"] > prediction["probabilities"]["SHORT"]
    assert prediction["probabilities"]["LONG"] >= 0.58
    assert prediction["latency_budget_ms"] <= 1.0
    assert any(row.startswith("micro_direction_score=") for row in prediction["evidence"])


def test_universal_anomaly_training_row_uses_future_horizon_label():
    item = high_quality_item(symbol="LABELUSDT", side="SHORT", price=100)
    item.change_5m = -1.1
    item.taker_buy_ratio = 0.31
    item.taker_sell_ratio = 0.69
    item.depth_imbalance = -0.22
    item.score_features = {"structure_metrics": {"current_wick_ratio": 0.22, "current_body_ratio": 0.64}}

    row = universal_anomaly_model.training_row(item, future_return_pct=-0.84, horizon_minutes=5)

    assert row["label_direction"] == "SHORT"
    assert row["label_return_pct"] == -0.84
    assert row["horizon_minutes"] == 5
    assert row["features"]["symbol_key"] == "LABELUSDT"
    assert "symbol" not in row["features"]
    assert row["features"]["taker_imbalance"] < 0


def test_universal_anomaly_training_builder_labels_future_horizon_without_symbol_feature(tmp_path):
    database = DB(str(tmp_path / "universal_training.sqlite"))
    builder = UniversalAnomalyTrainingBuilder(database=database)
    source = high_quality_item(symbol="TRAINEDGEUSDT", side="LONG", price=100)
    source.ts_ms = 1_000_000
    source.score_features = {
        "structure_metrics": {
            "current_wick_ratio": 0.18,
            "current_body_ratio": 0.72,
            "range_position": 0.58,
            "breakout_up": True,
            "breakout_down": False,
        }
    }
    future = high_quality_item(symbol="TRAINEDGEUSDT", side="LONG", price=101.2)
    future.ts_ms = source.ts_ms + 5 * 60 * 1000 + 1_000
    database.save_radar_items("source_scan", [source.asdict()])
    database.save_radar_items("future_scan", [future.asdict()])

    report = builder.collect(horizon_minutes=5, limit=20)
    samples = database.list_universal_anomaly_samples(limit=10, horizon_minutes=5)

    assert report["created"] == 1
    assert len(samples) == 1
    assert samples[0]["symbol"] == "TRAINEDGEUSDT"
    assert samples[0]["label_direction"] == "LONG"
    assert round(samples[0]["label_return_pct"], 4) == 1.2
    assert samples[0]["label_horizon_minutes"] == 5
    assert samples[0]["features"]["symbol_key"] == "TRAINEDGEUSDT"
    assert "symbol" not in samples[0]["features"]
    assert "base_asset" not in samples[0]["features"]
    assert samples[0]["features"]["taker_imbalance"] > 0


def test_universal_anomaly_training_builder_waits_for_full_horizon(tmp_path):
    database = DB(str(tmp_path / "universal_training_wait.sqlite"))
    builder = UniversalAnomalyTrainingBuilder(database=database)
    source = high_quality_item(symbol="NOLEAKUSDT", side="SHORT", price=100)
    source.ts_ms = 2_000_000
    early_future = high_quality_item(symbol="NOLEAKUSDT", side="SHORT", price=98.0)
    early_future.ts_ms = source.ts_ms + 2 * 60 * 1000
    database.save_radar_items("source_scan", [source.asdict()])
    database.save_radar_items("early_scan", [early_future.asdict()])

    report = builder.collect(horizon_minutes=5, limit=20)

    assert report["created"] == 0
    assert report["missing_future"] == 1
    assert database.list_universal_anomaly_samples(limit=10, horizon_minutes=5) == []


def test_universal_anomaly_training_api_exposes_summary(monkeypatch):
    from backend.main import app

    class FakeUniversalTraining:
        def summary(self):
            return {"total": 2, "by_horizon": {"5": 2}, "by_label": {"LONG": 1, "SHORT": 1}}

        def recent_samples(self, *, limit=50, horizon_minutes=None):
            return [
                {
                    "symbol": "AUDITUSDT",
                    "label_direction": "LONG",
                    "label_horizon_minutes": horizon_minutes or 5,
                    "features": {"taker_imbalance": 0.4},
                }
            ][:limit]

    monkeypatch.setattr("backend.main.universal_anomaly_training", FakeUniversalTraining(), raising=False)
    client = TestClient(app)

    response = client.get("/api/radar/universal-anomaly/training?horizon_minutes=5&limit=10")

    assert response.status_code == 200
    data = response.json()
    assert data["ok"] is True
    assert data["summary"]["total"] == 2
    assert data["recent_samples"][0]["features"]["taker_imbalance"] == 0.4
    assert "symbol" not in data["recent_samples"][0]["features"]


def test_universal_anomaly_sample_calibrator_repairs_symbol_key_and_flags_bad_groups(tmp_path):
    from backend.radar.universal_anomaly_calibration import UniversalAnomalySampleCalibrator

    database = DB(str(tmp_path / "universal_calibration.sqlite"))
    missing_symbol_key = universal_training_sample("MISSKEYUSDT", "LONG", 1)
    missing_symbol_key["features"].pop("symbol_key")
    wrong_symbol_key = universal_training_sample("WRONGKEYUSDT", "SHORT", 2)
    wrong_symbol_key["features"]["symbol_key"] = "OTHERUSDT"
    missing_numeric = universal_training_sample("MISSFEATUREUSDT", "LONG", 3)
    missing_numeric["features"].pop("taker_imbalance")
    thin = universal_training_sample("THINUSDT", "LONG", 4)
    neutral_heavy = [universal_training_sample("FLATUSDT", "NEUTRAL", idx) for idx in range(10, 14)]
    long_heavy = [universal_training_sample("TRENDUSDT", "LONG", idx) for idx in range(20, 24)]
    samples = [
        missing_symbol_key,
        wrong_symbol_key,
        missing_numeric,
        thin,
        *neutral_heavy,
        *long_heavy,
    ]
    database.save_universal_anomaly_samples(samples)

    report = UniversalAnomalySampleCalibrator(database=database).calibrate(
        horizon_minutes=5,
        limit=100,
        repair=True,
        min_symbol_samples=3,
        neutral_rate_warn=0.75,
        dominance_warn=0.85,
    )
    repaired = {
        sample["sample_id"]: sample
        for sample in database.list_universal_anomaly_samples(limit=100, horizon_minutes=5)
    }

    assert report["ok"] is True
    assert report["total"] == len(samples)
    assert report["repaired_symbol_key"] == 2
    assert report["symbol_key_mismatch"] == 2
    assert report["feature_missing_counts"]["taker_imbalance"] == 1
    assert "sample_count_below_floor" in report["symbol_reports"]["THINUSDT"]["warnings"]
    assert "neutral_rate_high" in report["symbol_reports"]["FLATUSDT"]["warnings"]
    assert "label_dominance_high" in report["symbol_reports"]["TRENDUSDT"]["warnings"]
    assert repaired[missing_symbol_key["sample_id"]]["features"]["symbol_key"] == "MISSKEYUSDT"
    assert repaired[wrong_symbol_key["sample_id"]]["features"]["symbol_key"] == "WRONGKEYUSDT"


def test_universal_anomaly_sample_calibration_api_exposes_dry_run_and_repair(monkeypatch):
    from backend.main import app

    calls = []
    monkeypatch.setattr(settings, "api_token", "test-token")

    class FakeCalibrator:
        def calibrate(self, **kwargs):
            calls.append(kwargs)
            return {
                "ok": True,
                "repair": kwargs["repair"],
                "total": 12,
                "repaired_symbol_key": 2 if kwargs["repair"] else 0,
            }

    monkeypatch.setattr("backend.main.universal_anomaly_sample_calibrator", FakeCalibrator(), raising=False)
    client = TestClient(app)

    dry_run = client.get(
        "/api/radar/universal-anomaly/calibration"
        "?horizon_minutes=5&limit=100&min_symbol_samples=3&neutral_rate_warn=0.75&dominance_warn=0.85"
    )
    repaired = client.post(
        "/api/radar/universal-anomaly/calibration/repair"
        "?horizon_minutes=5&limit=100&min_symbol_samples=3&neutral_rate_warn=0.75&dominance_warn=0.85",
        headers={"X-API-Token": "test-token"},
    )

    assert dry_run.status_code == 200
    assert dry_run.json()["repair"] is False
    assert repaired.status_code == 200
    assert repaired.json()["repair"] is True
    assert repaired.json()["repaired_symbol_key"] == 2
    assert calls == [
        {
            "horizon_minutes": 5,
            "limit": 100,
            "repair": False,
            "min_symbol_samples": 3,
            "neutral_rate_warn": 0.75,
            "dominance_warn": 0.85,
        },
        {
            "horizon_minutes": 5,
            "limit": 100,
            "repair": True,
            "min_symbol_samples": 3,
            "neutral_rate_warn": 0.75,
            "dominance_warn": 0.85,
        },
    ]


def universal_training_sample(symbol, label, idx):
    direction = {"LONG": 1.0, "SHORT": -1.0, "NEUTRAL": 0.0}[label]
    impulse = direction * (1.0 + idx * 0.01)
    taker = direction * 0.42
    depth = direction * 0.24
    if label == "NEUTRAL":
        impulse = 0.02 * ((idx % 3) - 1)
        taker = 0.0
        depth = 0.0
    return {
        "sample_id": f"train:{symbol}:{label}:{idx}",
        "model": "test_source",
        "symbol": symbol,
        "source_scan_id": f"src_{idx}",
        "label_scan_id": f"lbl_{idx}",
        "source_ts_ms": 1_000_000 + idx,
        "label_ts_ms": 1_300_000 + idx,
        "source_price": 100.0,
        "label_price": 101.0 if label == "LONG" else (99.0 if label == "SHORT" else 100.01),
        "source_direction": label,
        "source_rank": idx,
        "source_score": 80.0,
        "features": {
            "change_5m": impulse,
            "change_15m": impulse * 1.4,
            "change_1h": impulse * 0.6,
            "volume_spike": 2.4 if label != "NEUTRAL" else 0.8,
            "oi_change": abs(impulse),
            "funding_rate": 0.0001,
            "taker_imbalance": taker,
            "depth_imbalance": depth,
            "atr_pct": 1.1,
            "current_wick_ratio": 0.18,
            "current_body_ratio": 0.74 if label != "NEUTRAL" else 0.25,
            "range_position": 0.64 if label == "LONG" else (0.36 if label == "SHORT" else 0.5),
            "range_width_pct": 4.0,
            "distance_to_resistance_pct": 1.0,
            "distance_to_support_pct": 2.0,
            "breakout_up": 1.0 if label == "LONG" else 0.0,
            "breakout_down": 1.0 if label == "SHORT" else 0.0,
            "btc_relative_5m": impulse * 0.2,
            "eth_relative_5m": impulse * 0.15,
            "symbol_key": symbol,
        },
        "label_return_pct": 1.0 if label == "LONG" else (-1.0 if label == "SHORT" else 0.01),
        "label_direction": label,
        "label_horizon_minutes": 5,
        "created_at": 2_000_000 + idx,
    }


def test_universal_anomaly_trainer_fits_mlp_and_writes_artifact(tmp_path):
    database = DB(str(tmp_path / "universal_model_train.sqlite"))
    samples = []
    for idx in range(12):
        samples.append(universal_training_sample("LONGTRAINUSDT", "LONG", idx))
        samples.append(universal_training_sample("SHORTTRAINUSDT", "SHORT", idx))
        samples.append(universal_training_sample("FLATTRAINUSDT", "NEUTRAL", idx))
    database.save_universal_anomaly_samples(samples)
    artifact_path = tmp_path / "universal_anomaly_mlp.joblib"
    trainer = UniversalAnomalyClassifierTrainer(database=database, artifact_path=artifact_path)

    report = trainer.train(horizon_minutes=5, model_type="mlp", min_samples=12, limit=200, activate=False)
    artifact = trainer.load_artifact()
    prediction = trainer.predict_features(samples[0]["features"], artifact=artifact)

    assert report["ok"] is True
    assert report["engine"] == "mlp"
    assert artifact_path.exists()
    assert report["sample_count"] == 36
    assert report["class_counts"]["LONG"] == 12
    assert "symbol" not in report["feature_names"]
    assert "base_asset" not in report["feature_names"]
    assert "symbol_key" in report["feature_names"]
    assert report["categorical_feature_names"] == ["symbol_key"]
    assert report["metrics"]["train_accuracy"] >= 0.80
    assert prediction["direction"] == "LONG"
    assert prediction["probabilities"]["LONG"] > prediction["probabilities"]["SHORT"]


def test_universal_anomaly_trainer_fits_lightgbm_when_available(tmp_path):
    pytest.importorskip("lightgbm")
    database = DB(str(tmp_path / "universal_model_lgbm.sqlite"))
    samples = []
    for idx in range(12):
        samples.append(universal_training_sample("LONGTRAINUSDT", "LONG", idx))
        samples.append(universal_training_sample("SHORTTRAINUSDT", "SHORT", idx))
        samples.append(universal_training_sample("FLATTRAINUSDT", "NEUTRAL", idx))
    database.save_universal_anomaly_samples(samples)
    trainer = UniversalAnomalyClassifierTrainer(database=database, artifact_path=tmp_path / "universal_anomaly_lgbm.joblib")

    report = trainer.train(horizon_minutes=5, model_type="lightgbm", min_samples=12, limit=200, activate=False)
    prediction = trainer.predict_features(samples[0]["features"], artifact=trainer.load_artifact())

    assert report["ok"] is True
    assert report["engine"] == "lightgbm"
    assert report["model"] == "universal_anomaly_lightgbm_v1"
    assert report["sample_count"] == 36
    assert "symbol" not in report["feature_names"]
    assert "symbol_key" in report["feature_names"]
    assert report["categorical_feature_names"] == ["symbol_key"]
    assert prediction["direction"] == "LONG"
    assert prediction["probabilities"]["LONG"] > prediction["probabilities"]["SHORT"]


def test_universal_anomaly_trainer_validates_on_later_time_window(tmp_path, monkeypatch):
    import joblib

    monkeypatch.setattr(joblib, "dump", lambda *args, **kwargs: None)
    database = DB(str(tmp_path / "chronological_train.sqlite"))
    labels = ["LONG", "SHORT", "NEUTRAL"] * 8
    samples = []
    for idx, label in enumerate(labels):
        sample = universal_training_sample(f"CHRONO{idx}USDT", label, idx)
        sample["source_ts_ms"] = 1_000_000 + idx
        sample["label_ts_ms"] = 1_300_000 + idx
        sample["created_at"] = 2_000_000 + idx
        sample["features"]["change_5m"] = float(idx)
        samples.append(sample)
    database.save_universal_anomaly_samples(samples)

    class RecordingEstimator:
        def __init__(self):
            self.fit_batches = []
            self.score_batches = []
            self.classes_ = []

        def fit(self, x, y):
            self.fit_batches.append([row["change_5m"] for row in x])
            self.classes_ = sorted(set(y))
            return self

        def score(self, x, y):
            self.score_batches.append([row["change_5m"] for row in x])
            return 0.75

    class RecordingTrainer(UniversalAnomalyClassifierTrainer):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self.estimator = RecordingEstimator()

        def _select_engine(self, model_type):
            return "recording"

        def _build_estimator(self, engine):
            return self.estimator

    trainer = RecordingTrainer(database=database, artifact_path=tmp_path / "chrono.joblib")
    report = trainer.train(horizon_minutes=5, model_type="recording", min_samples=12, limit=100, activate=False)

    first_train_window = trainer.estimator.fit_batches[0]
    validation_window = trainer.estimator.score_batches[1]
    assert report["metrics"]["validation_split"] == "chronological_tail"
    assert max(first_train_window) < min(validation_window)


def test_universal_anomaly_model_uses_activated_trained_artifact(tmp_path):
    database = DB(str(tmp_path / "universal_model_activate.sqlite"))
    samples = []
    for idx in range(12):
        samples.append(universal_training_sample("LONGTRAINUSDT", "LONG", idx))
        samples.append(universal_training_sample("SHORTTRAINUSDT", "SHORT", idx))
        samples.append(universal_training_sample("FLATTRAINUSDT", "NEUTRAL", idx))
    database.save_universal_anomaly_samples(samples)
    trainer = UniversalAnomalyClassifierTrainer(database=database, artifact_path=tmp_path / "model.joblib")
    report = trainer.train(horizon_minutes=5, model_type="mlp", min_samples=12, limit=200, activate=False)
    model = UniversalAnomalyModel()
    model.activate_trained_artifact(report["artifact"])
    item = high_quality_item(symbol="LIVEPREDICTUSDT", side="LONG", price=100)
    item.change_5m = 1.2
    item.change_15m = 1.7
    item.taker_buy_ratio = 0.71
    item.taker_sell_ratio = 0.29
    item.depth_imbalance = 0.23
    item.score_features = {
        "structure_metrics": {
            "current_wick_ratio": 0.18,
            "current_body_ratio": 0.74,
            "range_position": 0.64,
            "breakout_up": True,
            "breakout_down": False,
        }
    }

    prediction = model.predict(item)

    assert prediction["model"].startswith("universal_anomaly_mlp")
    assert prediction["direction"] == "LONG"
    assert prediction["probabilities"]["LONG"] > prediction["probabilities"]["SHORT"]
    assert any(row == "trained_model_engine=mlp" for row in prediction["evidence"])


def test_universal_anomaly_model_suppresses_feature_name_warning():
    warnings_mod = __import__("warnings")

    class WarningEstimator:
        classes_ = ["LONG", "SHORT", "NEUTRAL"]

        def predict_proba(self, vector):
            warnings_mod.warn(
                "X does not have valid feature names, but LGBMClassifier was fitted with feature names",
                UserWarning,
            )
            return [[0.7, 0.2, 0.1]]

    model = UniversalAnomalyModel()
    model.activate_trained_artifact(
        {
            "estimator": WarningEstimator(),
            "feature_names": ["change_5m", "change_15m"],
            "classes": ["LONG", "SHORT", "NEUTRAL"],
            "engine": "lightgbm",
            "model_name": "universal_anomaly_lightgbm_v1",
        }
    )
    item = high_quality_item(symbol="WARNLESSUSDT", side="LONG", price=100)

    with warnings_mod.catch_warnings(record=True) as caught:
        warnings_mod.simplefilter("always")
        prediction = model.predict(item)

    assert prediction["direction"] == "LONG"
    assert not any("valid feature names" in str(row.message) for row in caught)


def test_universal_anomaly_model_train_api_invokes_trainer(monkeypatch):
    from backend.main import app

    calls = []
    monkeypatch.setattr(settings, "api_token", "test-token")

    class FakeTrainer:
        def status(self):
            return {"artifact_exists": True, "runtime": {"active": True, "engine": "mlp"}}

        def train(self, **kwargs):
            calls.append(kwargs)
            return {"ok": True, "engine": "mlp", "sample_count": 123, "activated": True, "artifact": object()}

    monkeypatch.setattr("backend.main.universal_anomaly_trainer", FakeTrainer(), raising=False)
    client = TestClient(app)

    status = client.get("/api/radar/universal-anomaly/model")
    trained = client.post(
        "/api/radar/universal-anomaly/model/train?horizon_minutes=5&model_type=mlp&min_samples=20&limit=200",
        headers={"X-API-Token": "test-token"},
    )

    assert status.status_code == 200
    assert status.json()["runtime"]["active"] is True
    assert trained.status_code == 200
    assert trained.json()["ok"] is True
    assert trained.json()["engine"] == "mlp"
    assert "artifact" not in trained.json()
    assert calls == [{"horizon_minutes": 5, "model_type": "mlp", "min_samples": 20, "limit": 200, "activate": True}]


def test_universal_anomaly_model_status_includes_auto_trainer(monkeypatch):
    from backend.main import app

    class FakeTrainer:
        def status(self):
            return {"runtime": {"active": True, "engine": "lightgbm"}}

    class FakeAutoTrainer:
        def status(self):
            return {"state": {"last_collect_ms": 123}, "last_result": {"ok": True}}

    monkeypatch.setattr("backend.main.universal_anomaly_trainer", FakeTrainer(), raising=False)
    monkeypatch.setattr("backend.main.universal_anomaly_auto_trainer", FakeAutoTrainer(), raising=False)
    client = TestClient(app)

    response = client.get("/api/radar/universal-anomaly/model")

    assert response.status_code == 200
    assert response.json()["runtime"]["engine"] == "lightgbm"
    assert response.json()["auto_trainer"]["state"]["last_collect_ms"] == 123


def test_universal_anomaly_trainer_status_exposes_symbol_feature_metadata(tmp_path):
    database = DB(str(tmp_path / "universal_model_status.sqlite"))
    samples = []
    for idx in range(12):
        samples.append(universal_training_sample("LONGSTATUSUSDT", "LONG", idx))
        samples.append(universal_training_sample("SHORTSTATUSUSDT", "SHORT", idx))
        samples.append(universal_training_sample("FLATSTATUSUSDT", "NEUTRAL", idx))
    database.save_universal_anomaly_samples(samples)
    trainer = UniversalAnomalyClassifierTrainer(database=database, artifact_path=tmp_path / "status.joblib")
    trainer.train(horizon_minutes=5, model_type="mlp", min_samples=12, limit=200, activate=False)

    status = trainer.status()

    assert "symbol_key" in status["artifact"]["feature_names"]
    assert status["artifact"]["categorical_feature_names"] == ["symbol_key"]
    assert status["artifact"]["symbol_category_count"] >= 3


def test_universal_anomaly_auto_trainer_defaults_to_recent_training_window():
    cfg = UniversalAnomalyAutoTrainer()._config()

    assert cfg["train_limit"] == 2500
    assert cfg["max_samples"] >= cfg["train_limit"]


def test_universal_anomaly_auto_trainer_trains_lightgbm_when_gate_passes(tmp_path):
    database = DB(str(tmp_path / "auto_train.sqlite"))
    samples = []
    for idx in range(12):
        samples.append(universal_training_sample("LONGAUTOUSDT", "LONG", idx))
        samples.append(universal_training_sample("SHORTAUTOUSDT", "SHORT", idx))
        samples.append(universal_training_sample("FLATAUTOUSDT", "NEUTRAL", idx))
    database.save_universal_anomaly_samples(samples)
    trainer = UniversalAnomalyClassifierTrainer(database=database, artifact_path=tmp_path / "auto_lgbm.joblib")

    class ExistingTraining:
        def collect(self, *, horizon_minutes=5, limit=500):
            return {"ok": True, "created": 0, "summary": database.universal_anomaly_sample_summary()}

        def summary(self):
            return database.universal_anomaly_sample_summary()

    auto = UniversalAnomalyAutoTrainer(training=ExistingTraining(), trainer=trainer)

    result = auto.step(
        now_ms_value=1_000_000,
        enabled=True,
        collect_interval_seconds=0,
        train_interval_seconds=0,
        horizon_minutes=5,
        collect_limit=100,
        train_limit=200,
        model_type="lightgbm",
        min_samples=12,
        min_class_samples=4,
        min_new_samples=0,
        min_validation_accuracy=0.1,
        min_accuracy_delta=0.0,
    )

    assert result["collected"] is True
    assert result["trained"] is True
    assert result["accepted"] is True
    assert result["train_report"]["engine"] == "lightgbm"
    assert trainer.status()["runtime"]["engine"] == "lightgbm"


def test_universal_anomaly_auto_trainer_repairs_samples_before_training(tmp_path):
    database = DB(str(tmp_path / "auto_train_calibration.sqlite"))
    samples = []
    for idx in range(12):
        samples.append(universal_training_sample("LONGREPAIRUSDT", "LONG", idx))
        samples.append(universal_training_sample("SHORTREPAIRUSDT", "SHORT", idx))
        samples.append(universal_training_sample("FLATREPAIRUSDT", "NEUTRAL", idx))
    samples[0]["features"].pop("symbol_key")
    database.save_universal_anomaly_samples(samples)
    trainer = UniversalAnomalyClassifierTrainer(database=database, artifact_path=tmp_path / "auto_repair.joblib")

    class ExistingTraining:
        def collect(self, *, horizon_minutes=5, limit=500):
            return {"ok": True, "created": 0, "summary": database.universal_anomaly_sample_summary()}

        def summary(self):
            return database.universal_anomaly_sample_summary()

    class RecordingCalibrator:
        def __init__(self):
            self.calls = []

        def calibrate(self, **kwargs):
            self.calls.append(kwargs)
            return {"ok": True, "total": len(samples), "repaired_symbol_key": 1, "warnings": ["symbol_key_mismatch"]}

    calibrator = RecordingCalibrator()
    auto = UniversalAnomalyAutoTrainer(training=ExistingTraining(), trainer=trainer, calibrator=calibrator)

    result = auto.step(
        now_ms_value=1_000_000,
        enabled=True,
        collect_interval_seconds=0,
        train_interval_seconds=0,
        horizon_minutes=5,
        collect_limit=100,
        train_limit=200,
        model_type="mlp",
        min_samples=12,
        min_class_samples=4,
        min_new_samples=0,
        min_validation_accuracy=0.1,
        min_accuracy_delta=0.0,
    )

    assert result["trained"] is True
    assert result["calibration_report"]["repaired_symbol_key"] == 1
    assert calibrator.calls == [{"horizon_minutes": 5, "limit": 200, "repair": True}]


def test_universal_anomaly_auto_trainer_rejects_degrading_candidate(tmp_path):
    class RuntimeModel:
        def __init__(self):
            self.activated = False

        def activate_trained_artifact(self, artifact):
            self.activated = True
            return {"active": True}

        def trained_model_status(self):
            return {"active": True, "engine": "lightgbm", "sample_count": 200, "metrics": {"validation_accuracy": 0.80}}

    class FakeTrainer:
        def __init__(self):
            self.artifact_path = tmp_path / "reject.joblib"
            self.runtime_model = RuntimeModel()

        def status(self):
            return {"runtime": self.runtime_model.trained_model_status(), "artifact_exists": True}

        def train(self, **kwargs):
            return {
                "ok": True,
                "engine": "lightgbm",
                "artifact": {"estimator": object(), "feature_names": ["x"]},
                "sample_count": 300,
                "metrics": {"validation_accuracy": 0.55},
            }

    class FakeTraining:
        def collect(self, **kwargs):
            return {"ok": True, "created": 120, "summary": self.summary()}

        def summary(self):
            return {"total": 300, "by_label": {"LONG": 100, "SHORT": 100, "NEUTRAL": 100}}

    trainer = FakeTrainer()
    auto = UniversalAnomalyAutoTrainer(training=FakeTraining(), trainer=trainer)

    result = auto.step(
        now_ms_value=1_000_000,
        enabled=True,
        collect_interval_seconds=0,
        train_interval_seconds=0,
        min_samples=100,
        min_class_samples=20,
        min_new_samples=50,
        min_validation_accuracy=0.50,
        min_accuracy_delta=0.0,
    )

    assert result["trained"] is True
    assert result["accepted"] is False
    assert result["reject_reason"] == "validation_accuracy_below_current"
    assert trainer.runtime_model.activated is False


def test_universal_anomaly_auto_trainer_skips_when_new_samples_are_insufficient(tmp_path):
    class RuntimeModel:
        def trained_model_status(self):
            return {"active": True, "engine": "lightgbm", "sample_count": 200, "metrics": {"validation_accuracy": 0.70}}

    class FakeTrainer:
        artifact_path = tmp_path / "skip.joblib"
        runtime_model = RuntimeModel()

        def status(self):
            return {"runtime": self.runtime_model.trained_model_status(), "artifact_exists": True}

        def train(self, **kwargs):
            raise AssertionError("train should not run")

    class FakeTraining:
        def collect(self, **kwargs):
            return {"ok": True, "created": 40, "summary": self.summary()}

        def summary(self):
            return {"total": 240, "by_label": {"LONG": 80, "SHORT": 80, "NEUTRAL": 80}}

    auto = UniversalAnomalyAutoTrainer(training=FakeTraining(), trainer=FakeTrainer())

    result = auto.step(
        now_ms_value=1_000_000,
        enabled=True,
        collect_interval_seconds=0,
        train_interval_seconds=0,
        min_samples=100,
        min_class_samples=20,
        min_new_samples=100,
    )

    assert result["trained"] is False
    assert result["train_skip_reason"] == "not_enough_new_samples"


def test_universal_anomaly_auto_trainer_loop_catches_errors_and_continues():
    class FlakyAutoTrainer(UniversalAnomalyAutoTrainer):
        def __init__(self):
            super().__init__(training=object(), trainer=object())
            self.calls = 0

        def step(self, **kwargs):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("broken samples")
            return {"ok": True, "collected": True, "trained": False}

    auto = FlakyAutoTrainer()

    auto.run_loop(sleep_seconds=0, max_iterations=2)

    assert auto.calls == 2
    assert auto.last_result["ok"] is True


def test_universal_anomaly_db_prunes_old_and_caps_sample_window(tmp_path):
    database = DB(str(tmp_path / "prune_samples.sqlite"))
    samples = []
    for idx in range(10):
        sample = universal_training_sample(f"PRUNE{idx}USDT", ["LONG", "SHORT", "NEUTRAL"][idx % 3], idx)
        sample["sample_id"] = f"prune:{idx}"
        sample["source_ts_ms"] = 1_000_000 + idx
        sample["label_ts_ms"] = 1_300_000 + idx
        sample["created_at"] = 10_000 + idx
        sample["features"]["change_5m"] = float(idx)
        samples.append(sample)
    database.save_universal_anomaly_samples(samples)

    report = database.prune_universal_anomaly_samples(
        max_samples=5,
        retention_days=999,
        now_ms_value=20_000,
    )
    remaining = database.list_universal_anomaly_samples(limit=20, horizon_minutes=5)
    remaining_ids = {row["sample_id"] for row in remaining}

    assert report["remaining"] == 5
    assert len(remaining) == 5
    assert remaining_ids == {f"prune:{idx}" for idx in range(5, 10)}


def test_radar_trade_top5_ranks_confirmed_candidates_across_top50():
    engine = RadarEngine()
    weak = high_quality_item(symbol="WEAKUSDT", side="LONG")
    weak.fund_confirm_count = 2
    weak.rank = 1
    weak.score = 99
    confirmed = high_quality_item(symbol="CONFIRMUSDT", side="SHORT")
    confirmed.fund_confirm_count = 3
    confirmed.rank = 2
    confirmed.score = 80
    neutral = high_quality_item(symbol="NEUTRALUSDT", side="LONG")
    neutral.direction = "NEUTRAL"
    neutral.fund_confirm_count = 3
    outside_top5 = high_quality_item(symbol="RANK6CONFIRMUSDT", side="LONG")
    outside_top5.fund_confirm_count = 3
    outside_top5.rank = 6
    filler = [high_quality_item(symbol=f"FILL{i}USDT", side="LONG") for i in range(3)]
    for idx, item in enumerate(filler, start=3):
        item.rank = idx
        item.fund_confirm_count = 2
    top = engine.select_confirmed_top5([weak, confirmed, neutral, *filler, outside_top5])
    symbols = [item.symbol for item in top]
    assert symbols[:2] == ["CONFIRMUSDT", "RANK6CONFIRMUSDT"]
    assert "NEUTRALUSDT" not in symbols
    assert all(item.direction in {"LONG", "SHORT"} for item in top)


def test_radar_major_symbols_are_context_only_for_trade_candidates(monkeypatch):
    monkeypatch.setattr(settings, "radar_exclude_major_symbols_from_anomaly", True)
    monkeypatch.setattr(settings, "radar_major_symbols", "BTCUSDT,ETHUSDT,BNBUSDT,SOLUSDT")
    monkeypatch.setattr(
        "backend.radar.radar_engine.candidate_feature_enhancer.evaluate",
        stable_candidate_feature_report,
    )
    engine = RadarEngine()
    btc = high_quality_item(symbol="BTCUSDT", side="LONG")
    btc.fund_confirm_count = 3
    btc.score = 99
    alt = high_quality_item(symbol="ALTQUALITYUSDT", side="LONG")
    alt.fund_confirm_count = 3
    alt.score = 80

    top = engine.select_confirmed_top5([btc, alt])
    strict = engine.select_ai_candidates([btc, alt])
    review = engine.select_ai_review_candidates([btc, alt])
    diagnostics = engine.production_candidate_diagnostics([btc, alt])

    assert [item.symbol for item in top] == ["ALTQUALITYUSDT"]
    assert [item.symbol for item in strict] == ["ALTQUALITYUSDT"]
    assert [item.symbol for item in review] == ["ALTQUALITYUSDT"]
    btc_diag = diagnostics["top_checked"][0]
    assert btc_diag["symbol"] == "BTCUSDT"
    assert "major_symbol_context_only" in btc_diag["failed"]
    assert diagnostics["policy"]["major_symbols_context_only"] is True


def test_radar_scan_excludes_major_symbols_from_anomaly_list(monkeypatch):
    monkeypatch.setattr(settings, "radar_exclude_major_symbols_from_anomaly", True)
    monkeypatch.setattr(settings, "radar_major_symbols", "BTCUSDT,ETHUSDT,BNBUSDT,SOLUSDT")
    monkeypatch.setattr(settings, "radar_require_short_term_anomaly", False)
    monkeypatch.setattr(radar_weight_calibrator, "report", lambda: {})
    monkeypatch.setattr("backend.radar.radar_engine.db.save_radar_items", lambda *args, **kwargs: None)
    engine = RadarEngine()

    async def fake_snapshots(force_refresh=False):
        return [
            MarketSnapshot("BTCUSDT", 64000, 0.2, 0.3, 0.5, 5.0, 0.2, 0, 0.58, 0.42, 0.2, 0.3, 0.4),
            MarketSnapshot("ALTQUALITYUSDT", 1.2, 3.0, 6.0, 8.0, 4.0, 1.2, 0, 0.7, 0.3, 0.35, 2.5, 0.35),
        ]

    monkeypatch.setattr(market_service, "get_snapshots", fake_snapshots)

    items = __import__("asyncio").run(engine.scan(force_refresh=True))

    assert [item.symbol for item in items] == ["ALTQUALITYUSDT"]
    assert all(item.symbol not in {"BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT"} for item in engine.top50)


def test_radar_scan_excludes_extended_mainstream_context_symbols(monkeypatch):
    monkeypatch.setattr(settings, "radar_exclude_major_symbols_from_anomaly", True)
    monkeypatch.setattr(settings, "radar_major_symbols", "BTCUSDT,ETHUSDT,XRPUSDT,ZECUSDT,WLDUSDT,XAUUSDT")
    monkeypatch.setattr(settings, "radar_require_short_term_anomaly", False)
    monkeypatch.setattr(radar_weight_calibrator, "report", lambda: {})
    monkeypatch.setattr("backend.radar.radar_engine.db.save_radar_items", lambda *args, **kwargs: None)
    engine = RadarEngine()

    async def fake_snapshots(force_refresh=False):
        return [
            MarketSnapshot("XRPUSDT", 2.0, 1.2, 1.8, 2.5, 4.0, 1.0, 0, 0.7, 0.3, 0.3, 1.2, 0.3),
            MarketSnapshot("ZECUSDT", 450, 1.1, 1.4, 2.2, 4.0, 1.0, 0, 0.7, 0.3, 0.3, 1.2, 0.3),
            MarketSnapshot("MICROALTUSDT", 1.2, 1.1, 2.2, 3.0, 4.0, 1.0, 0, 0.7, 0.3, 0.3, 1.2, 0.3),
        ]

    monkeypatch.setattr(market_service, "get_snapshots", fake_snapshots)

    items = __import__("asyncio").run(engine.scan(force_refresh=True))

    assert [item.symbol for item in items] == ["MICROALTUSDT"]


def test_radar_scan_keeps_candidates_and_marks_short_term_anomaly(monkeypatch):
    monkeypatch.setattr(settings, "radar_exclude_major_symbols_from_anomaly", False)
    monkeypatch.setattr(settings, "radar_require_short_term_anomaly", True)
    monkeypatch.setattr(settings, "radar_anomaly_min_change_5m", 0.35)
    monkeypatch.setattr(settings, "radar_anomaly_min_change_15m", 0.75)
    monkeypatch.setattr(settings, "radar_anomaly_min_change_1h", 2.0)
    monkeypatch.setattr(radar_weight_calibrator, "report", lambda: {})
    monkeypatch.setattr("backend.radar.radar_engine.db.save_radar_items", lambda *args, **kwargs: None)
    engine = RadarEngine()

    async def fake_snapshots(force_refresh=False):
        return [
            MarketSnapshot("STABLEFLOWUSDT", 1.0, 0.05, 0.2, 0.6, 5.0, 2.0, 0, 0.7, 0.3, 0.35, 1.4, 0.2),
            MarketSnapshot("FASTMOVEUSDT", 1.0, 0.42, 0.4, 0.6, 1.0, 0.2, 0, 0.6, 0.4, 0.1, 0.8, 0.2),
            MarketSnapshot("MIDMOVEUSDT", 1.0, 0.1, -0.9, -1.2, 1.0, 0.2, 0, 0.4, 0.6, -0.1, 0.8, 0.2),
        ]

    monkeypatch.setattr(market_service, "get_snapshots", fake_snapshots)

    items = __import__("asyncio").run(engine.scan(force_refresh=True))
    symbols = [item.symbol for item in items]

    assert set(symbols) == {"STABLEFLOWUSDT", "MIDMOVEUSDT", "FASTMOVEUSDT"}
    stable = next(item for item in items if item.symbol == "STABLEFLOWUSDT")
    fast = next(item for item in items if item.symbol == "FASTMOVEUSDT")
    assert stable.score_features["short_term_anomaly"] is False
    assert fast.score_features["short_term_anomaly"] is True

def test_active_coin_registry_refreshes_expires_and_cools_down():
    registry = ActiveCoinRegistry(idle_seconds=180, cooldown_seconds=120, max_symbols=5)

    registry.update_candidates(["PEOPLEUSDT"], now=100.0, reason_by_symbol={"PEOPLEUSDT": "ticker_24h_move"})
    registry.update_candidates(["PEOPLEUSDT"], now=220.0, reason_by_symbol={"PEOPLEUSDT": "ticker_short_move"})
    expired = registry.expire_idle(now=401.0)
    skipped = registry.update_candidates(["PEOPLEUSDT"], now=450.0)

    assert [coin.symbol for coin in expired] == ["PEOPLEUSDT"]
    assert skipped == []
    assert registry.active_symbols() == []
    diag = registry.diagnostics(now=450.0)
    assert diag["active_count"] == 0
    assert diag["cooldown_count"] == 1
    assert diag["recent_removed"][0]["reason"] == "idle_timeout"


def test_active_coin_registry_replaces_lowest_priority_when_full():
    registry = ActiveCoinRegistry(idle_seconds=180, cooldown_seconds=120, max_symbols=2)

    registry.update_candidates(
        ["LOW1USDT", "LOW2USDT"],
        now=100.0,
        score_by_symbol={"LOW1USDT": 1.0, "LOW2USDT": 2.0},
    )
    registry.update_candidates(
        ["HIGHUSDT"],
        now=101.0,
        score_by_symbol={"HIGHUSDT": 9.0},
    )

    assert registry.active_symbols() == ["HIGHUSDT", "LOW2USDT"]
    diag = registry.diagnostics(now=101.0)
    assert diag["recent_removed"][0]["symbol"] == "LOW1USDT"
    assert diag["recent_removed"][0]["reason"] == "capacity_replace"


def test_dynamic_symbol_stream_syncs_subscriptions_without_static_symbol_list():
    stream = DynamicSymbolStream(streams=("aggTrade", "depth20@100ms", "kline_1m"))

    first = stream.sync(["PEOPLEUSDT", "TRBUSDT"], now=100.0)
    second = stream.sync(["TRBUSDT"], now=130.0)

    assert first["subscribed"] == ["PEOPLEUSDT", "TRBUSDT"]
    assert second["unsubscribed"] == ["PEOPLEUSDT"]
    diag = stream.diagnostics()
    assert diag["active_symbols"] == ["TRBUSDT"]
    assert diag["active_count"] == 1
    assert diag["streams"] == ["aggTrade", "depth20@100ms", "kline_1m"]


def test_dynamic_symbol_stream_clears_old_error_after_valid_message():
    stream = DynamicSymbolStream(streams=("bookTicker",))
    stream._last_error = "ConnectionClosedError:no close frame received or sent"

    stream._remember_message('{"stream":"btcusdt@bookTicker","data":{"s":"BTCUSDT","b":"62700","a":"62701"}}')

    diag = stream.diagnostics()
    assert diag["last_error"] == ""
    assert diag["last_message_age_seconds"] is not None
    assert stream.latest("BTCUSDT")["bookTicker"]["s"] == "BTCUSDT"


def test_binance_ticker_ws_url_uses_market_routed_futures_path(monkeypatch):
    from backend.market.binance_ws_ticker import BinanceTickerStream

    monkeypatch.setattr(settings, "binance_testnet", False)
    monkeypatch.setattr(settings, "binance_ws_url", "")

    ticker_stream = BinanceTickerStream()

    assert ticker_stream._url() == "wss://fstream.binance.com/market/ws/!ticker@arr"


def test_binance_ticker_ws_filters_coin_m_rows_after_cm_migration():
    from backend.market.binance_ws_ticker import BinanceTickerStream

    ticker_stream = BinanceTickerStream()
    ticker_stream._updated_at = __import__("time").monotonic()
    ticker_stream._tickers = {
        "BTCUSDT": {"s": "BTCUSDT", "c": "60000", "q": "1000000", "P": "1.2", "st": 1},
        "BTCUSD_PERP": {"s": "BTCUSD_PERP", "c": "60000", "q": "1000000", "P": "1.2", "st": 2},
        "ETHUSDT": {"s": "ETHUSDT", "c": "3000", "q": "800000", "P": "0.8"},
    }

    rows = ticker_stream.snapshot_rows()

    assert [row["symbol"] for row in rows] == ["BTCUSDT", "ETHUSDT"]


def test_binance_ws_custom_url_override_is_preserved(monkeypatch):
    from backend.market.binance_ws_ticker import BinanceTickerStream

    monkeypatch.setattr(settings, "binance_ws_url", "wss://example.invalid/custom")

    assert BinanceTickerStream()._url() == "wss://example.invalid/custom"
    assert DynamicSymbolStream()._url(["BTCUSDT"]) == "wss://example.invalid/custom"


def test_binance_factor_source_prioritizes_active_ticker_anomalies(monkeypatch):
    monkeypatch.setattr(settings, "binance_symbol_limit", 1)
    monkeypatch.setattr(settings, "binance_factor_concurrency", 1)
    monkeypatch.setattr(settings, "binance_factor_ttl_seconds", 0)
    monkeypatch.setattr(settings, "binance_use_open_interest_hist", False)
    monkeypatch.setattr(settings, "binance_use_taker_ratio_endpoint", False)
    monkeypatch.setattr(settings, "radar_active_min_quote_volume", 100.0)
    monkeypatch.setattr(settings, "radar_active_min_change_24h", 5.0)
    monkeypatch.setattr(settings, "radar_active_min_short_change_pct", 0.35)
    monkeypatch.setattr(settings, "radar_exclude_major_symbols_from_anomaly", False)
    monkeypatch.setattr("backend.market.binance_factor_source.binance_ticker_stream.snapshot_rows", lambda: [])

    class ActiveCandidateClient(FakeBinanceMarketClient):
        async def exchange_info(self):
            return {
                "symbols": [
                    {
                        "symbol": "QUIETUSDT",
                        "status": "TRADING",
                        "contractType": "PERPETUAL",
                        "quoteAsset": "USDT",
                        "marginAsset": "USDT",
                        "underlyingType": "COIN",
                        "underlyingSubType": ["Layer-1"],
                    },
                    {
                        "symbol": "HOTUSDT",
                        "status": "TRADING",
                        "contractType": "PERPETUAL",
                        "quoteAsset": "USDT",
                        "marginAsset": "USDT",
                        "underlyingType": "COIN",
                        "underlyingSubType": ["Meme"],
                    },
                ]
            }

        async def premium_index(self):
            return [
                {"symbol": "QUIETUSDT", "markPrice": "10", "lastFundingRate": "0.0001"},
                {"symbol": "HOTUSDT", "markPrice": "1.2", "lastFundingRate": "0.0001"},
            ]

        async def ticker_24hr(self, symbol=None):
            return [
                {"symbol": "QUIETUSDT", "lastPrice": "10", "quoteVolume": "9000000", "priceChangePercent": "0.1"},
                {"symbol": "HOTUSDT", "lastPrice": "1.2", "quoteVolume": "1000", "priceChangePercent": "9.0"},
            ]

        async def klines(self, symbol, interval="5m", limit=30):
            rows = await super().klines("BTCUSDT", interval, limit)
            return rows

    source = BinanceFactorSource(client=ActiveCandidateClient())
    snaps = __import__("asyncio").run(source.get_snapshots(force_refresh=True))

    assert [snap.symbol for snap in snaps] == ["HOTUSDT"]
    assert active_coin_registry.active_symbols() == ["HOTUSDT"]
    assert dynamic_symbol_stream.diagnostics()["active_symbols"] == ["HOTUSDT"]


def test_binance_factor_source_ranks_active_ticker_candidates_before_capacity(monkeypatch):
    monkeypatch.setattr(active_coin_registry, "max_symbols", 2)
    monkeypatch.setattr(settings, "radar_active_coin_max_symbols", 2)
    monkeypatch.setattr(settings, "radar_active_min_quote_volume", 500000.0)
    monkeypatch.setattr(settings, "radar_active_min_change_24h", 2.5)
    monkeypatch.setattr(settings, "radar_active_min_short_change_pct", 999.0)
    monkeypatch.setattr(settings, "radar_exclude_major_symbols_from_anomaly", False)
    source = BinanceFactorSource(client=FakeBinanceMarketClient())
    source._exchange_symbol_meta = {
        symbol: _exchange_symbol_meta(symbol)
        for symbol in ["LOW1USDT", "LOW2USDT", "HIGH1USDT", "HIGH2USDT"]
    }
    premiums = {symbol: {"markPrice": "1"} for symbol in source._exchange_symbol_meta}
    tickers = {
        "LOW1USDT": {"lastPrice": "1", "quoteVolume": "600000", "priceChangePercent": "3"},
        "LOW2USDT": {"lastPrice": "1", "quoteVolume": "700000", "priceChangePercent": "4"},
        "HIGH1USDT": {"lastPrice": "1", "quoteVolume": "100000000", "priceChangePercent": "20"},
        "HIGH2USDT": {"lastPrice": "1", "quoteVolume": "50000000", "priceChangePercent": "15"},
    }

    active = source._discover_active_candidates(premiums, tickers)

    assert active == ["HIGH1USDT", "HIGH2USDT"]
    assert dynamic_symbol_stream.diagnostics()["active_symbols"] == ["HIGH1USDT", "HIGH2USDT"]


def test_binance_factor_source_excludes_localized_contract_symbols(monkeypatch):
    monkeypatch.setattr(settings, "radar_exclude_major_symbols_from_anomaly", False)
    monkeypatch.setattr(settings, "binance_symbol_limit", 3)
    source = BinanceFactorSource(client=FakeBinanceMarketClient())
    source._exchange_symbol_meta = {
        "BTCUSDT": _exchange_symbol_meta("BTCUSDT"),
        "龙虾USDT": _exchange_symbol_meta("龙虾USDT"),
    }
    premiums = {
        "BTCUSDT": {"markPrice": "100"},
        "龙虾USDT": {"markPrice": "0.01"},
    }
    tickers = {
        "BTCUSDT": {"lastPrice": "100", "quoteVolume": "1000000", "priceChangePercent": "0.5"},
        "龙虾USDT": {"lastPrice": "0.01", "quoteVolume": "9000000", "priceChangePercent": "25"},
    }

    selected = source._select_symbols(premiums, tickers)
    active = source._discover_active_candidates(premiums, tickers)

    assert selected == ["BTCUSDT"]
    assert active == []
    assert active_coin_registry.active_symbols() == []


def test_binance_factor_source_drops_existing_unsupported_active_symbols(monkeypatch):
    monkeypatch.setattr(settings, "radar_exclude_major_symbols_from_anomaly", False)
    source = BinanceFactorSource(client=FakeBinanceMarketClient())
    source._exchange_symbol_meta = {
        "BTCUSDT": _exchange_symbol_meta("BTCUSDT"),
        "龙虾USDT": _exchange_symbol_meta("龙虾USDT"),
    }
    active_coin_registry.update_candidates(["龙虾USDT", "BTCUSDT"], now=100.0)
    dynamic_symbol_stream.sync(["龙虾USDT", "BTCUSDT"], now=100.0)

    filtered = source._prioritize_active_symbols(
        ["BTCUSDT"],
        ["龙虾USDT", "BTCUSDT"],
        valid_symbols={"BTCUSDT", "龙虾USDT"},
    )
    source._drop_unsupported_active_symbols(now=101.0)
    dynamic_symbol_stream.sync(active_coin_registry.active_symbols(), now=101.0)

    assert filtered == ["BTCUSDT"]
    assert active_coin_registry.active_symbols() == ["BTCUSDT"]
    assert dynamic_symbol_stream.diagnostics()["active_symbols"] == ["BTCUSDT"]


def test_binance_factor_source_does_not_cut_active_pool_to_base_symbol_limit(monkeypatch):
    monkeypatch.setattr(settings, "binance_symbol_limit", 2)
    source = BinanceFactorSource(client=FakeBinanceMarketClient())

    selected = source._prioritize_active_symbols(
        ["BASE1USDT", "BASE2USDT"],
        ["HOT1USDT", "HOT2USDT", "HOT3USDT"],
    )

    assert selected[:3] == ["HOT1USDT", "HOT2USDT", "HOT3USDT"]
    assert len(selected) == 3


def test_binance_factor_source_filters_stale_active_symbols_not_in_current_market_rows(monkeypatch):
    monkeypatch.setattr(settings, "binance_symbol_limit", 2)
    source = BinanceFactorSource(client=FakeBinanceMarketClient())

    selected = source._prioritize_active_symbols(
        ["BASE1USDT", "BASE2USDT"],
        ["STALEUSDT", "HOTUSDT"],
        valid_symbols={"BASE1USDT", "BASE2USDT", "HOTUSDT"},
    )

    assert selected == ["HOTUSDT", "BASE1USDT"]


def test_binance_factor_source_respects_configured_concurrency_for_large_active_pool(monkeypatch):
    monkeypatch.setattr(settings, "binance_factor_concurrency", 4)
    source = BinanceFactorSource(client=FakeBinanceMarketClient())

    assert source._effective_concurrency(80) == 4


def test_binance_factor_source_preserves_completed_diagnostics_while_refreshing(monkeypatch):
    monkeypatch.setattr(settings, "binance_factor_ttl_seconds", 0)
    async_lib = __import__("asyncio")
    ready = async_lib.Event()
    release = async_lib.Event()

    class BlockingClient(FakeBinanceMarketClient):
        async def premium_index(self):
            ready.set()
            await release.wait()
            return await super().premium_index()

    source = BinanceFactorSource(client=BlockingClient())
    source.last_symbol_count = 80
    source.last_snapshot_count = 80
    source.last_effective_concurrency = 16
    source.last_refresh_timings = {"total_seconds": 21.0}

    async def run_refresh():
        task = async_lib.create_task(source.get_snapshots(force_refresh=True))
        await async_lib.wait_for(ready.wait(), timeout=1.0)
        assert source.refresh_in_progress is True
        assert source.last_symbol_count == 80
        assert source.last_snapshot_count == 80
        assert source.last_effective_concurrency == 16
        assert source.last_refresh_timings == {"total_seconds": 21.0}
        release.set()
        await task

    async_lib.run(run_refresh())

def test_radar_scan_ranks_trade_quality_above_raw_anomaly(monkeypatch):
    monkeypatch.setattr(settings, "radar_exclude_major_symbols_from_anomaly", False)
    monkeypatch.setattr(settings, "radar_require_short_term_anomaly", False)
    monkeypatch.setattr(radar_weight_calibrator, "report", lambda: {})
    monkeypatch.setattr("backend.radar.radar_engine.db.save_radar_items", lambda *args, **kwargs: None)
    engine = RadarEngine()

    async def fake_snapshots(force_refresh=False):
        return [
            MarketSnapshot("NOISYHOTUSDT", 1.0, 4.0, -4.0, 1.0, 5.0, -0.2, 0, 0.40, 0.60, 0.20, 2.5, 0.95),
            MarketSnapshot("QUALITYUSDT", 1.0, 1.7, 2.1, 5.4, 1.74, 0.04, 0, 0.515, 0.485, -0.09, 1.2, 0.52),
        ]

    monkeypatch.setattr(market_service, "get_snapshots", fake_snapshots)

    items = __import__("asyncio").run(engine.scan(force_refresh=True))

    assert items[0].symbol == "QUALITYUSDT"
    assert items[0].score_features["rank_model"] == "production_trade_quality_v2"
    assert items[0].score_explain["anomaly_score"] > 0
    assert items[0].fund_confirm_count >= 3


def test_radar_scan_attaches_universal_anomaly_prediction(monkeypatch):
    monkeypatch.setattr(settings, "radar_exclude_major_symbols_from_anomaly", False)
    monkeypatch.setattr(settings, "radar_require_short_term_anomaly", False)
    monkeypatch.setattr(radar_weight_calibrator, "report", lambda: {})
    monkeypatch.setattr("backend.radar.radar_engine.db.save_radar_items", lambda *args, **kwargs: None)
    engine = RadarEngine()

    async def fake_snapshots(force_refresh=False):
        return [
            MarketSnapshot(
                "MICROEDGEUSDT",
                1.0,
                1.4,
                2.0,
                2.4,
                3.2,
                1.1,
                0.0001,
                0.72,
                0.28,
                0.24,
                1.2,
                0.16,
                structure_metrics={
                    "current_wick_ratio": 0.16,
                    "current_body_ratio": 0.78,
                    "range_position": 0.62,
                    "breakout_up": True,
                    "breakout_down": False,
                },
            )
        ]

    monkeypatch.setattr(market_service, "get_snapshots", fake_snapshots)

    items = __import__("asyncio").run(engine.scan(force_refresh=True))
    prediction = items[0].score_features["universal_anomaly_model"]

    assert prediction["direction"] == "LONG"
    assert prediction["probabilities"]["LONG"] >= 0.58
    assert items[0].score_explain["components"]["universal_anomaly"] > 0
    assert any("universal_direction=LONG" in row for row in items[0].market_structure["evidence"])


def test_radar_ai_candidates_require_fund_confirm_3(monkeypatch):
    monkeypatch.setattr(
        "backend.radar.radar_engine.candidate_feature_enhancer.evaluate",
        stable_candidate_feature_report,
    )
    engine = RadarEngine()
    weak = high_quality_item(symbol="WEAKAIUSDT", side="LONG")
    weak.fund_confirm_count = 2
    weak.score = 95
    strong = high_quality_item(symbol="STRONGAIUSDT", side="LONG")
    strong.fund_confirm_count = 3
    strong.score = 80
    candidates = engine.select_ai_candidates([weak, strong])
    assert [item.symbol for item in candidates] == ["STRONGAIUSDT"]


def test_market_classifier_builds_structured_long_trend_plan():
    item = high_quality_item(symbol="TRENDLONGUSDT", side="LONG", price=100)
    item.atr_pct = 1.2
    item.wick_ratio = 0.25
    item.volume_spike = 2.4
    item.oi_change = 1.3
    item.fund_confirm_count = 4
    item.fund_confirm_total = 5

    setup = market_classifier.classify(item)

    assert setup["regime"] == "trend_continuation"
    assert setup["phase"] == "actionable"
    assert setup["bias"] == "LONG"
    assert setup["action"] == "OPEN_LONG"
    assert setup["entry_zone_low"] < setup["ideal_entry_price"] <= setup["entry_zone_high"]
    assert setup["stop_loss"] < setup["entry_zone_low"]
    assert setup["tp1"] > setup["ideal_entry_price"]
    assert setup["tp2"] > setup["tp1"]
    assert setup["risk_reward_r"] >= 2.0


def test_market_classifier_blocks_fake_or_noisy_breakout():
    item = high_quality_item(symbol="NOISYUSDT", side="LONG", price=100)
    item.fake_breakout_risk = "HIGH"
    item.wick_ratio = 0.92
    item.volume_spike = 3.5

    setup = market_classifier.classify(item)

    assert setup["regime"] == "fake_breakout"
    assert setup["phase"] == "invalid"
    assert setup["action"] == "WAIT"
    assert "fake_breakout_high" in setup["no_trade_reasons"]
    assert setup["stop_loss"] == 0.0


def test_market_classifier_keeps_non_short_term_candidate_out_of_open_action():
    item = high_quality_item(symbol="BUILDONLYUSDT", side="LONG", price=100)
    item.score_features["short_term_anomaly"] = False

    setup = market_classifier.classify(item)

    assert setup["action"] == "WAIT"
    assert setup["phase"] in {"observation", "building", "confirming"}
    assert "short_term_anomaly_absent" in setup["no_trade_reasons"]


def test_market_classifier_keeps_reference_levels_when_waiting_for_clean_entry():
    item = high_quality_item(symbol="WAITLEVELUSDT", side="SHORT", price=100)
    item.wick_ratio = 0.72
    item.volume_spike = 2.5
    item.fund_confirm_count = 3

    setup = market_classifier.classify(item)

    assert setup["action"] == "WAIT"
    assert setup["bias"] == "SHORT"
    assert "wick_too_high" in setup["no_trade_reasons"]
    assert setup["entry_zone_low"] < setup["ideal_entry_price"] <= setup["entry_zone_high"]
    assert setup["stop_loss"] > setup["entry_zone_high"]
    assert setup["tp2"] < setup["tp1"] < setup["ideal_entry_price"]


def test_rule_strategy_uses_market_structure_geometry():
    item = high_quality_item(symbol="STRUCTUREUSDT", side="LONG", price=100)
    item.market_structure = {
        "regime": "trend_continuation",
        "phase": "actionable",
        "bias": "LONG",
        "setup": "pullback_continuation",
        "action": "OPEN_LONG",
        "entry_zone_low": 99.2,
        "entry_zone_high": 100.1,
        "ideal_entry_price": 99.6,
        "stop_loss": 97.8,
        "tp1": 101.4,
        "tp2": 103.2,
        "confidence": 82,
        "no_trade_reasons": [],
        "invalidation": "structure_low_break",
    }

    plan = rule_strategy_generator.generate(item)

    assert plan.action == "OPEN_LONG"
    assert plan.entry_zone_low == 99.2
    assert plan.entry_zone_high == 100.1
    assert plan.ideal_entry_price == 99.6
    assert plan.stop_loss == 97.8
    assert plan.tp1 == 101.4
    assert plan.tp2 == 103.2
    assert plan.raw["market_structure"]["setup"] == "pullback_continuation"

def test_strict_ai_candidates_reject_medium_fake_risk():
    engine = RadarEngine()
    item = high_quality_item(symbol="MEDIUMFAKEUSDT", side="LONG")
    item.fake_breakout_risk = "MEDIUM"
    item.wick_ratio = 0.30
    item.score = 90

    selected = engine.select_ai_candidates([item])
    diagnostics = engine.production_candidate_diagnostics([item])

    assert selected == []
    assert "fake_breakout_not_low" in diagnostics["top_checked"][0]["failed"]

def test_strict_ai_candidates_reject_low_fake_wick_above_market_supported_band():
    engine = RadarEngine()
    item = high_quality_item(symbol="LOWWICK056USDT", side="LONG")
    item.fake_breakout_risk = "LOW"
    item.wick_ratio = 0.56
    item.score = 90

    selected = engine.select_ai_candidates([item])
    diagnostics = engine.production_candidate_diagnostics([item])

    assert selected == []
    assert "wick_too_high" in diagnostics["top_checked"][0]["failed"]

def test_strict_ai_candidates_block_recent_market_catastrophe(monkeypatch):
    engine = RadarEngine()
    short_item = high_quality_item(symbol="BADSHORTUSDT", side="SHORT")
    monkeypatch.setattr(
        "backend.radar.radar_engine.learning_data_audit.summary",
        lambda: {
            "market_backtest": {
                "generated_at_ms": now_ms(),
                "by_side_metrics": {
                    "SHORT": {"trades": 5, "win_rate": 0.0, "profit_factor": 0.0, "net_pnl_r": -6.4}
                }
            }
        },
    )

    selected = engine.select_ai_candidates([short_item])
    diagnostics = engine.production_candidate_diagnostics([short_item])

    assert selected == []
    assert "market_backtest_side_disallowed" in diagnostics["top_checked"][0]["failed"]

def test_strict_ai_candidates_block_recent_market_negative_expectancy_side(monkeypatch):
    engine = RadarEngine()
    long_item = high_quality_item(symbol="BADLONGUSDT", side="LONG")
    long_item.score = 90
    monkeypatch.setattr(
        "backend.radar.radar_engine.learning_data_audit.summary",
        lambda: {
            "market_backtest": {
                "generated_at_ms": now_ms(),
                "by_side_metrics": {
                    "LONG": {"trades": 20, "win_rate": 0.45, "profit_factor": 0.5281, "net_pnl_r": -6.1565}
                }
            }
        },
    )

    selected = engine.select_ai_candidates([long_item])
    diagnostics = engine.production_candidate_diagnostics([long_item])

    assert selected == []
    assert "market_backtest_side_disallowed" in diagnostics["top_checked"][0]["failed"]

def test_market_backtest_guard_blocks_negative_expectancy_side_from_current_metrics():
    from trading_lab.backtester import run_market_backtest as market_backtest

    blocks = market_backtest.side_blocks_from_market_metrics(
        {
            "LONG": {"trades": 20, "win_rate": 0.45, "profit_factor": 0.5281, "net_pnl_r": -6.1565},
            "SHORT": {"trades": 18, "win_rate": 0.8889, "profit_factor": 7.525, "net_pnl_r": 14.7984},
        }
    )

    assert [block["side"] for block in blocks] == ["LONG"]
    assert blocks[0]["reason"] == "recent_market_backtest_negative_expectancy"

def test_market_backtest_symbol_selection_excludes_tradifi(monkeypatch):
    from trading_lab.backtester import run_market_backtest as market_backtest

    monkeypatch.setattr(settings, "binance_symbol_limit", 4)
    monkeypatch.setattr(settings, "binance_mover_share", 0.5)

    async def fake_get_json(client, path, params):
        if path == "/fapi/v1/exchangeInfo":
            return {
                "symbols": [
                    {
                        "symbol": "BTCUSDT",
                        "status": "TRADING",
                        "contractType": "PERPETUAL",
                        "quoteAsset": "USDT",
                        "marginAsset": "USDT",
                        "underlyingType": "COIN",
                    },
                    {
                        "symbol": "GOODUSDT",
                        "status": "TRADING",
                        "contractType": "PERPETUAL",
                        "quoteAsset": "USDT",
                        "marginAsset": "USDT",
                        "underlyingType": "COIN",
                    },
                    {
                        "symbol": "SKHYNIXUSDT",
                        "status": "TRADING",
                        "contractType": "TRADIFI_PERPETUAL",
                        "quoteAsset": "USDT",
                        "marginAsset": "USDT",
                        "underlyingType": "KR_EQUITY",
                        "underlyingSubType": ["TradFi"],
                    },
                ]
            }
        return [
            {"symbol": "BTCUSDT", "lastPrice": "100", "quoteVolume": "10", "priceChangePercent": "0.1"},
            {"symbol": "GOODUSDT", "lastPrice": "1", "quoteVolume": "800", "priceChangePercent": "4"},
            {"symbol": "SKHYNIXUSDT", "lastPrice": "300000", "quoteVolume": "900000", "priceChangePercent": "80"},
        ]

    monkeypatch.setattr(market_backtest, "get_json", fake_get_json)

    selected = __import__("asyncio").run(market_backtest.select_symbols(object(), 4))

    assert selected == ["BTCUSDT", "GOODUSDT"]

def test_market_backtest_fetch_marks_discontinuous_klines(monkeypatch):
    from trading_lab.backtester import run_market_backtest as market_backtest

    async def fake_get_json(client, path, params):
        if params["symbol"] == "GAPUSDT":
            closes = [2.6] * 27 + [2.55, 0.63, 0.7]
        else:
            closes = [100 + idx * 0.2 for idx in range(30)]
        rows = []
        for idx, close in enumerate(closes):
            open_price = closes[idx - 1] if idx else close
            rows.append([
                idx * 300000,
                str(open_price),
                str(max(open_price, close) * 1.002),
                str(min(open_price, close) * 0.998),
                str(close),
                "10",
                idx * 300000 + 299999,
                "1000",
                100,
                "7",
                "600",
                "0",
            ])
        return rows

    monkeypatch.setattr(market_backtest, "get_json", fake_get_json)

    fetched = __import__("asyncio").run(
        market_backtest.fetch_market_data(
            client=object(),
            symbols=["GOODUSDT", "GAPUSDT"],
            interval="5m",
            interval_ms=300000,
            start_ms=0,
            end_ms=30 * 300000,
            fetch_oi=False,
        )
    )

    assert len(fetched["GOODUSDT"]["candles"]) == 30
    assert fetched["GAPUSDT"]["candles"] == []
    assert "kline_close_discontinuity" in fetched["GAPUSDT"]["errors"]

def test_market_side_blocks_include_expiry_metadata(monkeypatch):
    from backend.learning.market_side_guard import side_blocks_from_market_metrics

    monkeypatch.setattr(settings, "market_side_block_ttl_hours", 2.0, raising=False)
    started_ms = now_ms()

    blocks = side_blocks_from_market_metrics(
        {
            "LONG": {"trades": 20, "win_rate": 0.45, "profit_factor": 0.5281, "net_pnl_r": -6.1565},
        }
    )

    assert blocks[0]["created_at_ms"] >= started_ms
    assert blocks[0]["expires_at_ms"] == blocks[0]["created_at_ms"] + 2 * 3600 * 1000

def test_market_backtest_candidate_check_ignores_prior_side_blocks_during_measurement(monkeypatch):
    from trading_lab.backtester import run_market_backtest as market_backtest

    long_item = high_quality_item(symbol="RECOVERLONGUSDT", side="LONG")
    long_item.score = 90
    feature = {"feature_score": 80.0, "estimated_win_rate": 0.66, "selection_score": 82.0}
    monkeypatch.setattr(
        "trading_lab.backtester.run_market_backtest.learning_data_audit.summary",
        lambda: {
            "market_backtest": {
                "side_blocks": [
                    {
                        "side": "LONG",
                        "reason": "recent_market_backtest_negative_expectancy",
                        "created_at_ms": now_ms(),
                        "expires_at_ms": now_ms() + 3600000,
                    }
                ]
            }
        },
    )

    ok, reasons = market_backtest.production_candidate_check(long_item, feature)

    assert ok
    assert "market_backtest_side_disallowed" not in reasons

def test_strict_ai_candidates_ignore_expired_market_side_block(monkeypatch):
    engine = RadarEngine()
    long_item = high_quality_item(symbol="RECOVERAIUSDT", side="LONG")
    long_item.score = 90
    monkeypatch.setattr(
        "backend.radar.radar_engine.candidate_feature_enhancer.evaluate",
        lambda item: stable_candidate_feature_report(item),
    )
    monkeypatch.setattr(
        "backend.radar.radar_engine.learning_data_audit.summary",
        lambda: {
            "market_backtest": {
                "side_blocks": [
                    {
                        "side": "LONG",
                        "reason": "recent_market_backtest_negative_expectancy",
                        "created_at_ms": now_ms() - 7200000,
                        "expires_at_ms": now_ms() - 1,
                    }
                ]
            }
        },
    )

    selected = engine.select_ai_candidates([long_item])
    diagnostics = engine.production_candidate_diagnostics([long_item])

    assert [item.symbol for item in selected] == ["RECOVERAIUSDT"]
    assert "market_backtest_side_disallowed" not in diagnostics["top_checked"][0]["failed"]

def test_market_backtest_guard_releases_prior_side_when_current_metrics_recover(monkeypatch):
    from trading_lab.backtester import run_market_backtest as market_backtest

    monkeypatch.setattr(
        "trading_lab.backtester.run_market_backtest.learning_data_audit.summary",
        lambda: {
            "market_backtest": {
                "side_blocks": [
                    {
                        "side": "LONG",
                        "reason": "recent_market_backtest_negative_expectancy",
                        "created_at_ms": now_ms(),
                        "expires_at_ms": now_ms() + 3600000,
                    }
                ]
            }
        },
    )

    guard = market_backtest.current_market_guard(
        {
            "LONG": {"trades": 20, "win_rate": 0.60, "profit_factor": 1.35, "net_pnl_r": 3.2},
            "SHORT": {"trades": 18, "win_rate": 0.8889, "profit_factor": 7.525, "net_pnl_r": 14.7984},
        }
    )

    assert guard["side_blocks"] == []

def test_strict_review_candidates_allow_high_quality_partial_fund_for_paper_review():
    engine = RadarEngine()
    partial = high_quality_item(symbol="REVIEWAIUSDT", side="LONG")
    partial.fund_confirm_count = 2
    partial.score = 50

    strict = engine.select_ai_candidates([partial])
    review = engine.select_ai_review_candidates([partial])

    assert strict == []
    assert [item.symbol for item in review] == ["REVIEWAIUSDT"]

def test_autotrader_uses_strict_review_fallback_in_paper_closed_loop(monkeypatch):
    partial = high_quality_item(symbol="REVIEWFALLBACKUSDT", side="LONG")
    partial.fund_confirm_count = 2
    partial.score = 50
    monkeypatch.setattr(settings, "auto_trading_candidate_mode", "strict")
    monkeypatch.setattr(settings, "trade_mode", "paper")
    monkeypatch.setattr(settings, "live_trading_enabled", False)
    monkeypatch.setattr(settings, "auto_trading_candidate_limit", 1)
    monkeypatch.setattr(radar_engine, "top50", [partial])

    candidates, source = autotrader._candidate_batch({"recovery_mode": False})

    assert source == "strict_review"
    assert [item.symbol for item in candidates] == ["REVIEWFALLBACKUSDT"]

def test_candidate_feature_floor_preserves_exceptional_current_signal(monkeypatch):
    item = high_quality_item(symbol="FLOORUSDT", side="LONG")
    item.score = 50
    item.fund_confirm_count = 3
    item.fake_breakout_risk = "LOW"
    item.wick_ratio = 0.40
    monkeypatch.setattr(settings, "strategy_min_paper_win_rate", 0.56)
    monkeypatch.setattr(
        "backend.radar.candidate_feature_enhancer.learning_data_audit.summary",
        lambda: {"can_hard_block_from_learning": False},
    )

    monkeypatch.setattr(
        "backend.radar.candidate_feature_enhancer.trade_attributor.evaluate",
        lambda item, plan: SimpleNamespace(matched_samples=30, win_rate=0.48, profit_factor=0.92),
    )
    monkeypatch.setattr(
        "backend.radar.candidate_feature_enhancer.event_calibrator.compact_context",
        lambda item: {"similar_current_event": {"samples": 100, "win_rate": 0.48, "profit_factor": 0.90}},
    )

    report = candidate_feature_enhancer.evaluate(item)

    assert report.feature_score >= 85
    assert report.estimated_win_rate >= 0.56
    assert "attribution_low_trust_not_blended" in report.reasons
    assert "event_low_trust_not_blended" in report.reasons

def test_candidate_feature_floor_does_not_override_negative_attribution(monkeypatch):
    item = high_quality_item(symbol="LOSSFLOORUSDT", side="SHORT")
    item.score = 50
    item.fund_confirm_count = 3
    item.fake_breakout_risk = "LOW"
    item.wick_ratio = 0.40
    monkeypatch.setattr(settings, "strategy_min_paper_win_rate", 0.56)
    monkeypatch.setattr(settings, "trade_attribution_min_samples", 8)
    monkeypatch.setattr(settings, "trade_attribution_block_win_rate", 0.42)
    monkeypatch.setattr(settings, "trade_attribution_block_profit_factor", 0.85)
    monkeypatch.setattr(settings, "event_calibration_min_samples", 20)
    monkeypatch.setattr(
        "backend.radar.candidate_feature_enhancer.learning_data_audit.summary",
        lambda: {"can_hard_block_from_learning": True},
    )

    monkeypatch.setattr(
        "backend.radar.candidate_feature_enhancer.trade_attributor.evaluate",
        lambda item, plan: SimpleNamespace(matched_samples=12, win_rate=0.3333, profit_factor=0.42),
    )
    monkeypatch.setattr(
        "backend.radar.candidate_feature_enhancer.event_calibrator.compact_context",
        lambda item: {"similar_current_event": {"samples": 100, "win_rate": 0.33, "profit_factor": 0.63}},
    )

    report = candidate_feature_enhancer.evaluate(item)

    assert report.feature_score >= 85
    assert report.estimated_win_rate < 0.56
    assert "historical_hard_block_kept" in report.reasons
    assert "current_feature_floor_applied" not in report.reasons

def test_candidate_feature_floor_does_not_override_low_trust_negative_history(monkeypatch):
    item = high_quality_item(symbol="LOWTRUSTLOSSUSDT", side="LONG")
    item.score = 50
    item.fund_confirm_count = 3
    item.fake_breakout_risk = "LOW"
    item.wick_ratio = 0.40
    monkeypatch.setattr(settings, "strategy_min_paper_win_rate", 0.56)
    monkeypatch.setattr(settings, "trade_attribution_min_samples", 8)
    monkeypatch.setattr(settings, "trade_attribution_block_win_rate", 0.42)
    monkeypatch.setattr(settings, "trade_attribution_block_profit_factor", 0.85)
    monkeypatch.setattr(settings, "event_calibration_min_samples", 20)
    monkeypatch.setattr(
        "backend.radar.candidate_feature_enhancer.learning_data_audit.summary",
        lambda: {"can_hard_block_from_learning": False, "trust_level": "MEDIUM"},
    )

    monkeypatch.setattr(
        "backend.radar.candidate_feature_enhancer.trade_attributor.evaluate",
        lambda item, plan: SimpleNamespace(matched_samples=30, win_rate=0.31, profit_factor=0.42),
    )
    monkeypatch.setattr(
        "backend.radar.candidate_feature_enhancer.event_calibrator.compact_context",
        lambda item: {"similar_current_event": {"samples": 100, "win_rate": 0.33, "profit_factor": 0.63}},
    )

    report = candidate_feature_enhancer.evaluate(item)

    assert report.feature_score >= 85
    assert report.estimated_win_rate < 0.56
    assert "historical_negative_floor_blocked" in report.reasons
    assert "current_feature_floor_applied" not in report.reasons

def test_update_env_values_preserves_unrelated_keys(tmp_path):
    env_path = tmp_path / ".env"
    env_path.write_text("APP_PORT=8000\nBINANCE_TESTNET=true\nKEEP_ME=value\n", encoding="utf-8")
    update_env_values(env_path, {"BINANCE_TESTNET": "false", "BINANCE_API_KEY": "abc"})
    text = env_path.read_text(encoding="utf-8")
    assert "APP_PORT=8000" in text
    assert "KEEP_ME=value" in text
    assert "BINANCE_TESTNET=false" in text
    assert "BINANCE_API_KEY=abc" in text

def test_strategy_quality_gate_blocks_low_expectancy_plan():
    item = high_quality_item()
    poor_plan = StrategyPlan(
        strategy_id="bad",
        action="OPEN_LONG",
        symbol=item.symbol,
        side="LONG",
        entry_zone_low=100,
        entry_zone_high=100,
        ideal_entry_price=100,
        stop_loss=99,
        tp1=100.4,
        tp2=101.0,
        confidence=50,
        reason="bad rr",
    )
    report = strategy_quality_gate.evaluate(item, poor_plan)
    assert not report.paper_ok
    assert not report.live_ok
    assert "tp2_r_too_low" in report.reasons

def test_strategy_geometry_sampler_selects_validated_geometry(monkeypatch):
    from backend.learning.strategy_geometry_sampler import StrategyGeometrySampler

    item = high_quality_item(symbol="GEOMETRYUSDT", side="LONG", price=100)
    klines = []
    for index in range(90):
        open_price = 100.0
        close = 100.8
        high = 103.5
        low = 100.35
        klines.append([index, str(open_price), str(high), str(low), str(close), "10", index + 1, "1000", 100, "7", "700", "0"])

    sampler = StrategyGeometrySampler(fetch_klines=lambda symbol, interval, limit: klines)
    report = __import__("asyncio").run(sampler.evaluate(item))

    assert report["status"] == "ok"
    assert report["selected_geometry"]["side"] == "LONG"
    assert report["selected_geometry"]["tp2_r"] >= settings.strategy_min_tp2_r
    assert report["samples"]["sample_count"] >= 60
    assert report["samples"]["win_rate"] >= settings.strategy_min_paper_win_rate
    assert report["samples"]["expected_r"] >= settings.strategy_min_expected_r

def test_strategy_quality_gate_blocks_codex_open_when_geometry_sample_is_weak():
    item = high_quality_item(symbol="WEAKGEOMETRYUSDT", side="LONG", price=100)
    plan = StrategyPlan(
        strategy_id="weak_geometry",
        action="OPEN_LONG",
        symbol=item.symbol,
        side="LONG",
        entry_zone_low=99.9,
        entry_zone_high=100.1,
        ideal_entry_price=100,
        stop_loss=99,
        tp1=101.2,
        tp2=103.0,
        confidence=78,
        reason="codex open with weak geometry evidence",
        raw={
            "provider": "codex_cli",
            "strategy_geometry_sample_required": True,
            "strategy_geometry_sample": {
                "status": "weak",
                "samples": {"sample_count": 90, "win_rate": 0.41, "expected_r": -0.12, "profit_factor": 0.72},
                "selected_geometry": {},
            },
        },
    )

    report = strategy_quality_gate.evaluate(item, plan)

    assert not report.paper_ok
    assert "strategy_geometry_sample_not_ok" in report.reasons
    assert report.geometry_sample["status"] == "weak"

def test_strategy_quality_gate_requires_fund_confirm_3():
    item = high_quality_item()
    item.fund_confirm_count = 2
    plan = StrategyPlan(
        strategy_id="confirm_gate",
        action="OPEN_LONG",
        symbol=item.symbol,
        side="LONG",
        entry_zone_low=100,
        entry_zone_high=100,
        ideal_entry_price=100,
        stop_loss=99,
        tp1=101,
        tp2=103,
        confidence=90,
        reason="confirm gate",
    )
    report = strategy_quality_gate.evaluate(item, plan)
    assert not report.paper_ok
    assert not report.live_ok
    assert "fund_confirm_3_required" in report.reasons

def test_strategy_quality_gate_requires_low_fake_risk_for_paper_open():
    item = high_quality_item(symbol="QUALITYMEDIUMUSDT", side="LONG", price=100)
    item.fake_breakout_risk = "MEDIUM"
    plan = StrategyPlan(
        strategy_id="medium_fake_gate",
        action="OPEN_LONG",
        symbol=item.symbol,
        side="LONG",
        entry_zone_low=100,
        entry_zone_high=100,
        ideal_entry_price=100,
        stop_loss=99,
        tp1=101,
        tp2=103,
        confidence=90,
        reason="medium fake should not pass paper quality",
    )
    report = strategy_quality_gate.evaluate(item, plan)
    assert not report.paper_ok
    assert "fake_breakout_not_low" in report.reasons

def test_strategy_quality_gate_blocks_wick_above_market_supported_budget():
    item = high_quality_item(symbol="QUALITYWICKUSDT", side="LONG", price=100)
    item.wick_ratio = 0.56
    plan = StrategyPlan(
        strategy_id="wick_gate",
        action="OPEN_LONG",
        symbol=item.symbol,
        side="LONG",
        entry_zone_low=100,
        entry_zone_high=100,
        ideal_entry_price=100,
        stop_loss=99,
        tp1=101,
        tp2=103,
        confidence=90,
        reason="wick should not pass paper quality",
    )
    report = strategy_quality_gate.evaluate(item, plan)
    assert not report.paper_ok
    assert "wick_above_quality_budget" in report.reasons

def test_strategy_quality_gate_allows_balanced_old_wick_for_paper_only(monkeypatch):
    monkeypatch.setattr(settings, "paper_probe_max_wick_ratio", 0.55)
    monkeypatch.setattr(event_calibrator, "evaluate", lambda item, plan, heuristic_win_rate: SimpleNamespace(
        matched_samples=0,
        adjusted_win_rate=heuristic_win_rate,
        paper_ok=True,
        live_ok=False,
        reasons=[],
        asdict=lambda: {},
    ))
    monkeypatch.setattr(trade_attributor, "evaluate", lambda item, plan: SimpleNamespace(
        paper_ok=True,
        live_ok=False,
        reasons=[],
        asdict=lambda: {},
    ))
    item = high_quality_item(symbol="QUALITYBALANCEDWICKUSDT", side="LONG", price=100)
    item.score = 92
    item.fund_confirm_count = 3
    item.fund_confirm_total = 5
    item.wick_ratio = 0.96
    item.score_features = {
        "structure_metrics": {
            "current_wick_ratio": 0.18,
            "max_wick_ratio_14": 0.96,
            "avg_wick_ratio_14": 0.42,
            "bars_since_max_wick": 8,
        }
    }
    plan = StrategyPlan(
        strategy_id="balanced_wick_gate",
        action="OPEN_LONG",
        symbol=item.symbol,
        side="LONG",
        entry_zone_low=100,
        entry_zone_high=100,
        ideal_entry_price=100,
        stop_loss=99,
        tp1=101,
        tp2=103,
        confidence=90,
        reason="old wick can be paper balanced",
    )
    report = strategy_quality_gate.evaluate(item, plan)
    assert report.paper_ok
    assert not report.live_ok
    assert "wick_above_quality_budget" not in report.reasons

def test_strategy_quality_gate_blocks_extreme_current_wick_even_when_recent_max_is_low(monkeypatch):
    monkeypatch.setattr(settings, "paper_probe_max_wick_ratio", 0.55)
    monkeypatch.setattr(event_calibrator, "evaluate", lambda item, plan, heuristic_win_rate: SimpleNamespace(
        matched_samples=0,
        adjusted_win_rate=heuristic_win_rate,
        paper_ok=True,
        live_ok=False,
        reasons=[],
        asdict=lambda: {},
    ))
    monkeypatch.setattr(trade_attributor, "evaluate", lambda item, plan: SimpleNamespace(
        paper_ok=True,
        live_ok=False,
        reasons=[],
        asdict=lambda: {},
    ))
    item = high_quality_item(symbol="QUALITYCURRENTWICKUSDT", side="LONG", price=100)
    item.score = 100
    item.fund_confirm_count = 3
    item.fund_confirm_total = 5
    item.wick_ratio = 0.30
    item.score_features = {
        "structure_metrics": {
            "current_wick_ratio": 0.90,
            "max_wick_ratio_14": 0.30,
            "avg_wick_ratio_14": 0.20,
            "bars_since_max_wick": 0,
        }
    }
    plan = StrategyPlan(
        strategy_id="current_wick_gate",
        action="OPEN_LONG",
        symbol=item.symbol,
        side="LONG",
        entry_zone_low=100,
        entry_zone_high=100,
        ideal_entry_price=100,
        stop_loss=99,
        tp1=101,
        tp2=103,
        confidence=90,
        reason="current wick should stay hard blocked",
    )
    report = strategy_quality_gate.evaluate(item, plan)
    assert not report.paper_ok
    assert "wick_above_quality_budget" in report.reasons
    assert "current_wick_extreme" in report.reasons

def test_strategy_quality_gate_uses_event_calibration_to_block_bad_similar_events(monkeypatch):
    monkeypatch.setattr(settings, "event_calibration_enabled", True)
    monkeypatch.setattr(settings, "event_calibration_min_samples", 4)
    monkeypatch.setattr(settings, "event_calibration_min_win_rate", 0.6)
    monkeypatch.setattr(settings, "event_calibration_min_profit_factor", 1.2)
    monkeypatch.setattr(settings, "event_calibration_min_pnl", 0.0)
    samples = [
        sample_trade(side="LONG", pnl=-1.0, close_time=1),
        sample_trade(side="LONG", pnl=-0.8, close_time=2),
        sample_trade(side="LONG", pnl=-0.6, close_time=3),
        sample_trade(side="LONG", pnl=0.2, close_time=4),
    ]
    monkeypatch.setattr(event_calibrator, "_samples", lambda: samples)
    item = high_quality_item(side="LONG")
    plan = StrategyPlan(
        strategy_id="calibrated_block",
        action="OPEN_LONG",
        symbol=item.symbol,
        side="LONG",
        entry_zone_low=100,
        entry_zone_high=100,
        ideal_entry_price=100,
        stop_loss=99,
        tp1=101,
        tp2=103,
        confidence=90,
        reason="calibrated block",
    )
    report = strategy_quality_gate.evaluate(item, plan)
    assert not report.paper_ok
    assert "event_calibrated_win_rate_low" in report.reasons
    assert report.calibration["matched_samples"] >= 4


def test_strategy_quality_gate_reviews_event_calibration_for_paper_validation(monkeypatch):
    monkeypatch.setattr(settings, "event_calibration_enabled", True)
    monkeypatch.setattr(settings, "event_calibration_min_samples", 4)
    monkeypatch.setattr(settings, "event_calibration_min_win_rate", 0.6)
    monkeypatch.setattr(settings, "event_calibration_min_profit_factor", 1.2)
    monkeypatch.setattr(settings, "event_calibration_min_pnl", 0.0)
    monkeypatch.setattr(
        trade_attributor,
        "evaluate",
        lambda *_args, **_kwargs: SimpleNamespace(
            paper_ok=False,
            live_ok=False,
            reasons=["causal_pattern_win_rate_low"],
            asdict=lambda: {"paper_ok": False, "live_ok": False, "reasons": ["causal_pattern_win_rate_low"]},
        ),
    )
    samples = [
        sample_trade(side="LONG", pnl=-1.0, close_time=1),
        sample_trade(side="LONG", pnl=-0.8, close_time=2),
        sample_trade(side="LONG", pnl=-0.6, close_time=3),
        sample_trade(side="LONG", pnl=0.2, close_time=4),
    ]
    monkeypatch.setattr(event_calibrator, "_samples", lambda: samples)
    item = high_quality_item(side="LONG")
    plan = StrategyPlan(
        strategy_id="paper_validation_event_review",
        action="OPEN_LONG",
        symbol=item.symbol,
        side="LONG",
        entry_zone_low=100,
        entry_zone_high=100,
        ideal_entry_price=100,
        stop_loss=99,
        tp1=101,
        tp2=103,
        confidence=90,
        reason="paper validation should collect forward evidence",
        raw={"paper_validation": {"source": "paper_top"}},
    )

    report = strategy_quality_gate.evaluate(item, plan)

    assert report.paper_ok is True
    assert report.live_ok is False
    assert "event_calibrated_win_rate_low" in report.reasons
    assert "causal_pattern_win_rate_low" in report.reasons
    assert report.calibration["paper_ok"] is False
    assert report.attribution["paper_ok"] is False


def test_strategy_quality_gate_allows_positive_ev_borderline_win_rate_for_paper_validation(monkeypatch):
    monkeypatch.setattr(settings, "strategy_min_paper_win_rate", 0.53)
    monkeypatch.setattr(settings, "event_calibration_enabled", False)
    monkeypatch.setattr(
        trade_attributor,
        "evaluate",
        lambda *_args, **_kwargs: SimpleNamespace(
            paper_ok=True,
            live_ok=False,
            reasons=[],
            asdict=lambda: {"paper_ok": True, "live_ok": False, "reasons": []},
        ),
    )
    monkeypatch.setattr(strategy_quality_gate, "_estimate_win_rate", lambda *_args, **_kwargs: (0.515, ["borderline_positive_ev"]))
    item = high_quality_item(side="LONG")
    plan = StrategyPlan(
        strategy_id="borderline_validation",
        action="OPEN_LONG",
        symbol=item.symbol,
        side="LONG",
        entry_zone_low=100,
        entry_zone_high=100,
        ideal_entry_price=100,
        stop_loss=99,
        tp1=101,
        tp2=103,
        confidence=90,
        reason="positive EV validation",
        raw={"paper_validation": {"source": "paper_top"}},
    )
    formal_plan = StrategyPlan(
        **{**plan.__dict__, "strategy_id": "borderline_formal", "raw": {}}
    )

    validation_report = strategy_quality_gate.evaluate(item, plan)
    formal_report = strategy_quality_gate.evaluate(item, formal_plan)

    assert validation_report.estimated_win_rate == 0.515
    assert validation_report.expected_r >= settings.strategy_min_expected_r
    assert validation_report.paper_ok is True
    assert formal_report.paper_ok is False


def test_event_calibrator_allows_paper_when_recent_similar_events_recovered(monkeypatch):
    monkeypatch.setattr(settings, "event_calibration_enabled", True)
    monkeypatch.setattr(settings, "event_calibration_min_samples", 3)
    monkeypatch.setattr(settings, "event_calibration_min_win_rate", 0.6)
    monkeypatch.setattr(settings, "event_calibration_min_profit_factor", 1.2)
    monkeypatch.setattr(settings, "event_calibration_min_pnl", 0.0)
    samples = [
        *[sample_trade(side="LONG", pnl=-1.0, close_time=idx) for idx in range(1, 7)],
        *[sample_trade(side="LONG", pnl=1.0, close_time=idx) for idx in range(7, 10)],
    ]
    monkeypatch.setattr(event_calibrator, "_samples", lambda: sorted(samples, key=lambda row: row["close_time"], reverse=True))
    item = high_quality_item(side="LONG")
    plan = StrategyPlan(
        strategy_id="recent_recovery",
        action="OPEN_LONG",
        symbol=item.symbol,
        side="LONG",
        entry_zone_low=100,
        entry_zone_high=100,
        ideal_entry_price=100,
        stop_loss=99,
        tp1=101,
        tp2=103,
        confidence=90,
        reason="recent event structure recovered",
    )

    report = event_calibrator.evaluate(item, plan, heuristic_win_rate=0.7)

    assert report.matched_samples == 9
    assert report.paper_ok is True
    assert report.live_ok is False
    assert "recent_similar_event_recovered" in report.reasons
    assert "event_calibrated_win_rate_low" not in report.reasons
    assert "event_calibrated_profit_factor_low" not in report.reasons
    assert "event_calibrated_pnl_low" not in report.reasons

def test_trade_attributor_identifies_physical_loss_causes(monkeypatch):
    monkeypatch.setattr(settings, "trade_attribution_enabled", True)
    monkeypatch.setattr(settings, "trade_attribution_use_replay", False)
    monkeypatch.setattr(settings, "trade_attribution_min_samples", 3)
    samples = []
    for idx in range(3):
        sample = sample_trade(side="LONG", pnl=-0.08, close_time=idx + 1)
        sample.update({"notional": 3.0, "margin": 1.5, "fee": 0.004, "gross_pnl": -0.07})
        samples.append(sample)
    monkeypatch.setattr(trade_attributor, "_samples", lambda: [trade_attributor._normalize_sample(s) for s in samples])
    summary = trade_attributor.summary()
    factors = {item["factor"] for item in summary["main_loss_causes"]}
    assert "small_notional" in factors
    assert "small_margin" in factors

def test_trade_attributor_deep_analysis_explains_loss_trades(monkeypatch):
    monkeypatch.setattr(settings, "trade_attribution_enabled", True)
    monkeypatch.setattr(settings, "trade_attribution_use_replay", False)
    monkeypatch.setattr(settings, "trade_attribution_min_samples", 3)
    samples = []
    for idx in range(3):
        sample = sample_trade(side="LONG", pnl=-0.12, close_time=idx + 1)
        sample.update({"notional": 3.0, "margin": 1.5, "fee": 0.02, "gross_pnl": -0.10, "close_reason": "SL"})
        samples.append(sample)
    monkeypatch.setattr(trade_attributor, "_samples", lambda: [trade_attributor._normalize_sample(s) for s in samples])
    report = trade_attributor.deep_analysis(trade_limit=3)
    root_codes = {item["code"] for item in report["root_causes"]}
    assert "small_notional" in root_codes
    assert "stop_loss_hit" in root_codes
    assert report["recent_loss_trades"][0]["root_causes"]
    assert report["action_rules"]


def test_trade_memory_excludes_non_learning_close_reasons(monkeypatch):
    rows = [
        {
            "position_id": "pos_good_memory",
            "symbol": "MEMGOODUSDT",
            "side": "LONG",
            "pnl": 1.0,
            "close_reason": "TP2",
            "source_signal_id": "scan_memory",
            "close_time": 2,
        },
        {
            "position_id": "pos_stale_memory",
            "symbol": "MEMSTALEUSDT",
            "side": "LONG",
            "pnl": -10.0,
            "close_reason": "PRICE_SOURCE_STALE_RECONCILE",
            "source_signal_id": "scan_memory",
            "close_time": 3,
        },
    ]
    monkeypatch.setattr(position_registry, "list_closed", lambda limit=10000: rows)
    monkeypatch.setattr(trade_memory, "_radar_for", lambda scan_id, symbol: high_quality_item(symbol=symbol, side="LONG").asdict())

    samples = trade_memory.samples()
    summary = trade_memory.summary()

    assert [sample["position_id"] if "position_id" in sample else sample["sample_id"] for sample in samples] == ["pos_good_memory"]
    assert summary["closed_trades"] == 1
    assert summary["raw_closed_trades"] == 2
    assert summary["excluded_closed_trades"] == 1


def test_market_backtest_csv_exports_candidate_factor_fields(tmp_path):
    from trading_lab.backtester.run_market_backtest import write_trades_csv

    path = tmp_path / "trades.csv"
    trade = {
        "symbol": "AUDITUSDT",
        "side": "LONG",
        "entry_time": "2026-06-24T00:00:00+00:00",
        "exit_time": "2026-06-24T00:05:00+00:00",
        "rank": 1,
        "score": 72.0,
        "feature_score": 88.0,
        "estimated_win_rate": 0.61,
        "selection_score": 70.0,
        "entry_price": 1.0,
        "exit_price": 1.01,
        "stop_loss": 0.99,
        "take_profit": 1.02,
        "risk_pct": 1.0,
        "net_r": 0.8,
        "gross_r": 0.9,
        "cost_r": 0.1,
        "mfe_r": 1.2,
        "mae_r": -0.3,
        "win": True,
        "close_reason": "TP",
        "candidate": {
            "fund_confirm": "3/5",
            "fake_breakout_risk": "LOW",
            "direction_confirmations": 6,
            "change_5m": 0.8,
            "change_15m": 1.4,
            "change_1h": 2.2,
            "volume_spike": 1.9,
            "oi_change": 0.4,
            "taker_buy_ratio": 0.57,
            "taker_sell_ratio": 0.43,
            "wick_ratio": 0.44,
        },
    }

    write_trades_csv(path, [trade])
    header = path.read_text(encoding="utf-8").splitlines()[0].split(",")

    assert "candidate_fund_confirm" in header
    assert "candidate_fake_breakout_risk" in header
    assert "candidate_direction_confirmations" in header
    assert "candidate_wick_ratio" in header


def test_market_backtest_uses_live_trade_quality_score_model():
    from trading_lab.backtester.run_market_backtest import backtest_trade_quality_score

    item = high_quality_item(symbol="MODELUSDT", side="LONG")
    item.score_features = {
        "trend_score": 80,
        "volume_score": 70,
        "volatility_score": 50,
        "oi_score": 80,
        "taker_score": 65,
        "timeframe_score": 80,
        "sm_score": 55,
        "heat_score": 20,
        "fake_penalty": 5,
    }
    anomaly_score = score_engine.total(item.score_features)
    components = fund_confirm_components(MarketSnapshot(
        item.symbol,
        item.price,
        item.change_5m,
        item.change_15m,
        item.change_1h,
        item.volume_spike,
        item.oi_change,
        item.funding_rate,
        item.taker_buy_ratio,
        item.taker_sell_ratio,
        item.depth_imbalance,
        item.atr_pct,
        item.wick_ratio,
    ), item.direction)

    score, explain = backtest_trade_quality_score(item, anomaly_score, components, SCORE_WEIGHTS, {})

    assert explain["score_model"] == "production_trade_quality_v2"
    assert score != anomaly_score
    assert explain["anomaly_score"] == anomaly_score


def test_learning_data_audit_counts_only_learning_closed_trades(monkeypatch):
    learning_data_audit.clear_cache()
    monkeypatch.setattr(settings, "replay_enabled", False)
    monkeypatch.setattr(trade_memory, "samples", lambda limit=10000, require_radar=True: [sample_trade()])
    monkeypatch.setattr(
        db,
        "list_closed",
        lambda limit=100000: [
            {"close_reason": "TP2"},
            {"close_reason": "PRICE_SOURCE_STALE_RECONCILE"},
        ],
    )
    monkeypatch.setattr(learning_data_audit, "_radar_summary", lambda: {"span_days": 0, "rows": 0, "distinct_scans": 0, "distinct_symbols": 0})
    monkeypatch.setattr(
        learning_data_audit,
        "_market_backtest_summary",
        lambda: {
            "available": False,
            "quality_passed": False,
            "missing_reasons": ["market_backtest_report_missing"],
        },
    )

    report = learning_data_audit.summary(force=True)

    assert report["sources"]["closed_trades_total"] == 1
    assert report["sources"]["raw_closed_trades_total"] == 2
    assert report["sources"]["excluded_closed_trades"] == 1

def test_learning_data_audit_rejects_market_backtest_before_learning_reset(monkeypatch):
    learning_data_audit.clear_cache()
    monkeypatch.setattr(settings, "replay_enabled", True)
    monkeypatch.setattr(replay_memory, "samples", lambda limit=None: [])
    monkeypatch.setattr(trade_memory, "samples", lambda limit=10000, require_radar=True: [])
    monkeypatch.setattr(db, "list_closed", lambda limit=100000: [])
    monkeypatch.setattr(db, "get_kv", lambda key, default=None: 2000 if key == "learning_data_reset_at_ms" else default)
    monkeypatch.setattr(learning_data_audit, "_radar_summary", lambda: {"span_days": 0, "rows": 0, "distinct_scans": 0, "distinct_symbols": 0})
    monkeypatch.setattr(
        learning_data_audit,
        "_market_backtest_summary",
        lambda: {
            "available": True,
            "quality_passed": True,
            "quality_blockers": [],
            "generated_at_ms": 1000,
            "span_days": 14.0,
            "metrics": {"trades": 38, "win_rate": 0.68, "profit_factor": 1.8, "net_pnl_r": 11.4},
            "holdout_metrics": {"trades": 12, "win_rate": 0.66, "profit_factor": 1.4, "net_pnl_r": 2.0},
        },
    )

    report = learning_data_audit.summary(force=True)

    assert report["production_grade"] is False
    assert report["learning_reset_at_ms"] == 2000
    assert "market_backtest_before_learning_reset" in report["reasons"]
    assert report["market_backtest"]["quality_passed"] is False
    assert "market_backtest_before_learning_reset" in report["market_backtest"]["quality_blockers"]


def test_learned_risk_guard_blocks_repeated_loss_factor(monkeypatch):
    monkeypatch.setattr(settings, "trade_attribution_enabled", True)
    monkeypatch.setattr(settings, "trade_learning_guard_enabled", True)
    monkeypatch.setattr(settings, "trade_attribution_use_replay", False)
    monkeypatch.setattr(settings, "trade_attribution_min_samples", 3)
    monkeypatch.setattr(settings, "trade_learning_guard_min_rule_samples", 3)
    monkeypatch.setattr(settings, "trade_attribution_block_win_rate", 0.5)
    monkeypatch.setattr(settings, "trade_attribution_block_profit_factor", 1.0)
    monkeypatch.setattr(
        learning_data_audit,
        "compact",
        lambda: {"can_hard_block_from_learning": True, "trust_level": "PRODUCTION", "reasons": []},
    )
    samples = []
    for idx in range(3):
        sample = sample_trade(side="LONG", pnl=-0.5, close_time=idx + 1)
        sample["wick_ratio"] = 0.9
        sample["radar"]["wick_ratio"] = 0.9
        samples.append(sample)
    monkeypatch.setattr(trade_attributor, "_samples", lambda: [trade_attributor._normalize_sample(s) for s in samples])
    item = high_quality_item(side="LONG")
    item.wick_ratio = 0.9
    report = learned_risk_guard.evaluate(item, None, recovery_mode=True)
    assert not report.allow_paper
    assert "learned_block:wick_high" in report.reasons
    assert "causal_factor_negative:timeframe_aligned" not in report.reasons
    assert report.hard_blocks

def test_trade_attributor_releases_pattern_block_after_recent_matched_recovery(monkeypatch):
    item = high_quality_item(symbol="ATTRRECOVERUSDT", side="LONG")
    row = item.asdict()
    factors = trade_attributor._factors(row, "LONG", None)
    pattern = trade_attributor._pattern_key(row, "LONG", None)
    now = now_ms()

    def matched_sample(pnl, close_time, reason):
        return {
            **row,
            "side": "LONG",
            "direction": "LONG",
            "pnl": pnl,
            "close_time": close_time,
            "close_reason": reason,
            "factors": factors,
            "pattern": pattern,
        }

    samples = [matched_sample(1.0, now - idx * 1000, "TP") for idx in range(3)]
    samples.extend(matched_sample(-1.0, now - 100000 - idx * 1000, "SL") for idx in range(6))
    monkeypatch.setattr(settings, "trade_attribution_enabled", True)
    monkeypatch.setattr(settings, "trade_attribution_min_samples", 3)
    monkeypatch.setattr(settings, "trade_attribution_block_win_rate", 0.50)
    monkeypatch.setattr(settings, "trade_attribution_block_profit_factor", 1.0)
    monkeypatch.setattr(trade_attributor, "_samples", lambda: samples)

    report = trade_attributor.evaluate(item, None)

    assert report.paper_ok is True
    assert "recent_matched_pattern_recovered" in report.reasons
    assert "causal_pattern_win_rate_low" not in report.reasons
    assert "causal_pattern_profit_factor_low" not in report.reasons

def test_learned_risk_guard_reviews_low_trust_negative_pattern_without_blocking_paper(monkeypatch):
    monkeypatch.setattr(settings, "trade_attribution_enabled", True)
    monkeypatch.setattr(settings, "trade_learning_guard_enabled", True)
    monkeypatch.setattr(
        learning_data_audit,
        "compact",
        lambda: {"can_hard_block_from_learning": False, "trust_level": "MEDIUM", "reasons": ["market_backtest_not_passing"]},
    )

    class NegativeAttribution:
        current_factors = ["wick_high", "flow_negative"]
        matched_samples = 24
        match_level = "relaxed_physical_structure"
        win_rate = 0.31
        profit_factor = 0.62
        pnl = -8.4
        paper_ok = False
        live_ok = False
        reasons = ["causal_pattern_win_rate_low", "causal_pattern_profit_factor_low"]
        advice = ["negative pattern"]

        def asdict(self):
            return {
                "matched_samples": self.matched_samples,
                "win_rate": self.win_rate,
                "profit_factor": self.profit_factor,
                "pnl": self.pnl,
                "reasons": self.reasons,
            }

    monkeypatch.setattr(trade_attributor, "evaluate", lambda item, plan: NegativeAttribution())

    report = learned_risk_guard.evaluate(high_quality_item(side="LONG"), None, recovery_mode=False)

    assert report.allow_paper is True
    assert report.allow_live is False
    assert report.severity == "REVIEW"
    assert "learning_data_not_production_grade" in report.reasons
    assert "causal_pattern_win_rate_low" in report.reasons

def test_learned_risk_guard_allows_positive_matched_pattern_over_factor_blocks(monkeypatch):
    monkeypatch.setattr(settings, "trade_attribution_enabled", True)
    monkeypatch.setattr(settings, "trade_learning_guard_enabled", True)
    monkeypatch.setattr(settings, "trade_attribution_min_samples", 8)
    monkeypatch.setattr(settings, "trade_attribution_block_win_rate", 0.5)
    monkeypatch.setattr(settings, "trade_attribution_block_profit_factor", 1.0)
    monkeypatch.setattr(
        learning_data_audit,
        "compact",
        lambda: {"can_hard_block_from_learning": True, "trust_level": "PRODUCTION", "reasons": []},
    )

    class PositiveAttribution:
        current_factors = ["wick_high", "taker_not_aligned"]
        matched_samples = 12
        match_level = "relaxed_physical_structure"
        win_rate = 0.583
        profit_factor = 1.93
        pnl = 3.81
        paper_ok = True
        live_ok = False
        reasons = ["causal_factor_negative:wick_high", "causal_factor_negative:taker_not_aligned"]
        advice = ["factor warning"]

        def asdict(self):
            return {
                "matched_samples": self.matched_samples,
                "win_rate": self.win_rate,
                "profit_factor": self.profit_factor,
                "pnl": self.pnl,
                "reasons": self.reasons,
            }

    monkeypatch.setattr(trade_attributor, "evaluate", lambda item, plan: PositiveAttribution())
    monkeypatch.setattr(
        learned_risk_guard,
        "_hard_blocks",
        lambda current_factors: [{"code": "wick_high", "advice": "block high wick"}],
    )

    report = learned_risk_guard.evaluate(high_quality_item(side="LONG"), None, recovery_mode=False)

    assert report.allow_paper is True
    assert report.severity == "PASS"
    assert not report.hard_blocks
    assert all(not reason.startswith("causal_factor_negative:") for reason in report.reasons)
    assert "learned_block:wick_high" not in report.reasons


def test_autotrader_low_trust_learning_review_reaches_strategy_generation(monkeypatch):
    symbol = "LOWTRUSTPAPERUSDT"
    cleanup_symbol(symbol)
    position_registry.open.clear()
    autotrader.executed_strategy_ids.clear()
    item = high_quality_item(symbol=symbol, side="LONG", price=100)
    item.rank = 1
    item.fund_confirm_count = 3
    generated = {"called": False}

    monkeypatch.setattr(settings, "trade_attribution_enabled", True)
    monkeypatch.setattr(settings, "trade_learning_guard_enabled", True)
    monkeypatch.setattr(settings, "auto_trading_use_active_strategy_filter", False)
    monkeypatch.setattr(settings, "max_open_positions", 1)
    monkeypatch.setattr(radar_engine, "top50", [item])
    monkeypatch.setattr(strategy_registry, "active", lambda: None)
    monkeypatch.setattr(performance_guard, "summary", lambda: {"recovery_mode": False, "pnl": 0, "trades": 0, "win_rate": 0, "recent_win_rate": 0, "loss_streak": 0})
    monkeypatch.setattr(performance_guard, "precheck_candidate", lambda candidate: (True, ""))
    monkeypatch.setattr(
        learning_data_audit,
        "compact",
        lambda: {"can_hard_block_from_learning": False, "trust_level": "LOW", "reasons": ["real_closed_samples_low"]},
    )

    class NegativeAttribution:
        current_factors = ["wick_high"]
        matched_samples = 32
        match_level = "relaxed_physical_structure"
        win_rate = 0.25
        profit_factor = 0.4
        pnl = -6.0
        paper_ok = False
        live_ok = False
        reasons = ["causal_pattern_win_rate_low", "causal_factor_negative:wick_high"]
        advice = ["negative pattern"]

        def asdict(self):
            return {
                "matched_samples": self.matched_samples,
                "win_rate": self.win_rate,
                "profit_factor": self.profit_factor,
                "pnl": self.pnl,
                "reasons": self.reasons,
            }

    monkeypatch.setattr(trade_attributor, "evaluate", lambda item_arg, plan: NegativeAttribution())
    monkeypatch.setattr(autotrader, "_candidate_batch", lambda performance: ([item], "strict"))
    monkeypatch.setattr(autotrader, "_market_data_ok", lambda: (True, ""))

    async def geometry_order(candidates, candidate_source, performance_context):
        return candidates, {}

    async def account_context(open_positions):
        return {}, {"equity": 1000, "available_balance": 1000, "open_positions": 0, "max_open_positions": 1, "trade_mode": "paper"}

    async def prepare_latest(latest_item, *, force_scan):
        return latest_item, {"scan_ok": True, "symbol_present_after_scan": True, "item_age_seconds": 0}

    async def generate_plan(plan_item, account, performance_context, candidate_source, paper_probe, paper_validation, selected_strategy, **kwargs):
        generated["called"] = True
        return StrategyPlan(
            strategy_id="low_trust_review_wait",
            action="WAIT",
            symbol=plan_item.symbol,
            side=plan_item.direction,
            entry_zone_low=0,
            entry_zone_high=0,
            ideal_entry_price=0,
            stop_loss=0,
            tp1=0,
            tp2=0,
            confidence=0,
            reason="review continued to strategy generation",
            wait_type="WAIT_FOR_CONFIRMATION",
        )

    monkeypatch.setattr(autotrader, "_geometry_supported_candidate_order", geometry_order)
    monkeypatch.setattr(autotrader, "_account_context", account_context)
    monkeypatch.setattr(autotrader, "_prepare_latest_item_for_ai", prepare_latest)
    monkeypatch.setattr(autotrader, "_pre_trade_price_ok", lambda report: (True, ""))
    monkeypatch.setattr(autotrader, "_ai_candidate_freshness_report", lambda *args, **kwargs: (True, {"reasons": []}))
    monkeypatch.setattr(autotrader, "_generate_strategy_plan", generate_plan)
    monkeypatch.setattr(strategy_validator, "validate", lambda plan: (True, ""))
    monkeypatch.setattr("backend.trading.autotrader.wait_manager.evaluate", lambda item_arg, plan: {"decision": "WAIT", "reason": "strategy_wait"})

    try:
        result = asyncio.run(autotrader._run_once_locked())

        assert generated["called"] is True
        assert result["results"][0]["decision"] == "WAIT"
        assert result["results"][0]["reason"] == "strategy_wait"
    finally:
        cleanup_symbol(symbol)


def test_learned_risk_guard_allows_proven_reverse_candidate(monkeypatch):
    monkeypatch.setattr(settings, "trade_attribution_enabled", True)
    monkeypatch.setattr(settings, "trade_learning_guard_enabled", True)
    monkeypatch.setattr(settings, "trade_learning_reverse_enabled", True)
    monkeypatch.setattr(settings, "trade_attribution_use_replay", False)
    monkeypatch.setattr(settings, "trade_attribution_min_samples", 3)
    monkeypatch.setattr(settings, "trade_learning_guard_min_rule_samples", 3)
    monkeypatch.setattr(settings, "trade_learning_reverse_min_confirmations", 6)
    monkeypatch.setattr(settings, "trade_learning_reverse_min_win_rate", 0.52)
    monkeypatch.setattr(settings, "trade_learning_reverse_min_profit_factor", 1.05)
    monkeypatch.setattr(settings, "trade_attribution_block_win_rate", 0.5)
    monkeypatch.setattr(settings, "trade_attribution_block_profit_factor", 1.0)
    monkeypatch.setattr(
        learning_data_audit,
        "compact",
        lambda: {"can_hard_block_from_learning": True, "trust_level": "PRODUCTION", "reasons": []},
    )
    samples = []
    for idx in range(3):
        bad_short = sample_trade(side="SHORT", pnl=-0.5, close_time=idx + 1)
        bad_short.update(
            {
                "fund_confirm_count": 0,
                "change_5m": 1.2,
                "change_15m": 2.1,
                "change_1h": 1.0,
                "taker_buy_ratio": 0.68,
                "taker_sell_ratio": 0.32,
                "depth_imbalance": 0.22,
                "sm_delta": 0.8,
                "wick_ratio": 0.25,
            }
        )
        bad_short["radar"].update({k: bad_short[k] for k in ("fund_confirm_count", "change_5m", "change_15m", "change_1h", "taker_buy_ratio", "taker_sell_ratio", "depth_imbalance", "sm_delta", "wick_ratio")})
        samples.append(bad_short)
    for idx in range(3):
        good_long = sample_trade(side="LONG", pnl=0.7, close_time=10 + idx)
        samples.append(good_long)
    monkeypatch.setattr(trade_attributor, "_samples", lambda: [trade_attributor._normalize_sample(s) for s in samples])
    item = high_quality_item(side="LONG")
    item.direction = "SHORT"
    item.fund_confirm_count = 0
    item.fake_breakout_risk = "LOW"
    reverse_item, report = learned_risk_guard.maybe_reverse_candidate(item, recovery_mode=True)
    assert reverse_item is not None
    assert reverse_item.direction == "LONG"
    assert report["allow_reverse"] is True
    assert report["reverse_confirmations"] >= 6

def test_strategy_quality_gate_uses_trade_attribution_to_block_bad_structure(monkeypatch):
    monkeypatch.setattr(settings, "trade_attribution_enabled", True)
    monkeypatch.setattr(settings, "trade_attribution_min_samples", 3)
    monkeypatch.setattr(settings, "trade_attribution_block_win_rate", 0.5)
    monkeypatch.setattr(settings, "trade_attribution_block_profit_factor", 1.0)
    samples = [sample_trade(side="LONG", pnl=-1.0, close_time=idx + 1) for idx in range(3)]
    monkeypatch.setattr(trade_attributor, "_samples", lambda: [trade_attributor._normalize_sample(s) for s in samples])
    item = high_quality_item(side="LONG")
    plan = StrategyPlan(
        strategy_id="attribution_block",
        action="OPEN_LONG",
        symbol=item.symbol,
        side="LONG",
        entry_zone_low=100,
        entry_zone_high=100,
        ideal_entry_price=100,
        stop_loss=99,
        tp1=101,
        tp2=103,
        confidence=90,
        reason="bad structure",
    )
    report = strategy_quality_gate.evaluate(item, plan)
    assert not report.paper_ok
    assert "causal_pattern_win_rate_low" in report.reasons
    assert report.attribution["matched_samples"] >= 3


def test_learning_risk_layers_disabled_do_not_allow_live(monkeypatch):
    item = high_quality_item(side="LONG")
    plan = StrategyPlan(
        strategy_id="disabled_risk_layers",
        action="OPEN_LONG",
        symbol=item.symbol,
        side="LONG",
        entry_zone_low=100,
        entry_zone_high=100,
        ideal_entry_price=100,
        stop_loss=99,
        tp1=101,
        tp2=103,
        confidence=90,
        reason="risk layers disabled",
    )
    monkeypatch.setattr(settings, "event_calibration_enabled", False)
    monkeypatch.setattr(settings, "trade_attribution_enabled", False)
    monkeypatch.setattr(settings, "trade_learning_guard_enabled", False)

    calibration = event_calibrator.evaluate(item, plan, heuristic_win_rate=0.8)
    attribution = trade_attributor.evaluate(item, plan)
    learned = learned_risk_guard.evaluate(item, plan)
    quality = strategy_quality_gate.evaluate(item, plan)

    assert calibration.live_ok is False
    assert attribution.live_ok is False
    assert learned.allow_live is False
    assert quality.live_ok is False
    assert "event_calibration_disabled" in quality.reasons
    assert "trade_attribution_disabled" in quality.reasons


def test_auto_trading_risk_model_does_not_open_live_when_learning_risk_disabled(monkeypatch):
    monkeypatch.setattr(settings, "require_codex_strategy_for_entry", False)
    monkeypatch.setattr(settings, "event_calibration_enabled", False)
    monkeypatch.setattr(settings, "trade_attribution_enabled", False)
    monkeypatch.setattr(settings, "trade_learning_guard_enabled", False)
    monkeypatch.setattr(performance_guard, "_closed_rows", lambda: [])
    item = high_quality_item()
    plan = StrategyPlan(
        strategy_id="risk_disabled_live",
        action="OPEN_LONG",
        symbol=item.symbol,
        side="LONG",
        entry_zone_low=100,
        entry_zone_high=100,
        ideal_entry_price=100,
        stop_loss=99,
        tp1=101,
        tp2=103,
        confidence=90,
        reason="risk disabled",
    )

    exec_plan = auto_trading_risk_model.decide(
        item,
        plan,
        {"equity": 1000, "loss_streak": 0, "open_positions": 0, "max_open_positions": 3, "trade_mode": "live"},
        {"market_heat": 60, "volatility_regime": "normal"},
    )

    assert exec_plan.decision == "PAPER_ONLY"
    assert plan.raw["quality_gate"]["live_ok"] is False
    assert plan.raw["learned_guard"]["allow_live"] is False


def test_test_fixture_keeps_learning_risk_enabled_by_default():
    assert settings.event_calibration_enabled is True
    assert settings.trade_attribution_enabled is True
    assert settings.trade_learning_guard_enabled is True


def test_auto_trading_risk_model_requires_quality_gate(monkeypatch):
    monkeypatch.setattr(settings, "require_codex_strategy_for_entry", False)
    monkeypatch.setattr(performance_guard, "_closed_rows", lambda: [])
    monkeypatch.setattr(
        "backend.ai_strategy.dynamic_trade_model.learned_risk_guard.evaluate",
        lambda *args, **kwargs: SimpleNamespace(
            allow_paper=True,
            allow_live=True,
            reasons=[],
            matched_samples=30,
            win_rate=0.62,
            profit_factor=1.35,
            pnl=5.0,
            asdict=lambda: {
                "allow_paper": True,
                "allow_live": True,
                "reasons": [],
                "matched_samples": 30,
                "win_rate": 0.62,
                "profit_factor": 1.35,
                "pnl": 5.0,
            },
        ),
    )
    monkeypatch.setattr(
        "backend.ai_strategy.dynamic_trade_model.strategy_quality_gate.evaluate",
        lambda *args, **kwargs: SimpleNamespace(
            paper_ok=True,
            live_ok=True,
            tp2_r=2.8,
            cost_r=0.12,
            estimated_win_rate=0.62,
            expected_r=0.38,
            reasons=[],
            asdict=lambda: {
                "paper_ok": True,
                "live_ok": True,
                "tp2_r": 2.8,
                "cost_r": 0.12,
                "estimated_win_rate": 0.62,
                "expected_r": 0.38,
                "reasons": [],
            },
        ),
    )
    item = high_quality_item()
    good_plan = StrategyPlan(
        strategy_id="good",
        action="OPEN_LONG",
        symbol=item.symbol,
        side="LONG",
        entry_zone_low=100,
        entry_zone_high=100,
        ideal_entry_price=100,
        stop_loss=99,
        tp1=101,
        tp2=102.8,
        confidence=78,
        reason="good rr",
    )
    exec_plan = auto_trading_risk_model.decide(
        item,
        good_plan,
        {"equity": 1000, "loss_streak": 0, "open_positions": 0, "max_open_positions": 3, "trade_mode": "live"},
        {"market_heat": 60, "volatility_regime": "normal"},
    )
    assert exec_plan.decision == "OPEN"
    assert good_plan.raw["quality_gate"]["live_ok"] is True
    assert exec_plan.dynamic_margin >= settings.trade_min_margin_usdt
    assert exec_plan.notional >= settings.trade_min_notional_usdt
    assert exec_plan.risk_usdt > 0


def test_auto_trading_risk_model_blocks_live_when_account_cannot_trade(monkeypatch):
    monkeypatch.setattr(settings, "require_codex_strategy_for_entry", False)
    monkeypatch.setattr(performance_guard, "_closed_rows", lambda: [])
    monkeypatch.setattr(
        "backend.ai_strategy.dynamic_trade_model.learned_risk_guard.evaluate",
        lambda *args, **kwargs: SimpleNamespace(
            allow_paper=True,
            allow_live=True,
            reasons=[],
            matched_samples=30,
            win_rate=0.62,
            profit_factor=1.35,
            pnl=5.0,
            asdict=lambda: {
                "allow_paper": True,
                "allow_live": True,
                "reasons": [],
                "matched_samples": 30,
                "win_rate": 0.62,
                "profit_factor": 1.35,
                "pnl": 5.0,
            },
        ),
    )
    monkeypatch.setattr(
        "backend.ai_strategy.dynamic_trade_model.strategy_quality_gate.evaluate",
        lambda *args, **kwargs: SimpleNamespace(
            paper_ok=True,
            live_ok=True,
            tp2_r=2.8,
            cost_r=0.12,
            estimated_win_rate=0.62,
            expected_r=0.38,
            reasons=[],
            asdict=lambda: {
                "paper_ok": True,
                "live_ok": True,
                "tp2_r": 2.8,
                "cost_r": 0.12,
                "estimated_win_rate": 0.62,
                "expected_r": 0.38,
                "reasons": [],
            },
        ),
    )
    item = high_quality_item()
    plan = StrategyPlan(
        strategy_id="account_block",
        action="OPEN_LONG",
        symbol=item.symbol,
        side="LONG",
        entry_zone_low=100,
        entry_zone_high=100,
        ideal_entry_price=100,
        stop_loss=99,
        tp1=101,
        tp2=102.8,
        confidence=78,
        reason="good rr",
    )

    exec_plan = auto_trading_risk_model.decide(
        item,
        plan,
        {
            "equity": 1000,
            "available_balance": 1000,
            "loss_streak": 0,
            "open_positions": 0,
            "max_open_positions": 3,
            "trade_mode": "live",
            "execution_context": "live",
            "can_trade": False,
        },
        {"market_heat": 60, "volatility_regime": "normal"},
    )

    assert exec_plan.decision == "OBSERVE"
    assert "canTrade=false" in exec_plan.reason


def test_auto_trading_risk_model_uses_learned_allow_live_for_open(monkeypatch):
    monkeypatch.setattr(settings, "require_codex_strategy_for_entry", False)
    monkeypatch.setattr(performance_guard, "_closed_rows", lambda: [])
    monkeypatch.setattr(
        "backend.ai_strategy.dynamic_trade_model.learned_risk_guard.evaluate",
        lambda *args, **kwargs: SimpleNamespace(
            allow_paper=True,
            allow_live=False,
            reasons=["live_sample_count_low"],
            matched_samples=8,
            win_rate=0.62,
            profit_factor=1.35,
            pnl=5.0,
            asdict=lambda: {
                "allow_paper": True,
                "allow_live": False,
                "reasons": ["live_sample_count_low"],
                "matched_samples": 8,
                "win_rate": 0.62,
                "profit_factor": 1.35,
                "pnl": 5.0,
            },
        ),
    )
    monkeypatch.setattr(
        "backend.ai_strategy.dynamic_trade_model.strategy_quality_gate.evaluate",
        lambda *args, **kwargs: SimpleNamespace(
            paper_ok=True,
            live_ok=True,
            tp2_r=2.8,
            cost_r=0.12,
            estimated_win_rate=0.62,
            expected_r=0.38,
            reasons=[],
            asdict=lambda: {
                "paper_ok": True,
                "live_ok": True,
                "tp2_r": 2.8,
                "cost_r": 0.12,
                "estimated_win_rate": 0.62,
                "expected_r": 0.38,
                "reasons": [],
            },
        ),
    )
    item = high_quality_item()
    plan = StrategyPlan(
        strategy_id="learned_live_block",
        action="OPEN_LONG",
        symbol=item.symbol,
        side="LONG",
        entry_zone_low=100,
        entry_zone_high=100,
        ideal_entry_price=100,
        stop_loss=99,
        tp1=101,
        tp2=102.8,
        confidence=78,
        reason="quality ok but live learning not ok",
    )

    exec_plan = auto_trading_risk_model.decide(
        item,
        plan,
        {
            "equity": 1000,
            "available_balance": 1000,
            "loss_streak": 0,
            "open_positions": 0,
            "max_open_positions": 3,
            "trade_mode": "live",
            "execution_context": "live",
            "can_trade": True,
        },
        {"market_heat": 60, "volatility_regime": "normal"},
    )

    assert exec_plan.decision == "PAPER_ONLY"
    assert plan.raw["quality_gate"]["live_ok"] is True
    assert plan.raw["learned_guard"]["allow_live"] is False

def test_auto_trading_risk_model_requires_codex_strategy_for_entry(monkeypatch):
    monkeypatch.setattr(settings, "require_codex_strategy_for_entry", True)
    item = high_quality_item()
    local_plan = rule_strategy_generator.generate(item)

    exec_plan = auto_trading_risk_model.decide(
        item,
        local_plan,
        {"equity": 1000, "loss_streak": 0, "open_positions": 0, "max_open_positions": 3, "trade_mode": "paper"},
        {"market_heat": 60, "volatility_regime": "normal"},
    )

    assert exec_plan.decision == "OBSERVE"
    assert "Codex-generated strategy required" in exec_plan.reason

def test_auto_trading_risk_model_rejects_stale_entry_zone(monkeypatch):
    monkeypatch.setattr(settings, "require_codex_strategy_for_entry", False)
    monkeypatch.setattr(performance_guard, "_closed_rows", lambda: [])
    item = high_quality_item(price=102)
    plan = StrategyPlan(
        strategy_id="stale",
        action="OPEN_LONG",
        symbol=item.symbol,
        side="LONG",
        entry_zone_low=99.9,
        entry_zone_high=100.1,
        ideal_entry_price=100,
        stop_loss=99,
        tp1=101,
        tp2=102.8,
        confidence=78,
        reason="stale entry",
    )
    exec_plan = auto_trading_risk_model.decide(
        item,
        plan,
        {"equity": 1000, "available_balance": 1000, "loss_streak": 0, "open_positions": 0, "max_open_positions": 3, "trade_mode": "paper"},
        {"market_heat": 60, "volatility_regime": "normal"},
    )
    assert exec_plan.decision == "OBSERVE"
    assert "outside entry zone" in exec_plan.reason

def test_auto_trading_risk_model_does_not_bypass_guard_in_recovery(monkeypatch):
    monkeypatch.setattr(settings, "require_codex_strategy_for_entry", False)
    monkeypatch.setattr(settings, "auto_trading_use_performance_guard", False)
    monkeypatch.setattr(performance_guard, "summary", lambda: {"recovery_mode": True})
    monkeypatch.setattr(
        performance_guard,
        "evaluate",
        lambda item, plan, quality: PerformanceGuardReport(
            allow=False,
            recovery_mode=True,
            reasons=["recovery_score_low"],
            global_trades=40,
            global_win_rate=0.3,
            global_pnl=-10,
            recent_win_rate=0.2,
            loss_streak=4,
            symbol_side_trades=0,
            symbol_side_win_rate=0,
            symbol_side_pnl=0,
            direction_confirmations=3,
        ),
    )
    item = high_quality_item(price=100)
    plan = StrategyPlan(
        strategy_id="guarded",
        action="OPEN_LONG",
        symbol=item.symbol,
        side="LONG",
        entry_zone_low=99.9,
        entry_zone_high=100.1,
        ideal_entry_price=100,
        stop_loss=99,
        tp1=101,
        tp2=102.8,
        confidence=78,
        reason="guarded",
    )
    exec_plan = auto_trading_risk_model.decide(
        item,
        plan,
        {"equity": 1000, "available_balance": 1000, "loss_streak": 0, "open_positions": 0, "max_open_positions": 3, "trade_mode": "paper", "execution_context": "paper_closed_loop"},
        {"market_heat": 60, "volatility_regime": "normal"},
    )
    assert exec_plan.decision == "OBSERVE"
    assert "performance guard rejected" in exec_plan.reason

def test_paper_probe_bypasses_red_recovery_for_paper_sampling(monkeypatch):
    monkeypatch.setattr(settings, "live_trading_enabled", False)
    monkeypatch.setattr(settings, "trade_mode", "live")
    monkeypatch.setattr(settings, "trade_min_net_profit_usdt", 0.2)
    monkeypatch.setattr(performance_guard, "summary", lambda: {"recovery_mode": True, "trades": 40, "win_rate": 0.34, "pnl": -8, "recent_win_rate": 0.25, "loss_streak": 5})
    item = high_quality_item(symbol="PROBEUSDT", side="LONG", price=100)
    item.score = 34
    item.fund_confirm_count = 1
    plan = rule_strategy_generator.generate_probe(item)
    plan.raw["provider"] = "codex_cli"

    exec_plan = auto_trading_risk_model.decide(
        item,
        plan,
        {
            "equity": 1000,
            "available_balance": 1000,
            "loss_streak": 5,
            "open_positions": 0,
            "max_open_positions": 1,
            "trade_mode": "paper",
            "execution_context": "paper_closed_loop",
        },
        {"market_heat": 45, "volatility_regime": "normal"},
        paper_probe=True,
    )

    assert exec_plan.decision == "PAPER_ONLY"
    assert "paper_probe" in exec_plan.reason
    assert plan.raw["performance_guard"]["allow"] is True
    assert "bypassed_for_paper_probe_sampling" in plan.raw["performance_guard"]["reasons"]

def test_strategy_plan_requires_complete_contract_for_open():
    item = high_quality_item(symbol="CONTRACTUSDT", side="LONG", price=100)
    plan = rule_strategy_generator.generate(item)

    ok, reason = strategy_validator.validate(plan)

    assert ok
    contract = plan.raw["strategy_contract"]
    assert contract["hypothesis"]
    assert contract["signal"]["entry"]
    assert contract["risk"]["max_loss"]["defined_before_entry"] is True
    assert contract["execution"]["fill_assumption"]
    assert contract["position_lifecycle"]["states"]
    assert "PROTECTING" in contract["position_lifecycle"]["states"]
    assert contract["hold_logic"]["continue_holding_if"]
    assert contract["reduce_logic"]["reduce_if"]
    assert contract["add_logic"]["max_adds"] == 0
    assert contract["exit_logic"]["core_exit_only_if"]
    assert {"MFE", "MAE", "R_multiple"}.issubset(set(contract["review_metrics"]))
    assert len(contract["entry_conditions"]) >= 3
    assert contract["invalidation"]["hard_stop"] == plan.stop_loss
    assert contract["allowed_stages"]["live"] is False
    assert contract["research_review"]["role_a_researcher"]
    assert contract["research_review"]["role_b_risk_officer"]
    assert contract["cyqnt_feature_enhancement"]["symbol"] == item.symbol
    assert contract["cyqnt_feature_enhancement"]["estimated_win_rate"] > 0
    assert contract["learning_tags"]["cyqnt_estimated_win_rate"] == contract["cyqnt_feature_enhancement"]["estimated_win_rate"]
    assert "cyqnt_feature_score" in contract["signal"]["evidence"][-3]


def test_strategy_contract_allows_disabled_add_logic_with_empty_add_if():
    item = high_quality_item(symbol="NOADDUSDT", side="LONG", price=100)
    plan = rule_strategy_generator.generate(item)
    contract = dict(plan.raw["strategy_contract"])
    contract["add_logic"] = {
        "add_if": [],
        "max_adds": 0,
        "reason": "scale-in disabled until forward data proves it improves expectancy",
    }

    ok, reasons = contract_quality(contract)

    assert ok
    assert "contract_add_logic_incomplete" not in reasons


def test_strategy_contract_hard_stop_zero_is_not_missing():
    item = high_quality_item(symbol="ZEROSTOPUSDT", side="LONG", price=100)
    plan = rule_strategy_generator.generate(item)
    contract = dict(plan.raw["strategy_contract"])
    contract["invalidation"] = {
        **contract["invalidation"],
        "hard_stop": 0,
    }

    ok, reasons = contract_quality(contract)

    assert ok
    assert "contract_invalidation_incomplete" not in reasons


def test_strategy_context_includes_cyqnt_evidence_layer():
    item = high_quality_item(symbol="CTXCYQNTUSDT", side="LONG", price=100)

    context = context_compressor.build_strategy_context(item)
    cyqnt = context["cyqnt_feature_enhancement"]

    assert cyqnt["symbol"] == item.symbol
    assert cyqnt["cyqnt_available"] is True
    assert cyqnt["feature_score"] > 0
    assert cyqnt["estimated_win_rate"] > 0
    assert cyqnt["role"].startswith("local evidence layer")
    assert "decide whether the setup has enough edge to produce an OPEN plan" in cyqnt["must_use_for"]
    assert "ai_strategy_quality_feedback" in context
    assert context["ai_strategy_quality_feedback"]["summary"]["tracked_strategies"] >= 0
    assert context["market_freshness"]["item_ts_ms"] == item.ts_ms
    assert "latest refreshed radar snapshot" in context["market_freshness"]["instruction"]


def test_strategy_context_includes_universal_anomaly_model():
    item = high_quality_item(symbol="CTXUNIVERSALUSDT", side="LONG", price=100)
    item.score_features = {"universal_anomaly_model": universal_anomaly_model.predict(item)}

    context = context_compressor.build_strategy_context(item)
    model = context["universal_anomaly_model"]

    assert model["model"].startswith("universal_anomaly")
    assert model["direction"] == "LONG"
    assert model["probabilities"]["LONG"] > model["probabilities"]["SHORT"]
    assert model["role"].startswith("coin-agnostic microstructure")
    assert "direction probability agrees with side_bias" in model["must_check"]


def test_strategy_research_prompts_enforce_signal_risk_execution():
    for text in (CODEX_PROMPT, STRATEGY_QA_SYSTEM_PROMPT):
        assert "market hypothesis" in text
        assert "signal" in text
        assert "risk" in text
        assert "execution" in text
        assert "hold" in text or "position_lifecycle" in text
        assert "MFE" in text
        assert "MAE" in text
        assert "Role A" in text
        assert "Role B" in text
    assert "cyqnt_feature_enhancement" in CODEX_PROMPT
    assert "universal_anomaly_model" in CODEX_PROMPT
    assert "coin-agnostic microstructure confirmation" in CODEX_PROMPT
    assert "generation_gate" in CODEX_PROMPT
    assert "allow_open_plan is false" in CODEX_PROMPT
    assert "production_acceptance strict mode" in CODEX_PROMPT
    assert "downstream quality gates, risk_model, and live_readiness" in CODEX_PROMPT
    assert "not as an automatic veto" not in CODEX_PROMPT
    assert "prefer a constrained paper-only OPEN over WAIT" not in CODEX_PROMPT
    assert "extreme current wick noise as a WAIT condition" in CODEX_PROMPT
    assert "historical wick spikes may be balanced" in CODEX_PROMPT

def test_trading_lab_scaffold_exists():
    root = Path(__file__).resolve().parents[1] / "trading_lab"
    required = [
        "README.md",
        "principles.md",
        "strategy_template.md",
        "risk_rules.md",
        "position_lifecycle.md",
        "codex_strategy_research_prompt.md",
        "backtester/run_research.py",
        "data/README.md",
        "reports/README.md",
    ]
    for relative in required:
        assert (root / relative).exists()

def test_strategy_validator_rejects_open_without_contract():
    item = high_quality_item(symbol="NOCONTRACTUSDT", side="LONG", price=100)
    plan = StrategyPlan(
        strategy_id="missing_contract",
        action="OPEN_LONG",
        symbol=item.symbol,
        side="LONG",
        entry_zone_low=100,
        entry_zone_high=100,
        ideal_entry_price=100,
        stop_loss=99,
        tp1=101,
        tp2=103,
        confidence=80,
        reason="missing contract",
    )

    ok, reason = strategy_validator.validate(plan)

    assert not ok
    assert "strategy_contract_missing" in reason

def test_closed_trade_preserves_strategy_contract_for_learning(monkeypatch):
    symbol = "CONTRACTMEMUSDT"
    cleanup_symbol(symbol)
    position_registry.open.clear()
    monkeypatch.setattr(settings, "require_codex_strategy_for_entry", False)
    monkeypatch.setattr(performance_guard, "_closed_rows", lambda: [])
    try:
        item = high_quality_item(symbol=symbol, side="LONG", price=100)
        plan = rule_strategy_generator.generate(item)
        exec_plan = auto_trading_risk_model.decide(
            item,
            plan,
            {"equity": 1000, "available_balance": 1000, "loss_streak": 0, "open_positions": 0, "max_open_positions": 1, "trade_mode": "paper"},
            {"market_heat": 60, "volatility_regime": "normal"},
        )
        p = __import__("asyncio").run(paper_executor.open_position("scan_contract", plan.strategy_id, item.score, exec_plan))
        closed = position_manager.close_position(p, "TEST_CLOSE", exit_price=101)

        assert p.strategy_contract["strategy_kind"] == "radar_event_followthrough"
        assert closed.strategy_contract["learning_tags"]["symbol"] == symbol
    finally:
        cleanup_symbol(symbol)
        cleanup_acceptance_strategies()


def test_ai_strategy_feedback_records_open_ai_plan(monkeypatch):
    store = {}

    def save_strategy(strategy):
        store[strategy["strategy_id"]] = dict(strategy)
        return store[strategy["strategy_id"]]

    monkeypatch.setattr(strategy_registry, "get", lambda strategy_id: store.get(strategy_id))
    monkeypatch.setattr(strategy_registry, "save", save_strategy)

    item = high_quality_item(symbol="AIFEEDBACKUSDT", side="LONG", price=100)
    plan = rule_strategy_generator.generate(item)
    plan.strategy_id = "deepseek_feedback_open"
    plan.raw = {**plan.raw, "provider": "deepseek", "model": "deepseek-v4-pro"}
    exec_plan = feedback_exec_plan(plan)
    position = feedback_position(plan.strategy_id, item.symbol, position_id="pos_ai_feedback_open")

    out = ai_strategy_feedback.record_open(
        plan=plan,
        item=item,
        exec_plan=exec_plan,
        position=position,
        candidate_source="paper_top",
        paper_validation=True,
    )

    stored = store[plan.strategy_id]
    assert out["recorded"] is True
    assert stored["source"] == "ai_generated_deepseek"
    assert stored["status"] == "WATCH"
    assert stored["metrics"]["opened"] == 1
    assert stored["metrics"]["trades"] == 0
    assert stored["filters"]["allowed_sides"] == ["LONG"]
    assert stored["last_plan"]["action"] == "OPEN_LONG"
    assert stored["last_cyqnt_feature"]["symbol"] == item.symbol
    assert stored["last_cyqnt_feature"]["estimated_win_rate"] > 0
    assert stored["last_signal"]["cyqnt_feature_enhancement"]["symbol"] == item.symbol
    assert stored["filters"]["cyqnt_reference"]["estimated_win_rate"] == stored["last_cyqnt_feature"]["estimated_win_rate"]


def test_ai_strategy_feedback_records_local_rule_paper_loop(monkeypatch):
    store = {}

    def save_strategy(strategy):
        store[strategy["strategy_id"]] = dict(strategy)
        return store[strategy["strategy_id"]]

    monkeypatch.setattr(strategy_registry, "get", lambda strategy_id: store.get(strategy_id))
    monkeypatch.setattr(strategy_registry, "save", save_strategy)

    item = high_quality_item(symbol="RULEFEEDBACKUSDT", side="LONG", price=100)
    plan = rule_strategy_generator.generate_probe(item)
    plan.strategy_id = "rule_feedback_closed"
    plan.raw = {**plan.raw, "provider": "rule", "model": "local_rule_strategy"}
    position = feedback_position(plan.strategy_id, item.symbol, position_id="pos_rule_feedback_closed")

    opened = ai_strategy_feedback.record_open(
        plan=plan,
        item=item,
        exec_plan=feedback_exec_plan(plan),
        position=position,
        candidate_source="acceptance_controlled_paper_cycle",
        paper_validation=True,
    )
    closed = ClosedPosition(
        position_id=position.position_id,
        strategy_id=plan.strategy_id,
        symbol=item.symbol,
        side="LONG",
        entry_price=100,
        exit_price=103,
        quantity=1,
        margin=20,
        pnl=2.4,
        roi=12,
        close_reason="ACCEPTANCE_TP2",
        score_at_entry=item.score,
        open_time=position.open_time,
        close_time=position.open_time + 120000,
        source_signal_id="scan_rule_feedback_closed",
        notional=100,
        gross_pnl=3,
        fee=0.08,
        risk_usdt=1,
        risk_pct=1,
        strategy_contract=plan.raw["strategy_contract"],
        mfe=2.8,
        mae=-0.2,
        mfe_r=2.8,
        mae_r=-0.2,
        hold_time_ms=120000,
    )
    closed_out = ai_strategy_feedback.record_close(closed)
    stored = store[plan.strategy_id]

    assert opened["recorded"] is True
    assert closed_out["recorded"] is True
    assert stored["source"] == "ai_generated_rule"
    assert stored["provider"] == "rule"
    assert stored["metrics"]["trades"] == 1
    assert stored["forward"]["samples"][0]["close_reason"] == "ACCEPTANCE_TP2"


def test_ai_strategy_feedback_records_decision_observation_without_trade_sample():
    symbol = "AIOBSUSDT"
    with db.conn() as conn:
        conn.execute("DELETE FROM ai_decision_observations WHERE symbol=?", (symbol,))
        before_strategy = conn.execute(
            "SELECT COUNT(*) AS n FROM evolved_strategies WHERE strategy_id=?",
            ("codex_wait_observation",),
        ).fetchone()["n"]

    item = high_quality_item(symbol=symbol, side="LONG", price=100)
    plan = StrategyPlan(
        strategy_id="codex_wait_observation",
        action="WAIT",
        symbol=symbol,
        side="NEUTRAL",
        entry_zone_low=0,
        entry_zone_high=0,
        ideal_entry_price=0,
        stop_loss=0,
        tp1=0,
        tp2=0,
        confidence=0,
        reason="wait for cleaner confirmation",
        wait_type="WAIT_FOR_CONFIRMATION",
        raw={"provider": "codex_cli", "model": "gpt-5.5"},
    )

    try:
        out = ai_strategy_feedback.record_observation(
            item=item,
            plan=plan,
            decision="KEEP_WAITING",
            reason="wait_active",
            candidate_source="paper_top",
            stage="ai_wait",
            paper_validation=True,
        )
        context = ai_strategy_feedback.compact_context(item)
        observations = [
            row for row in ai_strategy_feedback.decision_observations(limit=20) if row.get("symbol") == symbol
        ]
        with db.conn() as conn:
            after_strategy = conn.execute(
                "SELECT COUNT(*) AS n FROM evolved_strategies WHERE strategy_id=?",
                ("codex_wait_observation",),
            ).fetchone()["n"]

        assert out["recorded"] is True
        assert observations
        assert observations[0]["sample_type"] == "decision_observation_not_trade_outcome"
        assert context["decision_observations"]["same_symbol_side_count"] >= 1
        assert context["decision_observations"]["current_candidate_repeat_risks"][0]["reason"] == "wait_active"
        assert after_strategy == before_strategy
    finally:
        with db.conn() as conn:
            conn.execute("DELETE FROM ai_decision_observations WHERE symbol=?", (symbol,))


def test_ai_strategy_feedback_closes_loop_after_trade_result(monkeypatch):
    store = {}

    def save_strategy(strategy):
        store[strategy["strategy_id"]] = dict(strategy)
        return store[strategy["strategy_id"]]

    monkeypatch.setattr(strategy_registry, "get", lambda strategy_id: store.get(strategy_id))
    monkeypatch.setattr(strategy_registry, "save", save_strategy)
    monkeypatch.setattr(settings, "evolve_min_holdout_trades", 1)
    monkeypatch.setattr(settings, "evolve_min_holdout_win_rate", 0.5)
    monkeypatch.setattr(settings, "evolve_min_profit_factor", 1.05)
    monkeypatch.setattr(settings, "evolve_min_net_pnl", 0.0)

    item = high_quality_item(symbol="AICLOSEDUSDT", side="LONG", price=100)
    plan = rule_strategy_generator.generate(item)
    plan.strategy_id = "deepseek_feedback_closed"
    plan.raw = {**plan.raw, "provider": "deepseek", "model": "deepseek-v4-pro"}
    position = feedback_position(plan.strategy_id, item.symbol, position_id="pos_ai_feedback_closed")

    ai_strategy_feedback.record_open(
        plan=plan,
        item=item,
        exec_plan=feedback_exec_plan(plan),
        position=position,
        candidate_source="paper_top",
        paper_validation=True,
    )
    closed = ClosedPosition(
        position_id=position.position_id,
        strategy_id=plan.strategy_id,
        symbol=item.symbol,
        side="LONG",
        entry_price=100,
        exit_price=103,
        quantity=1,
        margin=20,
        pnl=2.4,
        roi=12,
        close_reason="TP2",
        score_at_entry=item.score,
        open_time=position.open_time,
        close_time=position.open_time + 120000,
        source_signal_id="scan_ai_feedback_closed",
        notional=100,
        gross_pnl=3,
        fee=0.08,
        risk_usdt=1,
        risk_pct=1,
        strategy_contract=plan.raw["strategy_contract"],
        mfe=2.8,
        mae=-0.2,
        mfe_r=2.8,
        mae_r=-0.2,
        hold_time_ms=120000,
    )

    out = ai_strategy_feedback.record_close(closed)
    stored = store[plan.strategy_id]

    assert out["recorded"] is True
    assert stored["status"] == "PASS"
    assert stored["metrics"]["eligible"] is True
    assert stored["metrics"]["trades"] == 1
    assert stored["metrics"]["win_rate"] == 1.0
    assert stored["metrics"]["pnl"] == 2.4
    assert stored["forward"]["samples"][0]["close_reason"] == "TP2"
    assert stored["forward"]["samples"][0]["cyqnt_feature_enhancement"]["symbol"] == item.symbol


def test_ai_strategy_feedback_quarantines_price_source_stale_close(monkeypatch):
    store = {}

    def save_strategy(strategy):
        store[strategy["strategy_id"]] = dict(strategy)
        return store[strategy["strategy_id"]]

    monkeypatch.setattr(strategy_registry, "get", lambda strategy_id: store.get(strategy_id))
    monkeypatch.setattr(strategy_registry, "save", save_strategy)

    item = high_quality_item(symbol="AISTALEUSDT", side="LONG", price=100)
    plan = rule_strategy_generator.generate(item)
    plan.strategy_id = "codex_feedback_stale"
    plan.raw = {**plan.raw, "provider": "codex_cli", "model": "gpt-5.5"}
    position = feedback_position(plan.strategy_id, item.symbol, position_id="pos_ai_feedback_stale")

    ai_strategy_feedback.record_open(
        plan=plan,
        item=item,
        exec_plan=feedback_exec_plan(plan),
        position=position,
        candidate_source="paper_top",
        paper_validation=True,
    )
    closed = ClosedPosition(
        position_id=position.position_id,
        strategy_id=plan.strategy_id,
        symbol=item.symbol,
        side="LONG",
        entry_price=100,
        exit_price=99,
        quantity=1,
        margin=20,
        pnl=-1,
        roi=-5,
        close_reason="PRICE_SOURCE_STALE_RECONCILE",
        score_at_entry=item.score,
        open_time=position.open_time,
        close_time=position.open_time + 120000,
        source_signal_id="scan_ai_feedback_stale",
        notional=100,
        gross_pnl=-1,
        fee=0.08,
        risk_usdt=1,
        risk_pct=1,
        strategy_contract=plan.raw["strategy_contract"],
        hold_time_ms=120000,
    )

    out = ai_strategy_feedback.record_close(closed)
    stored = store[plan.strategy_id]

    assert out["recorded"] is False
    assert out["reason"] == "non_learning_close_reason"
    assert stored["status"] == "QUARANTINED"
    assert stored["metrics"]["opened"] == 0
    assert stored["metrics"]["open"] == 0
    assert stored["metrics"]["trades"] == 0
    assert stored["forward"]["non_learning_position_ids"] == [position.position_id]
    assert stored["forward"]["samples"] == []


def test_ai_strategy_feedback_excludes_stale_closed_position_fallback(monkeypatch):
    item = high_quality_item(symbol="AISTALEFALLBACKUSDT", side="LONG", price=100)
    plan = rule_strategy_generator.generate(item)
    strategy = {
        "strategy_id": "codex_stale_fallback",
        "source": "ai_generated_codex_cli",
        "status": "WATCH",
        "provider": "codex_cli",
        "strategy_contract": plan.raw["strategy_contract"],
        "last_signal": item.asdict(),
        "forward": {"samples": [], "open_position_ids": [], "closed_position_ids": []},
    }
    monkeypatch.setattr(strategy_registry, "list", lambda limit=100: [strategy])
    monkeypatch.setattr(
        position_registry,
        "list_closed",
        lambda limit=500: [
            {
                "position_id": "pos_stale_fallback",
                "strategy_id": strategy["strategy_id"],
                "symbol": item.symbol,
                "side": "LONG",
                "pnl": -1,
                "close_reason": "PRICE_SOURCE_STALE_RECONCILE",
                "strategy_contract": plan.raw["strategy_contract"],
                "close_time": now_ms(),
            }
        ],
    )

    summary = ai_strategy_feedback.quality_summary()

    assert summary["tracked_strategies"] == 1
    assert summary["closed_samples"] == 0
    assert summary["pnl"] == 0


def test_ai_strategy_feedback_context_flags_losing_ai_bucket(monkeypatch):
    item = high_quality_item(symbol="LOSSCTXUSDT", side="LONG", price=100)
    plan = rule_strategy_generator.generate(item)
    plan.strategy_id = "codex_losing_feedback"
    contract = plan.raw["strategy_contract"]
    strategy = {
        "strategy_id": plan.strategy_id,
        "source": "ai_generated_codex_cli",
        "status": "REJECTED",
        "provider": "codex_cli",
        "strategy_contract": contract,
        "last_signal": item.asdict(),
        "forward": {
            "samples": [
                {"symbol": item.symbol, "side": "LONG", "pnl": -1.0, "close_reason": "SL", "close_time": 3},
                {"symbol": item.symbol, "side": "LONG", "pnl": -0.8, "close_reason": "SL", "close_time": 2},
                {"symbol": item.symbol, "side": "LONG", "pnl": -0.6, "close_reason": "TIME_STOP", "close_time": 1},
            ]
        },
        "metrics": {"trades": 3, "win_rate": 0.0, "profit_factor": 0.0, "pnl": -2.4, "eligible": False},
    }
    monkeypatch.setattr(strategy_registry, "list", lambda limit=100: [strategy])

    report = ai_strategy_feedback.evaluate_candidate(item)
    context = context_compressor.build_strategy_context(item)

    assert report["quality_bias"] == "AVOID_REPEAT"
    assert report["avoid_repeating"][0]["name"] == "exact_symbol_side"
    assert report["generation_gate"]["allow_open_plan"] is False
    assert "avoid_repeating" in report["generation_gate"]["reasons"]
    assert context["ai_strategy_quality_feedback"]["candidate_feedback"]["quality_bias"] == "AVOID_REPEAT"
    assert context["ai_strategy_quality_feedback"]["candidate_feedback"]["avoid_repeating"]
    assert context["ai_strategy_quality_feedback"]["candidate_feedback"]["generation_gate"]["allow_open_plan"] is False

def test_ai_strategy_feedback_releases_hard_avoid_after_recent_bucket_recovers():
    now = now_ms()
    samples = [
        {"symbol": "AIRECOVERUSDT", "side": "LONG", "pnl": 1.0, "close_reason": "TP", "close_time": now - idx * 1000}
        for idx in range(3)
    ]
    samples.extend(
        {
            "symbol": "AIRECOVERUSDT",
            "side": "LONG",
            "pnl": -1.0,
            "close_reason": "SL",
            "close_time": now - 100000 - idx * 1000,
        }
        for idx in range(6)
    )

    report = ai_strategy_feedback._match_report("exact_symbol_side", samples)

    assert report["severity"] == "REVIEW"
    assert report["recent_recovered"] is True


def test_auto_trading_risk_model_rejects_cost_inefficient_small_position(monkeypatch):
    monkeypatch.setattr(settings, "require_codex_strategy_for_entry", False)
    monkeypatch.setattr(performance_guard, "_closed_rows", lambda: [])
    monkeypatch.setattr(settings, "trade_target_margin_pct", 0.02)
    monkeypatch.setattr(settings, "trade_max_margin_pct", 0.05)
    monkeypatch.setattr(settings, "trade_max_risk_pct", 0.03)
    monkeypatch.setattr(settings, "trade_min_margin_usdt", 1.0)
    monkeypatch.setattr(settings, "trade_min_notional_usdt", 2.0)
    monkeypatch.setattr(settings, "trade_min_net_profit_usdt", 0.20)
    monkeypatch.setattr(settings, "trade_min_profit_cost_ratio", 2.0)
    item = high_quality_item(symbol="SMALLCOSTUSDT", side="LONG", price=100)
    plan = StrategyPlan(
        strategy_id="small_cost",
        action="OPEN_LONG",
        symbol=item.symbol,
        side="LONG",
        entry_zone_low=100,
        entry_zone_high=100,
        ideal_entry_price=100,
        stop_loss=99,
        tp1=101,
        tp2=103,
        confidence=90,
        reason="small cost inefficient",
    )
    exec_plan = auto_trading_risk_model.decide(
        item,
        plan,
        {"equity": 50, "available_balance": 50, "loss_streak": 0, "open_positions": 0, "max_open_positions": 1, "trade_mode": "paper"},
        {"market_heat": 60, "volatility_regime": "normal"},
    )
    assert exec_plan.decision == "OBSERVE"
    assert "target net profit too small" in exec_plan.reason

def test_auto_loop_start_guard_blocks_recovery(monkeypatch):
    monkeypatch.setattr(performance_guard, "summary", lambda: {"recovery_mode": True, "pnl": -1})
    monkeypatch.setattr(settings, "trade_mode", "live")
    monkeypatch.setattr(settings, "live_trading_enabled", True)
    ok, reason, report = autotrader.loop_start_guard()
    assert not ok
    assert reason == "recovery_mode_blocks_live_auto_loop"
    assert report["recovery_mode"] is True

def test_paper_loop_start_guard_allows_paper_top_sampling(monkeypatch):
    monkeypatch.setattr(performance_guard, "summary", lambda: {"recovery_mode": False, "pnl": 0})
    monkeypatch.setattr(settings, "trade_mode", "live")
    monkeypatch.setattr(settings, "live_trading_enabled", False)
    monkeypatch.setattr(settings, "auto_trading_candidate_mode", "paper_top")
    monkeypatch.setattr(settings, "auto_trading_use_performance_guard", True)
    monkeypatch.setattr(settings, "max_open_positions", 1)
    ok, reason, report = autotrader.loop_start_guard()
    assert ok
    assert reason == "paper_closed_loop_sampling"
    assert report["recovery_mode"] is False

def test_paper_loop_start_guard_allows_recovery_probe_sampling(monkeypatch):
    monkeypatch.setattr(performance_guard, "summary", lambda: {"recovery_mode": True, "pnl": -1})
    monkeypatch.setattr(settings, "trade_mode", "live")
    monkeypatch.setattr(settings, "live_trading_enabled", False)
    monkeypatch.setattr(settings, "auto_trading_candidate_mode", "paper_top")
    monkeypatch.setattr(settings, "auto_trading_use_performance_guard", True)
    monkeypatch.setattr(settings, "paper_probe_enabled", True)
    monkeypatch.setattr(settings, "paper_loop_allow_recovery", True)
    monkeypatch.setattr(settings, "max_open_positions", 1)
    ok, reason, report = autotrader.loop_start_guard()
    assert ok
    assert reason == "paper_recovery_sampling"
    assert report["recovery_mode"] is True


def test_paper_repair_configures_codex_optional_for_controlled_paper(monkeypatch):
    captured = {}
    monkeypatch.setattr(settings, "require_codex_strategy_for_entry", True)
    monkeypatch.setattr(main_module, "update_env_values", lambda _path, values: captured.update(values))
    monkeypatch.setattr(strategy_evolver, "evolve", lambda use_codex, promote: {"ok": True, "use_codex": use_codex, "promote": promote})

    out = asyncio.run(main_module.api_learning_paper_repair(main_module.PaperRepairRequest(run_once=False)))

    assert captured["LIVE_TRADING_ENABLED"] == "false"
    assert captured["AI_ENABLED"] == "false"
    assert captured["AI_STRATEGY_PROVIDER"] == "rule"
    assert captured["REQUIRE_CODEX_STRATEGY_FOR_ENTRY"] == "false"
    assert settings.live_trading_enabled is False
    assert settings.ai_enabled is False
    assert settings.ai_strategy_provider == "rule"
    assert settings.require_codex_strategy_for_entry is False
    assert out["verify"] is None


def test_rule_strategy_provider_generates_local_paper_plan_without_external_ai(monkeypatch):
    item = high_quality_item(symbol="LOCALRULEUSDT", side="LONG", price=100)
    item.score = 88
    item.fund_confirm_count = 3
    monkeypatch.setattr(settings, "ai_enabled", False)
    monkeypatch.setattr(settings, "ai_strategy_provider", "rule")
    monkeypatch.setattr(settings, "require_codex_strategy_for_entry", False)
    monkeypatch.setattr(settings, "ai_position_review_enabled", False)
    position_registry.open.clear()

    plan = asyncio.run(
        openai_strategy_client.generate(
            item,
            {"candidate_selection": {"paper_probe": True, "paper_validation": True}},
        )
    )

    assert plan.action == "OPEN_LONG"
    assert plan.raw["provider"] == "rule"
    assert plan.raw["model"] == "local_rule_strategy"
    assert plan.raw["local_rule_fallback"] is True
    assert plan.raw["paper_probe"] is True


def test_rule_strategy_acceptance_mode_bypasses_open_position_capacity_guard(monkeypatch):
    item = high_quality_item(symbol="RULEACCEPTUSDT", side="LONG", price=100)
    item.score = 88
    item.fund_confirm_count = 3
    monkeypatch.setattr(settings, "ai_enabled", False)
    monkeypatch.setattr(settings, "ai_strategy_provider", "rule")
    monkeypatch.setattr(settings, "require_codex_strategy_for_entry", False)
    monkeypatch.setattr(settings, "ai_position_review_enabled", True)
    monkeypatch.setattr(settings, "max_open_positions", 1)
    position_registry.open.clear()
    position_registry.open["existing-paper"] = object()

    try:
        guarded = asyncio.run(openai_strategy_client.generate(item, {"candidate_selection": {"paper_probe": True}}))
        acceptance = asyncio.run(
            openai_strategy_client.generate(
                item,
                {"candidate_selection": {"paper_probe": True, "acceptance_mode": True}},
            )
        )
    finally:
        position_registry.open.clear()

    assert guarded.action == "WAIT"
    assert guarded.raw["provider"] == "local_position_priority_guard"
    assert acceptance.action == "OPEN_LONG"
    assert acceptance.raw["provider"] == "rule"


def test_legacy_startup_initializes_structured_database(monkeypatch):
    calls = []
    scheduled = []
    monkeypatch.setattr(main_module, "init_db", lambda: calls.append("init_db"))
    monkeypatch.setattr(main_module, "start_universal_anomaly_auto_train_thread", lambda: None)
    monkeypatch.setattr(main_module.universal_anomaly_trainer, "activate_latest", lambda: None)
    monkeypatch.setattr(settings, "market_data_mode", "mock")

    def fake_create_task(coro):
        coro.close()
        scheduled.append(coro)
        return object()

    monkeypatch.setattr(main_module.asyncio, "create_task", fake_create_task)

    asyncio.run(main_module.startup())

    assert calls == ["init_db"]
    assert len(scheduled) == 2


def test_paper_top_recovery_uses_paper_threshold_when_live_disabled(monkeypatch):
    item = high_quality_item(symbol="PAPERRECOVERYUSDT", side="LONG", price=100)
    item.score = 70
    item.fund_confirm_count = 3
    monkeypatch.setattr(settings, "trade_mode", "live")
    monkeypatch.setattr(settings, "live_trading_enabled", False)
    monkeypatch.setattr(settings, "auto_trading_candidate_mode", "paper_top")
    monkeypatch.setattr(settings, "auto_trading_candidate_min_score", 55.0)
    monkeypatch.setattr(settings, "strategy_recovery_min_score", 82.0)
    monkeypatch.setattr(settings, "auto_trading_candidate_limit", 1)
    monkeypatch.setattr(radar_engine, "top50", [item])
    monkeypatch.setattr(radar_engine, "select_ai_candidates", lambda items: [])

    candidates, source = autotrader._candidate_batch({"recovery_mode": True})

    assert source == "paper_top"
    assert [candidate.symbol for candidate in candidates] == ["PAPERRECOVERYUSDT"]

def test_paper_top_adapts_low_real_market_score_for_paper_observation(monkeypatch):
    item = high_quality_item(symbol="LOWREALUSDT", side="SHORT", price=100)
    item.score = 34
    item.fund_confirm_count = 3
    monkeypatch.setattr(settings, "trade_mode", "live")
    monkeypatch.setattr(settings, "live_trading_enabled", False)
    monkeypatch.setattr(settings, "auto_trading_candidate_mode", "paper_top")
    monkeypatch.setattr(settings, "auto_trading_candidate_min_score", 55.0)
    monkeypatch.setattr(settings, "strategy_recovery_min_score", 82.0)
    monkeypatch.setattr(settings, "auto_trading_candidate_limit", 1)
    monkeypatch.setattr(radar_engine, "top50", [item])
    monkeypatch.setattr(radar_engine, "select_ai_candidates", lambda items: [])

    candidates, source = autotrader._candidate_batch({"recovery_mode": True})
    diagnostics = autotrader.candidate_diagnostics({"recovery_mode": True})

    assert source == "paper_top"
    assert [candidate.symbol for candidate in candidates] == ["LOWREALUSDT"]
    assert diagnostics["gate"]["adaptive_score_floor"] is True
    assert diagnostics["gate"]["min_fund_confirm"] == 3

def test_paper_top_filters_top5_full_confirm_on_wick_warning(monkeypatch):
    item = high_quality_item(symbol="WICKYUSDT", side="LONG", price=100)
    item.score = 74
    item.fund_confirm_count = 3
    item.wick_ratio = 0.82
    monkeypatch.setattr(settings, "trade_mode", "live")
    monkeypatch.setattr(settings, "live_trading_enabled", False)
    monkeypatch.setattr(settings, "auto_trading_candidate_mode", "paper_top")
    monkeypatch.setattr(settings, "auto_trading_candidate_min_score", 55.0)
    monkeypatch.setattr(settings, "paper_probe_max_wick_ratio", 0.65)
    monkeypatch.setattr(settings, "auto_trading_candidate_limit", 1)
    monkeypatch.setattr(radar_engine, "top50", [item])
    monkeypatch.setattr(radar_engine, "select_ai_candidates", lambda items: [])

    candidates, source = autotrader._candidate_batch({"recovery_mode": False})
    diagnostics = autotrader.candidate_diagnostics({"recovery_mode": False})

    assert source == "paper_top"
    assert candidates == []
    assert diagnostics["counts"]["paper_top_all_gates"] == 0
    assert diagnostics["counts"]["paper_noise_budget_flagged"] == 1
    assert "paper_noise_budget_exceeded" in diagnostics["examples_top12"][0]["failed"]
    assert "wick_above_paper_noise_budget" in diagnostics["examples_top12"][0]["warnings"]

def test_light_candidate_diagnostics_skips_expensive_candidate_evaluation(monkeypatch):
    item = high_quality_item(symbol="LIGHTDIAGUSDT", side="LONG", price=100)
    item.score = 88
    item.fund_confirm_count = 3
    item.fund_confirm_total = 5
    monkeypatch.setattr(settings, "trade_mode", "live")
    monkeypatch.setattr(settings, "live_trading_enabled", False)
    monkeypatch.setattr(settings, "auto_trading_candidate_mode", "paper_top")
    monkeypatch.setattr(settings, "auto_trading_candidate_min_score", 55.0)
    monkeypatch.setattr(settings, "paper_probe_max_wick_ratio", 0.55)
    monkeypatch.setattr(radar_engine, "top50", [item])
    monkeypatch.setattr(
        "backend.trading.autotrader.candidate_feature_enhancer.evaluate",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("heavy candidate evaluation called")),
    )
    monkeypatch.setattr(
        radar_engine,
        "production_candidate_diagnostics",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("heavy production diagnostics called")),
    )

    diagnostics = autotrader.candidate_diagnostics_light({"recovery_mode": False})

    assert diagnostics["lightweight"] is True
    assert diagnostics["counts"]["top50"] == 1
    assert diagnostics["counts"]["paper_top_all_gates"] == 1

def test_autotrade_diagnostics_api_uses_lightweight_path(monkeypatch):
    item = high_quality_item(symbol="LIGHTAPIUSDT", side="LONG", price=100)
    item.score = 88
    item.fund_confirm_count = 3
    item.fund_confirm_total = 5
    monkeypatch.setattr(settings, "trade_mode", "live")
    monkeypatch.setattr(settings, "live_trading_enabled", False)
    monkeypatch.setattr(settings, "auto_trading_candidate_mode", "paper_top")
    monkeypatch.setattr(settings, "auto_trading_candidate_min_score", 55.0)
    monkeypatch.setattr(radar_engine, "top50", [item])
    monkeypatch.setattr(
        autotrader,
        "_candidate_batch",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("heavy candidate batch called")),
    )
    monkeypatch.setattr(
        autotrader,
        "candidate_diagnostics",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("heavy candidate diagnostics called")),
    )
    monkeypatch.setattr(
        ai_trade_director,
        "status",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("heavy trade director status called")),
    )

    result = asyncio.run(main_module.api_autotrade_diagnostics())

    assert result["candidate_filter"]["lightweight"] is True
    assert result["candidate_symbols_before_strategy"] == ["LIGHTAPIUSDT"]

def test_autotrade_diagnostics_starts_background_scan_without_waiting_when_cache_empty(monkeypatch):
    calls = {"started": False}
    monkeypatch.setattr(radar_engine, "top50", [])
    monkeypatch.setattr(radar_engine, "scan_in_progress", lambda: False)
    monkeypatch.setattr(main_module.radar_engine, "top50", [])
    monkeypatch.setattr(main_module.radar_engine, "scan_in_progress", lambda: False)

    async def fail_scan_wait(*_args, **_kwargs):
        raise AssertionError("diagnostics should not wait for a full radar scan")

    def fake_start_background(*_args, **_kwargs):
        calls["started"] = True
        return True

    monkeypatch.setattr(main_module, "_radar_scan_with_timeout", fail_scan_wait)
    monkeypatch.setattr(main_module, "_start_radar_scan_background", fake_start_background)

    result = asyncio.run(main_module.api_autotrade_diagnostics())

    assert calls["started"] is True
    assert result["ok"] is False
    assert result["scan_error"] == "radar_scan_warming_up"
    assert result["candidate_filter"]["lightweight"] is True

def test_system_readiness_api_exposes_chain_status_without_waiting_or_leaking_secrets(monkeypatch):
    from backend.main import app

    calls = {"started": False}
    monkeypatch.setattr(settings, "api_token", "secret-api-token-value")
    monkeypatch.setattr(settings, "binance_api_key", "SECRET_BINANCE_KEY_123")
    monkeypatch.setattr(settings, "binance_api_secret", "SECRET_BINANCE_SECRET_456")
    monkeypatch.setattr(radar_engine, "top50", [])
    monkeypatch.setattr(radar_engine, "scan_in_progress", lambda: False)
    monkeypatch.setattr(main_module.radar_engine, "top50", [])
    monkeypatch.setattr(main_module.radar_engine, "scan_in_progress", lambda: False)

    async def fail_scan_wait(*_args, **_kwargs):
        raise AssertionError("readiness should not wait for a full radar scan")

    def fake_start_background(*_args, **_kwargs):
        calls["started"] = True
        return True

    monkeypatch.setattr(main_module, "_radar_scan_with_timeout", fail_scan_wait)
    monkeypatch.setattr(main_module, "_start_radar_scan_background", fake_start_background)

    client = TestClient(app)
    response = client.get("/api/system/readiness")

    assert response.status_code == 200
    data = response.json()
    assert calls["started"] is True
    for section in ["market_data", "wait", "live_enablement", "paper_learning", "codex", "websocket", "database", "blockers"]:
        assert section in data
    assert data["market_data"]["warmup_started"] is True
    assert any(blocker["code"] == "radar_scan_warming_up" for blocker in data["blockers"])
    raw = json.dumps(data, ensure_ascii=False)
    assert "SECRET_BINANCE_KEY_123" not in raw
    assert "SECRET_BINANCE_SECRET_456" not in raw
    assert "secret-api-token-value" not in raw


def test_system_readiness_report_marks_codex_wait_and_paper_closed_loop(monkeypatch):
    import backend.system_readiness as system_readiness

    item = high_quality_item(symbol="READYWAITUSDT", side="LONG", price=100)
    item.score = 88
    item.fund_confirm_count = 3
    item.fund_confirm_total = 5
    monkeypatch.setattr(radar_engine, "top50", [item])
    monkeypatch.setattr(settings, "ai_enabled", True)
    monkeypatch.setattr(settings, "ai_strategy_provider", "codex_cli")
    monkeypatch.setattr(settings, "require_codex_strategy_for_entry", True)
    monkeypatch.setattr(settings, "trade_mode", "live")
    monkeypatch.setattr(settings, "live_trading_enabled", False)
    monkeypatch.setattr(settings, "paper_probe_enabled", True)
    monkeypatch.setattr(settings, "auto_trading_candidate_mode", "paper_top")
    monkeypatch.setattr(settings, "auto_trading_candidate_min_score", 55.0)
    monkeypatch.setattr(
        system_readiness.ai_service,
        "status",
        lambda **_kwargs: {
            "enabled": True,
            "provider": "codex_cli",
            "candidate_count_before_ai": 1,
            "will_invoke_for_current_candidates": True,
            "not_invoked_reason": "",
            "codex_cli": {
                "command_found": False,
                "ready_for_generation": False,
                "availability_reason": "codex_command_missing",
                "model": "gpt-test",
                "last_status": "never_invoked",
                "last_error": "",
            },
        },
    )

    report = system_readiness.system_readiness_report()

    assert report["wait"]["candidate_symbols"] == ["READYWAITUSDT"]
    assert report["codex"]["ready_for_generation"] is False
    assert report["codex"]["availability_reason"] == "codex_command_missing"
    assert report["paper_learning"]["closed_loop_enabled"] is True
    assert any(blocker["code"] == "codex_command_missing" for blocker in report["wait"]["blockers"])
    assert any(blocker["code"] == "codex_command_missing" for blocker in report["blockers"])


def test_system_readiness_blocks_codex_required_entry_when_auth_missing(monkeypatch):
    import backend.system_readiness as system_readiness

    item = high_quality_item(symbol="AUTHWAITUSDT", side="LONG", price=100)
    monkeypatch.setattr(radar_engine, "top50", [item])
    monkeypatch.setattr(settings, "ai_enabled", True)
    monkeypatch.setattr(settings, "ai_strategy_provider", "codex_cli")
    monkeypatch.setattr(settings, "require_codex_strategy_for_entry", True)
    monkeypatch.setattr(settings, "paper_probe_enabled", True)
    monkeypatch.setattr(settings, "auto_trading_candidate_mode", "paper_top")
    monkeypatch.setattr(settings, "auto_trading_candidate_min_score", 55.0)
    monkeypatch.setattr(
        system_readiness.ai_service,
        "status",
        lambda **_kwargs: {
            "enabled": True,
            "provider": "codex_cli",
            "candidate_count_before_ai": 1,
            "will_invoke_for_current_candidates": False,
            "not_invoked_reason": "codex_auth_missing",
            "codex_cli": {
                "command_found": True,
                "ready_for_generation": False,
                "availability_reason": "codex_auth_missing",
                "auth_available": False,
                "auth_required": True,
                "last_status": "never_invoked",
                "last_error": "",
            },
        },
    )

    report = system_readiness.system_readiness_report()

    assert report["codex"]["command_found"] is True
    assert report["codex"]["ready_for_generation"] is False
    assert report["codex"]["availability_reason"] == "codex_auth_missing"
    assert any(blocker["code"] == "codex_auth_missing" for blocker in report["wait"]["blockers"])
    assert any(blocker["code"] == "codex_required_for_paper_entry" for blocker in report["paper_learning"]["blockers"])


def test_compact_ai_strategy_status_exposes_codex_generation_readiness():
    status = main_module._compact_ai_strategy_status(
        {
            "enabled": True,
            "provider": "codex_cli",
            "candidate_source": "paper_top",
            "candidate_count_before_ai": 1,
            "will_invoke_for_current_candidates": False,
            "not_invoked_reason": "codex_command_missing",
            "codex_cli": {
                "command_found": False,
                "ready_for_generation": False,
                "availability_reason": "codex_command_missing",
                "schema_exists": True,
                "model": "gpt-test",
            },
            "deepseek": {},
        }
    )

    assert status["codex_cli"]["ready_for_generation"] is False
    assert status["codex_cli"]["availability_reason"] == "codex_command_missing"


def test_system_readiness_codex_status_does_not_reuse_rule_invocation_flag(monkeypatch):
    import backend.system_readiness as system_readiness

    monkeypatch.setattr(settings, "ai_strategy_provider", "rule")
    monkeypatch.setattr(settings, "require_codex_strategy_for_entry", False)

    status = system_readiness.codex_status(
        {
            "enabled": False,
            "provider": "rule",
            "will_invoke_for_current_candidates": True,
            "not_invoked_reason": "",
            "codex_cli": {
                "command_found": False,
                "ready_for_generation": False,
                "availability_reason": "codex_command_missing",
            },
        }
    )

    assert status["provider"] == "rule"
    assert status["will_invoke_for_current_candidates"] is False
    assert status["not_invoked_reason"] == "provider_rule_not_codex"


def test_system_readiness_exposes_paper_graduation_progress(monkeypatch):
    import backend.system_readiness as system_readiness

    monkeypatch.setattr(
        system_readiness.learning_data_audit,
        "summary",
        lambda: {
            "production_grade": False,
            "trust_level": "LOW",
            "can_hard_block_from_learning": False,
            "reasons": ["real_closed_samples_low", "market_backtest_missing"],
            "minimums": {"real_closed_samples": 30, "radar_history_days": 14.0},
            "sources": {
                "combined_samples": 404,
                "replay_samples": 403,
                "real_closed_samples_with_radar": 1,
                "replay_ratio": 0.9975,
            },
            "radar_snapshots": {"span_days": 0.2},
            "market_backtest": {"available": False, "quality_passed": False},
        },
    )
    monkeypatch.setattr(
        system_readiness.performance_guard,
        "summary",
        lambda: {"trades": 1, "win_rate": 1.0, "recent_win_rate": 1.0, "pnl": 0.5, "loss_streak": 0, "recovery_mode": False},
    )
    monkeypatch.setattr(
        system_readiness.trade_attributor,
        "summary",
        lambda: {"sample_count": 404, "global_win_rate": 0.4, "global_profit_factor": 0.8, "global_pnl": -1.0},
    )

    report = system_readiness.system_readiness_report()
    progress = report["paper_learning"]["graduation_progress"]

    assert progress["real_closed_samples_with_radar"] == 1
    assert progress["minimum_real_closed_samples"] == 30
    assert progress["missing_real_closed_samples"] == 29
    assert progress["market_backtest_available"] is False
    assert progress["market_backtest_quality_passed"] is False
    assert progress["radar_history_days"] == 0.2
    assert progress["minimum_radar_history_days"] == 14.0
    assert progress["production_grade"] is False
    assert progress["next_requirement"] == "Collect 29 more real closed paper/shadow samples with radar context or provide a passing market backtest."


def test_system_readiness_database_probe_counts_tables(tmp_path):
    import backend.system_readiness as system_readiness

    database = DB(str(tmp_path / "readiness.sqlite"))
    database.set_kv("probe", {"ok": True})

    health = system_readiness.database_health(database)

    assert health["ok"] is True
    assert health["exists"] is True
    assert health["tables"]["kv"] == 1
    assert "readiness.sqlite" in health["path"]


def test_paper_top_balances_old_wick_noise_when_current_candle_is_clean(monkeypatch):
    item = high_quality_item(symbol="BALANCEDWICKUSDT", side="LONG", price=100)
    item.score = 92
    item.fund_confirm_count = 3
    item.fund_confirm_total = 5
    item.wick_ratio = 0.96
    item.score_features = {
        "structure_metrics": {
            "current_wick_ratio": 0.18,
            "max_wick_ratio_14": 0.96,
            "avg_wick_ratio_14": 0.42,
            "bars_since_max_wick": 8,
        }
    }
    monkeypatch.setattr(settings, "trade_mode", "live")
    monkeypatch.setattr(settings, "live_trading_enabled", False)
    monkeypatch.setattr(settings, "auto_trading_candidate_mode", "paper_top")
    monkeypatch.setattr(settings, "auto_trading_candidate_min_score", 55.0)
    monkeypatch.setattr(settings, "paper_probe_max_wick_ratio", 0.55)
    monkeypatch.setattr(settings, "auto_trading_candidate_limit", 1)
    monkeypatch.setattr(radar_engine, "top50", [item])
    monkeypatch.setattr(radar_engine, "select_ai_candidates", lambda items: [])

    candidates, source = autotrader._candidate_batch({"recovery_mode": False})
    diagnostics = autotrader.candidate_diagnostics({"recovery_mode": False})

    assert source == "paper_top"
    assert [candidate.symbol for candidate in candidates] == ["BALANCEDWICKUSDT"]
    assert diagnostics["counts"]["paper_top_all_gates"] == 1
    assert diagnostics["examples_top12"][0]["failed"] == []
    assert "historical_wick_noise_balanced" in diagnostics["examples_top12"][0]["warnings"]

def test_paper_top_still_hard_blocks_extreme_current_wick(monkeypatch):
    item = high_quality_item(symbol="EXTREMECURRENTWICKUSDT", side="LONG", price=100)
    item.score = 100
    item.fund_confirm_count = 3
    item.fund_confirm_total = 5
    item.wick_ratio = 0.30
    item.score_features = {
        "structure_metrics": {
            "current_wick_ratio": 0.90,
            "max_wick_ratio_14": 0.90,
            "avg_wick_ratio_14": 0.34,
            "bars_since_max_wick": 0,
        }
    }
    monkeypatch.setattr(settings, "trade_mode", "live")
    monkeypatch.setattr(settings, "live_trading_enabled", False)
    monkeypatch.setattr(settings, "auto_trading_candidate_mode", "paper_top")
    monkeypatch.setattr(settings, "auto_trading_candidate_min_score", 55.0)
    monkeypatch.setattr(settings, "paper_probe_max_wick_ratio", 0.55)
    monkeypatch.setattr(settings, "auto_trading_candidate_limit", 1)
    monkeypatch.setattr(radar_engine, "top50", [item])
    monkeypatch.setattr(radar_engine, "select_ai_candidates", lambda items: [])

    candidates, source = autotrader._candidate_batch({"recovery_mode": False})
    diagnostics = autotrader.candidate_diagnostics({"recovery_mode": False})

    assert source == "paper_top"
    assert candidates == []
    assert diagnostics["counts"]["paper_top_all_gates"] == 0
    assert "paper_noise_budget_exceeded" in diagnostics["examples_top12"][0]["failed"]
    assert "current_wick_extreme" in diagnostics["examples_top12"][0]["warnings"]

def test_paper_top_uses_clean_top20_backup_before_probe_fallback(monkeypatch):
    noisy_top = []
    for idx in range(5):
        item = high_quality_item(symbol=f"NOISYTOP{idx}USDT", side="LONG", price=100)
        item.rank = idx + 1
        item.score = 96 - idx
        item.fund_confirm_count = 3
        item.fund_confirm_total = 5
        item.wick_ratio = 0.92
        item.score_features = {
            "structure_metrics": {
                "current_wick_ratio": 0.24,
                "max_wick_ratio_14": 0.92,
                "avg_wick_ratio_14": 0.74,
                "bars_since_max_wick": 1,
            }
        }
        noisy_top.append(item)
    backup = high_quality_item(symbol="CLEANTOP20USDT", side="LONG", price=100)
    backup.rank = 12
    backup.score = 82
    backup.fund_confirm_count = 3
    backup.fund_confirm_total = 5
    backup.wick_ratio = 0.24
    monkeypatch.setattr(settings, "trade_mode", "live")
    monkeypatch.setattr(settings, "live_trading_enabled", False)
    monkeypatch.setattr(settings, "auto_trading_candidate_mode", "paper_top")
    monkeypatch.setattr(settings, "auto_trading_candidate_min_score", 55.0)
    monkeypatch.setattr(settings, "paper_probe_max_wick_ratio", 0.55)
    monkeypatch.setattr(settings, "auto_trading_candidate_limit", 1)
    monkeypatch.setattr(radar_engine, "top50", [*noisy_top, backup])
    monkeypatch.setattr(radar_engine, "select_ai_candidates", lambda items: [])
    monkeypatch.setattr(autotrader, "_paper_probe_batch", lambda *args, **kwargs: ([], "paper_probe_disabled_for_test"))

    candidates, source = autotrader._candidate_batch({"recovery_mode": False})
    diagnostics = autotrader.candidate_diagnostics({"recovery_mode": False})

    assert source == "paper_top"
    assert [candidate.symbol for candidate in candidates] == ["CLEANTOP20USDT"]
    assert diagnostics["counts"]["balanced_scope"] == 6
    assert diagnostics["counts"]["paper_noise_budget_flagged"] == 5

def test_paper_top_scope_promotes_best_trade_quality_from_top50(monkeypatch):
    top5 = []
    for idx in range(5):
        item = high_quality_item(symbol=f"TOP{idx}USDT", side="LONG", price=100)
        item.rank = idx + 1
        item.score = 72
        item.fund_confirm_count = 2
        top5.append(item)
    rank6 = high_quality_item(symbol="RANK6USDT", side="LONG", price=100)
    rank6.rank = 6
    rank6.score = 90
    rank6.fund_confirm_count = 3
    monkeypatch.setattr(settings, "trade_mode", "live")
    monkeypatch.setattr(settings, "live_trading_enabled", False)
    monkeypatch.setattr(settings, "auto_trading_candidate_mode", "paper_top")
    monkeypatch.setattr(settings, "auto_trading_candidate_min_score", 55.0)
    monkeypatch.setattr(settings, "auto_trading_candidate_limit", 3)
    monkeypatch.setattr(radar_engine, "top50", [*top5, rank6])
    monkeypatch.setattr(radar_engine, "select_ai_candidates", lambda items: [])

    candidates, source = autotrader._candidate_batch({"recovery_mode": False})
    diagnostics = autotrader.candidate_diagnostics({"recovery_mode": False})

    assert source == "paper_top"
    assert [candidate.symbol for candidate in candidates] == ["RANK6USDT"]
    assert diagnostics["counts"]["top5_scope"] == 5
    assert diagnostics["counts"]["ai_review_candidates"] == 1
    assert diagnostics["counts"]["paper_top_all_gates"] >= 1

def test_paper_top_mode_uses_trade_top5_not_strict_candidate_list(monkeypatch):
    top5 = []
    for idx in range(5):
        item = high_quality_item(symbol=f"STRICTTOP{idx}USDT", side="LONG", price=100)
        item.rank = idx + 1
        item.score = 70
        item.fund_confirm_count = 2
        top5.append(item)
    rank6 = high_quality_item(symbol="STRICTRANK6USDT", side="LONG", price=100)
    rank6.rank = 6
    rank6.score = 90
    rank6.fund_confirm_count = 3
    monkeypatch.setattr(settings, "trade_mode", "live")
    monkeypatch.setattr(settings, "live_trading_enabled", False)
    monkeypatch.setattr(settings, "auto_trading_candidate_mode", "paper_top")
    monkeypatch.setattr(settings, "auto_trading_candidate_min_score", 55.0)
    monkeypatch.setattr(settings, "auto_trading_candidate_limit", 3)
    monkeypatch.setattr(radar_engine, "top50", [*top5, rank6])
    monkeypatch.setattr(radar_engine, "select_ai_candidates", lambda items: [rank6])

    candidates, source = autotrader._candidate_batch({"recovery_mode": False})

    assert source == "paper_top"
    assert [candidate.symbol for candidate in candidates] == ["STRICTRANK6USDT"]

def test_paper_top_only_sends_highest_enhanced_top5_candidate_to_ai(monkeypatch):
    items = []
    for idx, score in enumerate([61, 82, 77, 69, 80]):
        item = high_quality_item(symbol=f"SCORETOP{idx}USDT", side="LONG", price=100)
        item.rank = idx + 1
        item.score = score
        items.append(item)
    monkeypatch.setattr(settings, "trade_mode", "live")
    monkeypatch.setattr(settings, "live_trading_enabled", False)
    monkeypatch.setattr(settings, "auto_trading_candidate_mode", "paper_top")
    monkeypatch.setattr(radar_engine, "top50", items)
    enhanced_rank = {f"SCORETOP{idx}USDT": value for idx, value in enumerate([10, 40, 30, 80, 50])}
    monkeypatch.setattr(
        "backend.trading.autotrader.candidate_feature_enhancer.rank_key",
        lambda item: (enhanced_rank[item.symbol],),
    )
    monkeypatch.setattr(autotrader, "_paper_openability_score", lambda item: 0.0)

    candidates, source = autotrader._candidate_batch({"recovery_mode": False})
    diagnostics = autotrader.candidate_diagnostics({"recovery_mode": False})

    assert source == "paper_top"
    assert [candidate.symbol for candidate in candidates] == ["SCORETOP3USDT"]
    assert diagnostics["counts"]["ai_review_candidates"] == 1

def test_paper_top_candidate_lock_ignores_scan_reorder_inside_lock_window(monkeypatch):
    first = high_quality_item(symbol="LOCKAUSDT", side="LONG", price=100)
    first.score = 82
    first.rank = 1
    second = high_quality_item(symbol="LOCKBUSDT", side="LONG", price=100)
    second.score = 78
    second.rank = 2
    monkeypatch.setattr(settings, "trade_mode", "live")
    monkeypatch.setattr(settings, "live_trading_enabled", False)
    monkeypatch.setattr(settings, "auto_trading_candidate_mode", "paper_top")
    monkeypatch.setattr(settings, "ai_candidate_lock_seconds", 180)
    monkeypatch.setattr(settings, "ai_candidate_replace_score_margin", 8.0)
    monkeypatch.setattr(radar_engine, "top50", [first, second])
    monkeypatch.setattr("backend.trading.autotrader.candidate_feature_enhancer.evaluate", stable_candidate_feature_report)

    candidates, source = autotrader._candidate_batch({"recovery_mode": False})

    assert source == "paper_top"
    assert [candidate.symbol for candidate in candidates] == ["LOCKAUSDT"]

    first_refresh = high_quality_item(symbol="LOCKAUSDT", side="LONG", price=100)
    first_refresh.score = 82
    first_refresh.rank = 2
    second_refresh = high_quality_item(symbol="LOCKBUSDT", side="LONG", price=100)
    second_refresh.score = 92
    second_refresh.rank = 1
    monkeypatch.setattr(radar_engine, "top50", [second_refresh, first_refresh])

    candidates, source = autotrader._candidate_batch({"recovery_mode": False})

    assert source == "paper_top"
    assert [candidate.symbol for candidate in candidates] == ["LOCKAUSDT"]
    assert autotrader.candidate_lock_status()["reason"] == "locked_candidate"

def test_paper_top_candidate_lock_survives_temporary_top5_drop(monkeypatch):
    locked = high_quality_item(symbol="LOCKDROPUSDT", side="LONG", price=100)
    locked.score = 94
    locked.rank = 1
    challenger = high_quality_item(symbol="LOCKDROPCHALLENGERUSDT", side="LONG", price=100)
    challenger.score = 48
    challenger.rank = 2
    challenger.fund_confirm_count = 2
    monkeypatch.setattr(settings, "trade_mode", "live")
    monkeypatch.setattr(settings, "live_trading_enabled", False)
    monkeypatch.setattr(settings, "auto_trading_candidate_mode", "paper_top")
    monkeypatch.setattr(settings, "ai_candidate_lock_seconds", 180)
    monkeypatch.setattr(radar_engine, "top50", [locked, challenger])

    def stable_feature_report(item):
        score = float(getattr(item, "score", 0.0) or 0.0)
        feature_score = 80.0
        estimated_win_rate = 0.60 + score / 1000.0
        selection_score = 70.0 + score / 10.0
        return SimpleNamespace(
            feature_score=feature_score,
            estimated_win_rate=estimated_win_rate,
            selection_score=selection_score,
            asdict=lambda: {
                "feature_score": feature_score,
                "estimated_win_rate": estimated_win_rate,
                "selection_score": selection_score,
                "positive_factors": ["estimated_win_rate_above_paper_gate", "fund_confirm_full"],
                "failure_risks": [],
            }
        )

    monkeypatch.setattr("backend.trading.autotrader.candidate_feature_enhancer.evaluate", stable_feature_report)

    candidates, source = autotrader._candidate_batch({"recovery_mode": False})

    assert source == "paper_top"
    assert [candidate.symbol for candidate in candidates] == ["LOCKDROPUSDT"]

    refreshed_locked = high_quality_item(symbol="LOCKDROPUSDT", side="LONG", price=100)
    refreshed_locked.score = 30
    refreshed_locked.rank = 8
    refreshed_locked.fund_confirm_count = 1
    stronger_pool = []
    for idx, score in enumerate([91, 90, 89, 88, 87], start=1):
        item = high_quality_item(symbol=f"LOCKDROPFILL{idx}USDT", side="LONG", price=100)
        item.score = score
        item.rank = idx
        stronger_pool.append(item)
    monkeypatch.setattr(radar_engine, "top50", [*stronger_pool, refreshed_locked])

    candidates, source = autotrader._candidate_batch({"recovery_mode": False})

    assert source == "paper_top"
    assert [candidate.symbol for candidate in candidates] == ["LOCKDROPUSDT"]
    assert autotrader.candidate_lock_status()["reason"] == "locked_candidate_retained_outside_current_top5"

def test_paper_top_candidate_lock_replaces_after_age_and_score_margin(monkeypatch):
    first = high_quality_item(symbol="OLDLOCKUSDT", side="LONG", price=100)
    first.score = 80
    first.rank = 1
    second = high_quality_item(symbol="NEWLOCKUSDT", side="LONG", price=100)
    second.score = 78
    second.rank = 2
    monkeypatch.setattr(settings, "trade_mode", "live")
    monkeypatch.setattr(settings, "live_trading_enabled", False)
    monkeypatch.setattr(settings, "auto_trading_candidate_mode", "paper_top")
    monkeypatch.setattr(settings, "ai_candidate_lock_seconds", 1)
    monkeypatch.setattr(settings, "ai_candidate_replace_score_margin", 5.0)
    monkeypatch.setattr(settings, "ai_candidate_max_stale_seconds", 300)
    monkeypatch.setattr(radar_engine, "top50", [first, second])
    monkeypatch.setattr("backend.trading.autotrader.candidate_feature_enhancer.evaluate", stable_candidate_feature_report)

    candidates, source = autotrader._candidate_batch({"recovery_mode": False})

    assert source == "paper_top"
    assert [candidate.symbol for candidate in candidates] == ["OLDLOCKUSDT"]

    autotrader.ai_candidate_lock["locked_at_ms"] = now_ms() - 2000
    first_refresh = high_quality_item(symbol="OLDLOCKUSDT", side="LONG", price=100)
    first_refresh.score = 80
    first_refresh.rank = 2
    second_refresh = high_quality_item(symbol="NEWLOCKUSDT", side="LONG", price=100)
    second_refresh.score = 88
    second_refresh.rank = 1
    monkeypatch.setattr(radar_engine, "top50", [second_refresh, first_refresh])

    candidates, source = autotrader._candidate_batch({"recovery_mode": False})

    assert source == "paper_top"
    assert [candidate.symbol for candidate in candidates] == ["NEWLOCKUSDT"]
    assert autotrader.candidate_lock_status()["reason"] == "stronger_candidate_replaced_lock"

def test_paper_top_prefers_feature_enhanced_higher_win_rate_candidate(monkeypatch):
    noisy = high_quality_item(symbol="NOISYHIGHUSDT", side="LONG", price=100)
    noisy.score = 92
    noisy.rank = 1
    noisy.change_5m = 0.2
    noisy.change_15m = 0.1
    noisy.change_1h = -0.3
    noisy.taker_buy_ratio = 0.56
    noisy.taker_sell_ratio = 0.44
    noisy.depth_imbalance = 0.09
    noisy.sm_delta = -0.5
    noisy.volume_spike = 1.4
    noisy.wick_ratio = 0.69
    noisy.fake_breakout_risk = "MEDIUM"

    aligned = high_quality_item(symbol="FEATUREDUSDT", side="LONG", price=100)
    aligned.score = 78
    aligned.rank = 2
    aligned.change_5m = 2.2
    aligned.change_15m = 3.1
    aligned.change_1h = 2.4
    aligned.taker_buy_ratio = 0.72
    aligned.taker_sell_ratio = 0.28
    aligned.depth_imbalance = 0.24
    aligned.sm_delta = 1.1
    aligned.volume_spike = 3.2
    aligned.wick_ratio = 0.21
    aligned.fake_breakout_risk = "LOW"

    monkeypatch.setattr(settings, "trade_mode", "live")
    monkeypatch.setattr(settings, "live_trading_enabled", False)
    monkeypatch.setattr(settings, "auto_trading_candidate_mode", "paper_top")
    monkeypatch.setattr(radar_engine, "top50", [noisy, aligned])

    candidates, source = autotrader._candidate_batch({"recovery_mode": False})
    diagnostics = autotrader.candidate_diagnostics({"recovery_mode": False})

    assert source == "paper_top"
    assert [candidate.symbol for candidate in candidates] == ["FEATUREDUSDT"]
    enhanced = {row["symbol"]: row for row in diagnostics["enhanced_top5"]}
    assert "FEATUREDUSDT" in enhanced
    assert "NOISYHIGHUSDT" not in enhanced
    assert diagnostics["counts"]["paper_noise_budget_flagged"] >= 1


def test_paper_top_filters_unclean_enhanced_candidate_before_ai_review(monkeypatch):
    unclean = high_quality_item(symbol="UNCLEANAIUSDT", side="LONG", price=100)
    unclean.rank = 1
    unclean.score = 92
    unclean.fund_confirm_count = 1

    valid = high_quality_item(symbol="VALIDAIUSDT", side="LONG", price=100)
    valid.rank = 2
    valid.score = 58
    valid.fund_confirm_count = 2

    monkeypatch.setattr(settings, "trade_mode", "live")
    monkeypatch.setattr(settings, "live_trading_enabled", False)
    monkeypatch.setattr(settings, "auto_trading_candidate_mode", "paper_top")
    monkeypatch.setattr(radar_engine, "top50", [unclean, valid])
    monkeypatch.setattr(
        autotrader,
        "_paper_openability_score",
        lambda item: 999.0 if item.symbol == "UNCLEANAIUSDT" else 1.0,
    )

    candidates, source = autotrader._candidate_batch({"recovery_mode": False})

    assert source == "paper_top"
    assert [candidate.symbol for candidate in candidates] == ["VALIDAIUSDT"]


def test_autotrader_live_account_context_carries_can_trade_false(monkeypatch):
    monkeypatch.setattr(settings, "trade_mode", "live")
    monkeypatch.setattr(settings, "live_trading_enabled", True)

    async def fake_account_summary():
        return {
            "mode": "live",
            "configured": True,
            "canTrade": False,
            "walletBalance": 1000,
            "availableBalance": 900,
            "marginBalance": 1000,
        }

    monkeypatch.setattr(account_service, "get_account_summary", fake_account_summary)

    summary, account = __import__("asyncio").run(autotrader._account_context(open_positions=0))

    assert summary["canTrade"] is False
    assert account["can_trade"] is False
    assert account["execution_context"] == "live"


def test_paper_top_geometry_sample_replaces_weak_ai_candidate(monkeypatch):
    weak = high_quality_item(symbol="GEOMETRYWEAKUSDT", side="SHORT", price=100)
    weak.rank = 1
    weak.score = 96
    strong = high_quality_item(symbol="GEOMETRYOKUSDT", side="SHORT", price=100)
    strong.rank = 2
    strong.score = 72

    weak_sample = {
        "status": "weak",
        "sample_model": "first_touch_geometry_v1",
        "symbol": weak.symbol,
        "side": "SHORT",
        "pass_count": 0,
        "samples": {"sample_count": 119, "win_rate": 0.46, "expected_r": 0.05, "profit_factor": 1.03},
    }
    ok_sample = {
        "status": "ok",
        "sample_model": "first_touch_geometry_v1",
        "symbol": strong.symbol,
        "side": "SHORT",
        "pass_count": 12,
        "selected_geometry": {"side": "SHORT", "entry": 100, "stop_loss": 101, "tp1": 99, "tp2": 97},
        "samples": {"sample_count": 119, "win_rate": 0.64, "expected_r": 0.42, "profit_factor": 1.82},
    }

    monkeypatch.setattr(autotrader, "_paper_top_candidates", lambda performance, limit: [weak, strong])

    async def fake_geometry(item):
        return ok_sample if item.symbol == strong.symbol else weak_sample

    monkeypatch.setattr("backend.trading.autotrader.strategy_geometry_sampler.evaluate", fake_geometry)

    ordered, reports = __import__("asyncio").run(
        autotrader._geometry_supported_candidate_order([weak], "paper_top", {"recovery_mode": False})
    )

    assert [item.symbol for item in ordered] == ["GEOMETRYOKUSDT"]
    assert autotrader._candidate_geometry_samples["GEOMETRYOKUSDT"] == ok_sample
    assert autotrader.candidate_lock_status()["symbol"] == "GEOMETRYOKUSDT"
    assert {row["symbol"]: row["geometry_status"] for row in reports} == {
        "GEOMETRYWEAKUSDT": "weak",
        "GEOMETRYOKUSDT": "ok",
    }


def test_paper_top_empty_uses_paper_probe_backfill(monkeypatch):
    unclean = high_quality_item(symbol="UNCLEANONLYUSDT", side="LONG", price=100)
    unclean.rank = 1
    unclean.score = 92
    unclean.fund_confirm_count = 1
    probe = high_quality_item(symbol="PROBEBACKFILLUSDT", side="LONG", price=100)
    probe.fund_confirm_count = 1

    monkeypatch.setattr(settings, "trade_mode", "live")
    monkeypatch.setattr(settings, "live_trading_enabled", False)
    monkeypatch.setattr(settings, "auto_trading_candidate_mode", "paper_top")
    monkeypatch.setattr(settings, "paper_probe_enabled", True)
    monkeypatch.setattr(radar_engine, "top50", [unclean])
    monkeypatch.setattr(autotrader, "_paper_probe_batch", lambda *args, **kwargs: ([probe], "paper_probe_paper_top_empty"))

    candidates, source = autotrader._candidate_batch({"recovery_mode": False})

    assert source == "paper_probe_paper_top_empty"
    assert [candidate.symbol for candidate in candidates] == ["PROBEBACKFILLUSDT"]


def test_paper_top_ai_review_skips_neutral_top5(monkeypatch):
    neutral = high_quality_item(symbol="NEUTRALTOPUSDT", side="LONG", price=100)
    neutral.rank = 1
    neutral.direction = "NEUTRAL"
    long_item = high_quality_item(symbol="DIRECTIONALTOPUSDT", side="LONG", price=100)
    long_item.rank = 2
    monkeypatch.setattr(settings, "trade_mode", "live")
    monkeypatch.setattr(settings, "live_trading_enabled", False)
    monkeypatch.setattr(settings, "auto_trading_candidate_mode", "paper_top")
    monkeypatch.setattr(radar_engine, "top50", [neutral, long_item])

    candidates, source = autotrader._candidate_batch({"recovery_mode": False})
    diagnostics = autotrader.candidate_diagnostics({"recovery_mode": False})

    assert source == "paper_top"
    assert [candidate.symbol for candidate in candidates] == ["DIRECTIONALTOPUSDT"]
    assert diagnostics["counts"]["ai_review_candidates"] == 1

def test_paper_top_ai_open_creates_paper_validation_position(monkeypatch):
    symbol = "TOPVALIDATEUSDT"
    cleanup_symbol(symbol)
    position_registry.open.clear()
    autotrader.executed_strategy_ids.clear()
    item = high_quality_item(symbol=symbol, side="LONG", price=100)
    item.rank = 1
    item.score = 74
    item.fund_confirm_count = 3
    item.wick_ratio = 0.42
    monkeypatch.setattr(settings, "trade_mode", "live")
    monkeypatch.setattr(settings, "live_trading_enabled", False)
    monkeypatch.setattr(settings, "auto_trading_candidate_mode", "paper_top")
    monkeypatch.setattr(settings, "auto_trading_candidate_min_score", 55.0)
    monkeypatch.setattr(settings, "auto_trading_candidate_limit", 1)
    monkeypatch.setattr(settings, "max_open_positions", 1)
    monkeypatch.setattr(settings, "paper_account_equity_usdt", 1000.0)
    monkeypatch.setattr(settings, "trade_min_net_profit_usdt", 0.2)
    monkeypatch.setattr(settings, "trade_min_profit_cost_ratio", 1.0)
    monkeypatch.setattr(radar_engine, "top50", [item])
    async def fake_scan(force_refresh=False):
        return radar_engine.top50
    monkeypatch.setattr(radar_engine, "scan", fake_scan)
    monkeypatch.setattr(strategy_registry, "active", lambda: None)
    monkeypatch.setattr(performance_guard, "summary", lambda: {"recovery_mode": False, "pnl": 0, "trades": 0, "win_rate": 0, "recent_win_rate": 0, "loss_streak": 0})

    async def fake_price(symbol_arg):
        return item.price

    async def fake_generate(review_item, position_context=None):
        plan = rule_strategy_generator.generate_probe(review_item)
        plan.raw["provider"] = "codex_cli"
        return plan

    monkeypatch.setattr(market_service, "price_for", fake_price)
    monkeypatch.setattr("backend.trading.autotrader.openai_strategy_client.generate", fake_generate)
    original_decide = auto_trading_risk_model.decide

    def assert_validation_not_probe(review_item, plan, account, market, paper_probe=False):
        assert paper_probe is False
        return original_decide(review_item, plan, account, market, paper_probe=paper_probe)

    monkeypatch.setattr("backend.trading.autotrader.auto_trading_risk_model.decide", assert_validation_not_probe)

    try:
        diagnostics = autotrader.candidate_diagnostics({"recovery_mode": False})
        result = __import__("asyncio").run(autotrader._run_once_locked())
        opened = position_registry.list_open()

        assert diagnostics["counts"]["paper_validation_candidates"] == 1
        assert result["results"][0]["decision"] == "OPEN_PAPER_VALIDATION"
        assert result["results"][0]["paper_validation"] is True
        assert len(opened) == 1
        assert opened[0].symbol == symbol
        assert opened[0].strategy_contract["execution"]["stage"] == "paper_validation"
        assert opened[0].strategy_contract["allowed_stages"]["live"] is False
    finally:
        cleanup_symbol(symbol)


def test_paper_top_retries_next_candidate_after_quality_rejection(monkeypatch):
    first_symbol = "TOPREJECTUSDT"
    second_symbol = "TOPRETRYUSDT"
    cleanup_symbol(first_symbol)
    cleanup_symbol(second_symbol)
    position_registry.open.clear()
    autotrader.executed_strategy_ids.clear()
    autotrader.ai_candidate_lock.clear()
    autotrader.ai_candidate_wait_cooldowns.clear()
    first = high_quality_item(symbol=first_symbol, side="LONG", price=100)
    first.rank = 1
    first.score = 92
    second = high_quality_item(symbol=second_symbol, side="LONG", price=100)
    second.rank = 2
    second.score = 88
    monkeypatch.setattr(settings, "trade_mode", "live")
    monkeypatch.setattr(settings, "live_trading_enabled", False)
    monkeypatch.setattr(settings, "auto_trading_candidate_mode", "paper_top")
    monkeypatch.setattr(settings, "auto_trading_candidate_limit", 1)
    monkeypatch.setattr(settings, "auto_trading_candidate_min_score", 55.0)
    monkeypatch.setattr(settings, "max_open_positions", 1)
    monkeypatch.setattr(settings, "paper_account_equity_usdt", 1000.0)
    monkeypatch.setattr(settings, "trade_min_net_profit_usdt", 0.2)
    monkeypatch.setattr(settings, "trade_min_profit_cost_ratio", 1.0)
    monkeypatch.setattr(radar_engine, "top50", [first, second])
    monkeypatch.setattr(radar_engine, "last_scan_id", "scan_retry_quality")
    monkeypatch.setattr(strategy_registry, "active", lambda: None)
    monkeypatch.setattr(
        performance_guard,
        "summary",
        lambda: {
            "recovery_mode": False,
            "pnl": 0,
            "trades": 0,
            "win_rate": 0,
            "recent_win_rate": 0,
            "loss_streak": 0,
        },
    )

    async def fake_scan(force_refresh=False):
        return radar_engine.top50

    async def fake_prepare(item, *, force_scan):
        return item, {
            "market_refresh_degraded": False,
            "trade_price": {
                "ok": True,
                "stale": False,
                "safe_for_execution": True,
                "error": "",
                "price": item.price,
                "source": "book_ticker_mid",
            },
            "symbol_present_after_scan": True,
        }

    async def fake_generate_strategy_plan(review_item, *args, **kwargs):
        plan = rule_strategy_generator.generate_probe(review_item)
        plan.raw["provider"] = "codex_cli"
        plan.raw["strategy_contract"] = build_rule_contract(review_item, plan)
        return plan

    risk_calls = []

    def fake_decide(review_item, plan, account, market, paper_probe=False):
        risk_calls.append(review_item.symbol)
        if review_item.symbol == first_symbol:
            return ExecutionPlan(
                decision="OBSERVE",
                mode="paper",
                symbol=plan.symbol,
                side=plan.side,
                dynamic_margin=0,
                dynamic_leverage=1,
                quantity=0,
                entry_price=plan.ideal_entry_price,
                stop_loss=plan.stop_loss,
                tp1=plan.tp1,
                tp2=plan.tp2,
                tp1_close_ratio=0.0,
                tp2_close_ratio=0.0,
                management_mode="OBSERVE",
                cooldown_after_trade=0,
                reason="quality rejected: test first candidate",
            )
        return feedback_exec_plan(plan)

    monkeypatch.setattr(radar_engine, "scan", fake_scan)
    monkeypatch.setattr(autotrader, "_prepare_latest_item_for_ai", fake_prepare)
    monkeypatch.setattr(autotrader, "_generate_strategy_plan", fake_generate_strategy_plan)
    monkeypatch.setattr("backend.trading.autotrader.auto_trading_risk_model.decide", fake_decide)

    try:
        result = asyncio.run(autotrader._run_once_locked())
        opened = position_registry.list_open()

        assert risk_calls == [first_symbol, second_symbol]
        assert result["results"][0]["decision"] == "OBSERVE"
        assert result["results"][0]["retry_candidates_added"] == [second_symbol]
        assert result["results"][1]["decision"] == "OPEN_PAPER_VALIDATION"
        assert result["results"][1]["symbol"] == second_symbol
        assert len(opened) == 1
        assert opened[0].symbol == second_symbol
    finally:
        cleanup_symbol(first_symbol)
        cleanup_symbol(second_symbol)


def test_paper_top_geometry_retry_pool_excludes_wait_cooldown_candidate(monkeypatch):
    first = high_quality_item(symbol="COOLDOWNPOOLUSDT", side="LONG", price=100)
    first.rank = 1
    first.score = 92
    second = high_quality_item(symbol="NEXTPOOLUSDT", side="LONG", price=100)
    second.rank = 2
    second.score = 88
    monkeypatch.setattr(settings, "auto_trading_candidate_mode", "paper_top")
    monkeypatch.setattr(settings, "auto_trading_candidate_limit", 1)
    monkeypatch.setattr(settings, "trade_mode", "live")
    monkeypatch.setattr(settings, "live_trading_enabled", False)
    monkeypatch.setattr(radar_engine, "top50", [first, second])
    autotrader.ai_candidate_wait_cooldowns[first.symbol] = now_ms() + 180_000

    try:
        pool = autotrader._geometry_candidate_pool([second], "paper_top", {"recovery_mode": False})

        assert first.symbol not in [item.symbol for item in pool]
        assert [item.symbol for item in pool][:1] == [second.symbol]
    finally:
        autotrader.ai_candidate_wait_cooldowns.pop(first.symbol, None)


def test_autotrader_does_not_live_execute_when_exec_plan_mode_is_paper(monkeypatch):
    symbol = "MODEGUARDUSDT"
    cleanup_symbol(symbol)
    position_registry.open.clear()
    autotrader.executed_strategy_ids.clear()
    autotrader.ai_candidate_lock.clear()
    autotrader.ai_candidate_wait_cooldowns.clear()
    item = high_quality_item(symbol=symbol, side="LONG", price=100)
    item.rank = 1
    item.score = 88
    item.fund_confirm_count = 3

    monkeypatch.setattr(settings, "trade_mode", "live")
    monkeypatch.setattr(settings, "live_trading_enabled", True)
    monkeypatch.setattr(settings, "live_use_test_order", False)
    monkeypatch.setattr(settings, "auto_trading_candidate_mode", "strict")
    monkeypatch.setattr(settings, "auto_trading_candidate_limit", 1)
    monkeypatch.setattr(settings, "auto_trading_use_active_strategy_filter", False)
    monkeypatch.setattr(settings, "max_open_positions", 1)
    monkeypatch.setattr(radar_engine, "top50", [item])
    monkeypatch.setattr(strategy_registry, "active", lambda: None)
    monkeypatch.setattr(performance_guard, "summary", lambda: {"recovery_mode": False, "pnl": 10, "trades": 20, "win_rate": 0.6, "recent_win_rate": 0.6, "loss_streak": 0})
    monkeypatch.setattr(performance_guard, "precheck_candidate", lambda candidate: (True, "ok"))
    monkeypatch.setattr(learned_risk_guard, "precheck_item", lambda *args, **kwargs: (True, SimpleNamespace(asdict=lambda: {})))

    async def fake_scan(force_refresh=False):
        return radar_engine.top50

    async def fake_generate(review_item, position_context=None):
        plan = rule_strategy_generator.generate(review_item)
        plan.raw["provider"] = "codex_cli"
        plan.raw["strategy_contract"] = build_rule_contract(review_item, plan)
        return plan

    async def fake_account_context(open_positions):
        return (
            {"mode": "live", "configured": True, "canTrade": True, "walletBalance": 1000, "availableBalance": 1000},
            {"equity": 1000, "available_balance": 1000, "trade_mode": "live", "execution_context": "live", "open_positions": open_positions, "max_open_positions": 1},
        )

    def fake_decide(review_item, plan, account, market, paper_probe=False):
        return ExecutionPlan(
            decision="OPEN",
            mode="paper",
            symbol=plan.symbol,
            side=plan.side,
            dynamic_margin=25,
            dynamic_leverage=2,
            quantity=0.5,
            entry_price=plan.ideal_entry_price,
            stop_loss=plan.stop_loss,
            tp1=plan.tp1,
            tp2=plan.tp2,
            tp1_close_ratio=0.5,
            tp2_close_ratio=1.0,
            management_mode="TEST",
            cooldown_after_trade=60,
            reason="risk model forced paper mode",
            notional=50,
            risk_usdt=1,
            risk_pct=0.1,
            strategy_contract=plan.raw.get("strategy_contract", {}),
        )

    async def fail_live_open(*args, **kwargs):
        raise AssertionError("live executor must not run when exec_plan.mode is paper")

    monkeypatch.setattr(radar_engine, "scan", fake_scan)
    monkeypatch.setattr("backend.trading.autotrader.openai_strategy_client.generate", fake_generate)
    monkeypatch.setattr(autotrader, "_account_context", fake_account_context)
    monkeypatch.setattr("backend.trading.autotrader.auto_trading_risk_model.decide", fake_decide)
    monkeypatch.setattr("backend.trading.autotrader.live_executor.open_position", fail_live_open)
    monkeypatch.setattr(autotrader, "_real_live_execution_guard", lambda: (_ for _ in ()).throw(AssertionError("live guard should not run for paper execution mode")))

    try:
        result = __import__("asyncio").run(autotrader._run_once_locked())
        opened = position_registry.list_open()

        assert result["results"][0]["decision"] == "OPEN_PAPER"
        assert result["results"][0]["reason"].startswith("live execution blocked because exec_plan.mode=paper")
        assert len(opened) == 1
        assert opened[0].position_id.startswith("pos_")
        assert not opened[0].position_id.startswith("livepos")
    finally:
        cleanup_symbol(symbol)


def test_paper_top_active_strategy_miss_still_enters_ai_review(monkeypatch):
    position_registry.open.clear()
    symbol = "TOPSTRATEGYMISSUSDT"
    with db.conn() as conn:
        conn.execute("DELETE FROM ai_decision_observations WHERE symbol=?", (symbol,))
    item = high_quality_item(symbol=symbol, side="LONG", price=100)
    item.rank = 1
    item.score = 80
    item.fund_confirm_count = 3
    active = {
        "strategy_id": "active_short_only",
        "name": "short only",
        "status": "ACTIVE",
        "filters": {"allowed_sides": ["SHORT"], "min_score": 1, "min_fund_confirm": 3},
        "metrics": {"eligible": True},
    }
    monkeypatch.setattr(settings, "trade_mode", "live")
    monkeypatch.setattr(settings, "live_trading_enabled", False)
    monkeypatch.setattr(settings, "auto_trading_candidate_mode", "paper_top")
    monkeypatch.setattr(settings, "auto_trading_candidate_min_score", 55.0)
    monkeypatch.setattr(settings, "auto_trading_candidate_limit", 1)
    monkeypatch.setattr(settings, "auto_trading_use_active_strategy_filter", True)
    monkeypatch.setattr(settings, "max_open_positions", 1)
    monkeypatch.setattr(radar_engine, "top50", [item])
    async def fake_scan(force_refresh=False):
        return radar_engine.top50
    monkeypatch.setattr(radar_engine, "scan", fake_scan)
    monkeypatch.setattr(strategy_registry, "active", lambda: active)
    monkeypatch.setattr(strategy_registry, "list", lambda limit=50: [])
    monkeypatch.setattr(performance_guard, "summary", lambda: {"recovery_mode": False, "pnl": 0, "trades": 0, "win_rate": 0, "recent_win_rate": 0, "loss_streak": 0})
    monkeypatch.setattr(autotrader, "_paper_probe_batch", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("paper_top must not fallback to probe")))
    called = []

    async def fake_generate(review_item, position_context=None):
        called.append(review_item.symbol)
        return StrategyPlan(
            "ai_wait_strategy_miss",
            "WAIT",
            review_item.symbol,
            "NEUTRAL",
            review_item.price,
            review_item.price,
            review_item.price,
            0,
            0,
            0,
            0,
            "ai review wait",
            "WAIT_FOR_CONFIRMATION",
        )

    monkeypatch.setattr("backend.trading.autotrader.openai_strategy_client.generate", fake_generate)

    try:
        result = __import__("asyncio").run(autotrader._run_once_locked())
        observation = result["results"][0]["ai_decision_observation"]
        rows = [
            row for row in ai_strategy_feedback.decision_observations(limit=20) if row.get("symbol") == symbol
        ]

        assert called == [symbol]
        assert result["results"][0]["decision"] in {"KEEP_WAITING", "WAIT_EXPIRED", "PAPER_OBSERVE"}
        assert observation["recorded"] is True
        assert rows
        assert rows[0]["stage"] == "ai_wait"
    finally:
        with db.conn() as conn:
            conn.execute("DELETE FROM ai_decision_observations WHERE symbol=?", (symbol,))

def test_paper_top_recovery_still_enters_ai_review_without_probe_fallback(monkeypatch):
    position_registry.open.clear()
    item = high_quality_item(symbol="TOPRECOVERYUSDT", side="LONG", price=100)
    item.rank = 1
    item.score = 80
    item.fund_confirm_count = 3
    monkeypatch.setattr(settings, "trade_mode", "live")
    monkeypatch.setattr(settings, "live_trading_enabled", False)
    monkeypatch.setattr(settings, "auto_trading_candidate_mode", "paper_top")
    monkeypatch.setattr(settings, "auto_trading_candidate_min_score", 55.0)
    monkeypatch.setattr(settings, "auto_trading_candidate_limit", 1)
    monkeypatch.setattr(settings, "evolved_strategy_required_in_recovery", True)
    monkeypatch.setattr(settings, "max_open_positions", 1)
    monkeypatch.setattr(radar_engine, "top50", [item])
    async def fake_scan(force_refresh=False):
        return radar_engine.top50
    monkeypatch.setattr(radar_engine, "scan", fake_scan)
    monkeypatch.setattr(strategy_registry, "active", lambda: None)
    monkeypatch.setattr(performance_guard, "summary", lambda: {"recovery_mode": True, "pnl": -1, "trades": 20, "win_rate": 0.3, "recent_win_rate": 0.2, "loss_streak": 3})
    monkeypatch.setattr(autotrader, "_paper_probe_batch", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("paper_top recovery must not fallback to probe")))
    called = []

    async def fake_generate(review_item, position_context=None):
        called.append(review_item.symbol)
        return StrategyPlan(
            "ai_wait_recovery",
            "WAIT",
            review_item.symbol,
            "NEUTRAL",
            review_item.price,
            review_item.price,
            review_item.price,
            0,
            0,
            0,
            0,
            "ai recovery wait",
            "WAIT_FOR_CONFIRMATION",
        )

    monkeypatch.setattr("backend.trading.autotrader.openai_strategy_client.generate", fake_generate)

    result = __import__("asyncio").run(autotrader._run_once_locked())

    assert called == ["TOPRECOVERYUSDT"]
    assert result["results"][0]["decision"] in {"KEEP_WAITING", "WAIT_EXPIRED", "PAPER_OBSERVE"}

def test_ai_review_stale_candidate_releases_lock_before_codex_generation(monkeypatch):
    position_registry.open.clear()
    item = high_quality_item(symbol="WAITDECAYUSDT", side="LONG", price=100)
    item.rank = 30
    item.score = 80
    item.fund_confirm_count = 3
    item.ts_ms = now_ms() - 300000
    monkeypatch.setattr(settings, "trade_mode", "live")
    monkeypatch.setattr(settings, "live_trading_enabled", False)
    monkeypatch.setattr(settings, "auto_trading_candidate_mode", "paper_top")
    monkeypatch.setattr(settings, "auto_trading_candidate_min_score", 55.0)
    monkeypatch.setattr(settings, "auto_trading_candidate_limit", 1)
    monkeypatch.setattr(settings, "ai_candidate_max_stale_seconds", 60)
    monkeypatch.setattr(settings, "max_open_positions", 1)
    monkeypatch.setattr(radar_engine, "top50", [item])
    async def fake_scan(force_refresh=False):
        return radar_engine.top50
    monkeypatch.setattr(radar_engine, "scan", fake_scan)
    monkeypatch.setattr(strategy_registry, "active", lambda: None)
    monkeypatch.setattr(performance_guard, "summary", lambda: {"recovery_mode": False, "pnl": 0, "trades": 0, "win_rate": 0, "recent_win_rate": 0, "loss_streak": 0})

    async def fake_generate(review_item, position_context=None):
        raise AssertionError("stale candidate should not invoke Codex generation")
        return StrategyPlan(
            "ai_wait_decay",
            "WAIT",
            review_item.symbol,
            "NEUTRAL",
            review_item.price,
            review_item.price,
            review_item.price,
            0,
            0,
            0,
            0,
            "ai wait decay",
            "WAIT_FOR_CONFIRMATION",
        )

    monkeypatch.setattr("backend.trading.autotrader.openai_strategy_client.generate", fake_generate)

    result = __import__("asyncio").run(autotrader._run_once_locked())

    assert result["results"][0]["decision"] == "SKIP_STALE_CANDIDATE"
    assert "candidate_snapshot_stale" in result["results"][0]["reason"]
    assert result["results"][0]["candidate_lock_released"] is True
    assert result["results"][0]["candidate_wait_cooldown_until_ms"] > now_ms()
    assert autotrader.candidate_lock_status() == {}


def test_paper_top_retries_latest_geometry_candidate_after_pre_ai_stale(monkeypatch):
    position_registry.open.clear()
    autotrader.ai_candidate_lock = {}
    autotrader.ai_candidate_wait_cooldowns.clear()
    stale = high_quality_item(symbol="STALEGEOMETRYUSDT", side="LONG", price=100)
    stale.rank = 1
    fresh = high_quality_item(symbol="FRESHGEOMETRYUSDT", side="LONG", price=100)
    fresh.rank = 2
    stale_neutral = high_quality_item(symbol=stale.symbol, side="LONG", price=100)
    stale_neutral.direction = "NEUTRAL"
    stale_neutral.fund_confirm_count = 0
    stale_neutral.score = 0

    monkeypatch.setattr(settings, "trade_mode", "live")
    monkeypatch.setattr(settings, "live_trading_enabled", False)
    monkeypatch.setattr(settings, "auto_trading_candidate_mode", "paper_top")
    monkeypatch.setattr(settings, "auto_trading_candidate_min_score", 55.0)
    monkeypatch.setattr(settings, "max_open_positions", 1)
    monkeypatch.setattr(radar_engine, "top50", [stale, fresh])
    monkeypatch.setattr(strategy_registry, "active", lambda: None)
    monkeypatch.setattr(performance_guard, "summary", lambda: {"recovery_mode": False, "pnl": 0, "trades": 0, "win_rate": 0})

    batches = iter([([stale], "paper_top"), ([fresh], "paper_top")])
    monkeypatch.setattr(autotrader, "_candidate_batch", lambda performance: next(batches))

    async def fake_geometry_order(candidates, candidate_source, performance_context=None):
        return candidates, [{"symbol": candidates[0].symbol, "geometry_status": "ok"}]

    async def fake_prepare(item, force_scan=True):
        report = {
            "market_refresh_degraded": False,
            "trade_price": {
                "ok": True,
                "stale": False,
                "safe_for_execution": True,
                "error": "",
            },
        }
        if item.symbol == stale.symbol:
            return stale_neutral, report
        return item, report

    async def fake_account_context(open_positions):
        return {}, {"open_positions": []}

    calls = []

    async def fake_generate(review_item, position_context=None):
        calls.append(review_item.symbol)
        return StrategyPlan(
            "fresh_wait",
            "WAIT",
            review_item.symbol,
            "NEUTRAL",
            review_item.price,
            review_item.price,
            review_item.price,
            0,
            0,
            0,
            0,
            "wait after fresh retry",
            "WAIT_FOR_CONFIRMATION",
        )

    monkeypatch.setattr(autotrader, "_geometry_supported_candidate_order", fake_geometry_order)
    monkeypatch.setattr(autotrader, "_prepare_latest_item_for_ai", fake_prepare)
    monkeypatch.setattr(autotrader, "_account_context", fake_account_context)
    monkeypatch.setattr("backend.trading.autotrader.openai_strategy_client.generate", fake_generate)

    result = __import__("asyncio").run(autotrader._run_once_locked())

    assert calls == ["FRESHGEOMETRYUSDT"]
    assert result["results"][0]["decision"] == "SKIP_STALE_CANDIDATE"
    assert result["results"][0]["retry_candidates_added"] == ["FRESHGEOMETRYUSDT"]
    assert result["results"][1]["symbol"] == "FRESHGEOMETRYUSDT"


def test_ai_review_plan_side_mismatch_after_codex_regenerates_latest_side(monkeypatch):
    position_registry.open.clear()
    autotrader.ai_candidate_lock = {}
    autotrader.ai_candidate_wait_cooldowns.clear()
    item = high_quality_item(symbol="SIDEDRIFTUSDT", side="LONG", price=100)
    item.rank = 3
    item.score = 80
    item.fund_confirm_count = 2
    latest = high_quality_item(symbol="SIDEDRIFTUSDT", side="SHORT", price=100)
    latest.rank = 3
    latest.score = 80
    latest.fund_confirm_count = 2
    monkeypatch.setattr(settings, "trade_mode", "live")
    monkeypatch.setattr(settings, "live_trading_enabled", False)
    monkeypatch.setattr(settings, "auto_trading_candidate_mode", "paper_top")
    monkeypatch.setattr(settings, "auto_trading_candidate_min_score", 55.0)
    monkeypatch.setattr(settings, "auto_trading_candidate_limit", 1)
    monkeypatch.setattr(settings, "max_open_positions", 1)
    monkeypatch.setattr(radar_engine, "top50", [item])
    async def fake_scan(force_refresh=False):
        return radar_engine.top50
    monkeypatch.setattr(radar_engine, "scan", fake_scan)
    monkeypatch.setattr(strategy_registry, "active", lambda: None)
    monkeypatch.setattr(performance_guard, "summary", lambda: {"recovery_mode": False, "pnl": 0, "trades": 0, "win_rate": 0, "recent_win_rate": 0, "loss_streak": 0})

    calls = []

    async def fake_generate(review_item, position_context=None):
        calls.append(
            {
                "side": review_item.direction,
                "retry": bool(((position_context or {}).get("candidate_selection") or {}).get("retry_context")),
                "refresh": ((position_context or {}).get("candidate_selection") or {}).get("pre_ai_market_refresh") or {},
            }
        )
        if len(calls) == 1:
            radar_engine.top50 = [latest]
            side = "LONG"
            action = "OPEN_LONG"
        else:
            side = "SHORT"
            action = "OPEN_SHORT"
            review_item = latest
        entry = 100.0
        radar_engine.top50 = [latest]
        plan = StrategyPlan(
            "codex_side_drift",
            action,
            review_item.symbol,
            side,
            99.8,
            100.2,
            entry,
            101.0 if side == "SHORT" else 99.0,
            99.0 if side == "SHORT" else 101.0,
            98.0 if side == "SHORT" else 102.5,
            70,
            "test codex open before side drift",
            "",
            raw={"provider": "codex_cli"},
        )
        plan.raw["strategy_contract"] = build_rule_contract(review_item, plan)
        return plan

    def fake_decide(review_item, plan, account, market, paper_probe=False):
        assert review_item.direction == "SHORT"
        assert plan.side == "SHORT"
        assert plan.raw["drift_regeneration"]["previous_plan_side"] == "LONG"
        return ExecutionPlan(
            "OBSERVE",
            "paper",
            review_item.symbol,
            plan.side,
            0,
            0,
            0,
            plan.ideal_entry_price,
            plan.stop_loss,
            plan.tp1,
            plan.tp2,
            0.5,
            1.0,
            "NONE",
            120,
            "regenerated latest side reached risk model",
        )

    monkeypatch.setattr("backend.trading.autotrader.openai_strategy_client.generate", fake_generate)
    monkeypatch.setattr("backend.trading.autotrader.auto_trading_risk_model.decide", fake_decide)

    result = __import__("asyncio").run(autotrader._run_once_locked())

    assert [call["side"] for call in calls] == ["LONG", "SHORT"]
    assert calls[0]["refresh"]["force_scan"] is True
    assert calls[1]["retry"] is True
    assert result["results"][0]["decision"] == "OBSERVE"
    assert result["results"][0]["reason"] == "regenerated latest side reached risk model"
    assert result["results"][0]["drift_regenerated"] is True


def test_paper_probe_batch_supplies_sampling_candidate_in_recovery(monkeypatch):
    item = high_quality_item(symbol="PROBELOWUSDT", side="SHORT", price=100)
    item.score = 34
    item.fund_confirm_count = 1
    monkeypatch.setattr(settings, "trade_mode", "live")
    monkeypatch.setattr(settings, "live_trading_enabled", False)
    monkeypatch.setattr(settings, "paper_probe_enabled", True)
    monkeypatch.setattr(settings, "paper_probe_min_score_floor", 18.0)
    monkeypatch.setattr(settings, "paper_probe_min_fund_confirm", 1)
    monkeypatch.setattr(settings, "paper_probe_min_direction_confirmations", 4)
    monkeypatch.setattr(settings, "auto_trading_candidate_limit", 1)
    monkeypatch.setattr(radar_engine, "top50", [item])

    candidates, source = autotrader._paper_probe_batch({"recovery_mode": True}, "paper_probe_no_candidates")
    diagnostics = autotrader.candidate_diagnostics({"recovery_mode": True})

    assert source == "paper_probe_no_candidates"
    assert [candidate.symbol for candidate in candidates] == ["PROBELOWUSDT"]
    assert diagnostics["counts"]["paper_probe_candidates"] == 1

def test_paper_probe_entry_requires_ai_strategy_generation(monkeypatch):
    position_registry.open.clear()
    item = high_quality_item(symbol="PROBECODEXUSDT", side="LONG", price=100)
    item.score = 34
    item.fund_confirm_count = 1
    calls = []

    monkeypatch.setattr(settings, "trade_mode", "live")
    monkeypatch.setattr(settings, "live_trading_enabled", False)
    monkeypatch.setattr(settings, "max_open_positions", 1)
    monkeypatch.setattr(radar_engine, "top50", [item])
    async def fake_scan(force_refresh=False):
        return radar_engine.top50
    monkeypatch.setattr(radar_engine, "scan", fake_scan)
    monkeypatch.setattr(strategy_registry, "active", lambda: None)
    monkeypatch.setattr(autotrader, "_candidate_batch", lambda performance: ([item], "paper_probe_no_candidates"))

    async def fake_account_context(open_positions):
        return (
            {"mode": "paper", "configured": True},
            {
                "equity": 1000,
                "available_balance": 1000,
                "open_positions": 0,
                "max_open_positions": 1,
                "trade_mode": "paper",
                "execution_context": "paper_closed_loop",
            },
        )

    async def fake_price(symbol):
        return 100

    async def fake_generate(review_item, position_context=None):
        calls.append((review_item.symbol, (position_context or {}).get("candidate_selection")))
        plan = rule_strategy_generator.generate_probe(review_item)
        plan.raw["provider"] = "codex_cli"
        return plan

    def fake_decide(review_item, plan, account, market, paper_probe=False):
        assert plan.raw["provider"] == "codex_cli"
        assert paper_probe is True
        return ExecutionPlan(
            decision="OBSERVE",
            mode="paper" if item.symbol == "RISKFIRSTUSDT" else "live",
            symbol=plan.symbol,
            side=plan.side,
            dynamic_margin=0,
            dynamic_leverage=0,
            quantity=0,
            entry_price=plan.ideal_entry_price,
            stop_loss=plan.stop_loss,
            tp1=plan.tp1,
            tp2=plan.tp2,
            tp1_close_ratio=0.5,
            tp2_close_ratio=1.0,
            management_mode="NONE",
            cooldown_after_trade=60,
            reason="test stop before execution",
        )

    monkeypatch.setattr(autotrader, "_account_context", fake_account_context)
    monkeypatch.setattr(market_service, "price_for", fake_price)
    monkeypatch.setattr(openai_strategy_client, "generate", fake_generate)
    monkeypatch.setattr(auto_trading_risk_model, "decide", fake_decide)

    result = __import__("asyncio").run(autotrader._run_once_locked())

    assert calls[0][0] == "PROBECODEXUSDT"
    assert calls[0][1]["source"] == "paper_probe_no_candidates"
    assert calls[0][1]["paper_validation"] is False
    assert calls[0][1]["paper_probe"] is True
    assert calls[0][1]["strict_candidate"] is False
    assert calls[0][1]["latest_market_required"] is True
    assert calls[0][1]["pre_ai_market_refresh"]["force_scan"] is True
    assert result["results"][0]["decision"] == "OBSERVE"

def test_ai_trade_director_owns_full_trade_responsibility_chain(monkeypatch):
    position_registry.open.clear()
    item = high_quality_item(symbol="DIRECTORUSDT", side="LONG", price=100)
    monkeypatch.setattr(settings, "trade_mode", "live")
    monkeypatch.setattr(settings, "live_trading_enabled", False)
    monkeypatch.setattr(settings, "auto_trading_candidate_mode", "paper_top")
    monkeypatch.setattr(radar_engine, "top50", [item])

    status = ai_trade_director.status()

    assert status["responsible"] == "AITradeDirector"
    roles = {row["role"] for row in status["responsibility_chain"]}
    assert {"AITradeDirector", "cyqnt-trd", "Codex/DeepSeek", "Jesse", "risk_model", "executor", "position_manager", "learning"}.issubset(roles)
    assert status["candidate_symbols"] == ["DIRECTORUSDT"]
    assert status["candidate_evidence"][0]["cyqnt_feature_enhancement"]["symbol"] == item.symbol
    assert status["candidate_evidence"][0]["jesse_audit"]["execution_permission"] is False
    assert status["safety"]["real_order_allowed"] is False

def test_ai_trade_director_run_once_wraps_autotrader_execution(monkeypatch):
    position_registry.open.clear()
    item = high_quality_item(symbol="DIRECTORRUNUSDT", side="LONG", price=100)
    monkeypatch.setattr(settings, "trade_mode", "live")
    monkeypatch.setattr(settings, "live_trading_enabled", False)
    monkeypatch.setattr(settings, "auto_trading_candidate_mode", "paper_top")
    monkeypatch.setattr(radar_engine, "top50", [item])

    async def fake_run_once():
        return {"results": [{"symbol": item.symbol, "decision": "PAPER_OBSERVE", "reason": "test"}]}

    monkeypatch.setattr(autotrader, "run_once", fake_run_once)

    out = __import__("asyncio").run(ai_trade_director.run_once(source="manual"))

    assert out["results"][0]["decision"] == "PAPER_OBSERVE"
    assert out["trade_director"]["responsible"] == "AITradeDirector"
    assert out["trade_director"]["decision_summary"]["symbol"] == item.symbol
    assert out["trade_director"]["jesse_research"]["execution_permission"] is False


def test_ai_trade_director_manual_run_cannot_bypass_live_loop_guard(monkeypatch):
    position_registry.open.clear()
    item = high_quality_item(symbol="DIRECTORBLOCKUSDT", side="LONG", price=100)
    monkeypatch.setattr(settings, "trade_mode", "live")
    monkeypatch.setattr(settings, "live_trading_enabled", True)
    monkeypatch.setattr(settings, "live_use_test_order", False)
    monkeypatch.setattr(settings, "auto_trading_candidate_mode", "paper_top")
    monkeypatch.setattr(radar_engine, "top50", [item])
    monkeypatch.setattr(
        autotrader,
        "loop_start_guard",
        lambda: (False, "strict_candidate_mode_required_for_auto_loop", {"recovery_mode": False}),
    )

    async def fail_run_once():
        raise AssertionError("manual run_once must not execute when live loop guard fails")

    monkeypatch.setattr(autotrader, "run_once", fail_run_once)

    out = __import__("asyncio").run(ai_trade_director.run_once(source="manual"))

    assert out["results"][0]["decision"] == "DIRECTOR_BLOCKED"
    assert out["results"][0]["reason"] == "strict_candidate_mode_required_for_auto_loop"
    assert out["results"][0]["live_order_surface"] is True
    assert out["trade_director"]["manual_override"] is False


def test_trade_acceptance_runner_verifies_real_paper_cycle(monkeypatch):
    cleanup_symbol("ACCEPTUSDT")
    cleanup_acceptance_strategies()

    async def fake_generate(item, position_context=None):
        plan = rule_strategy_generator.generate_probe(item)
        plan.strategy_id = "codex_acceptance_test"
        plan.raw["provider"] = "codex_cli"
        return plan

    monkeypatch.setattr("backend.trading.trade_acceptance.openai_strategy_client.generate", fake_generate)
    try:
        out = __import__("asyncio").run(trade_acceptance_runner.run_controlled_paper_cycle())

        stages = {stage["name"]: stage for stage in out["stages"]}
        assert out["ok"] is True
        assert stages["scan_candidate"]["ok"] is True
        assert stages["cyqnt_evidence"]["ok"] is True
        assert stages["strategy_plan"]["ok"] is True
        assert stages["risk_model"]["ok"] is True
        assert stages["paper_open"]["ok"] is True
        assert stages["position_manager_active"]["ok"] is True
        assert stages["paper_close"]["ok"] is True
        assert stages["learning_close_recorded"]["ok"] is True
        assert out["result"]["symbol"] == "ACCEPTUSDT"
        assert out["result"]["close_reason"] == "ACCEPTANCE_TP2"
        assert out["real_order_allowed"] is False
        assert not position_registry.has_symbol("ACCEPTUSDT")
    finally:
        cleanup_symbol("ACCEPTUSDT")
        cleanup_acceptance_strategies()


def test_trade_acceptance_report_separates_existing_open_positions(monkeypatch):
    existing = SimpleNamespace(position_id="pos_existing")
    monkeypatch.setattr(
        "backend.trading.trade_acceptance.position_registry.list_open",
        lambda: [existing],
    )
    monkeypatch.setattr(
        "backend.trading.trade_acceptance.position_registry.list_closed",
        lambda limit=200: [{"position_id": "pos_acceptance_closed"}],
    )

    report = trade_acceptance_runner._report(
        [trade_acceptance_runner._stage("paper_close", True, {})],
        {"pos_existing"},
        set(),
        result={"position_id": "pos_acceptance_closed"},
    )

    assert report["position_delta"]["open_positions_after"] == 1
    assert report["position_delta"]["opened_during_test"] == ["pos_acceptance_closed"]
    assert report["position_delta"]["open_test_positions_after"] == []


def test_strategy_plan_schema_is_strict_for_codex_structured_output():
    schema = json.loads(Path("backend/ai_strategy/strategy_plan.schema.json").read_text(encoding="utf-8"))

    def visit(node):
        if isinstance(node, dict):
            if node.get("type") == "object" or (isinstance(node.get("type"), list) and "object" in node["type"]):
                assert node.get("additionalProperties") is False
            for value in node.values():
                visit(value)
        elif isinstance(node, list):
            for value in node:
                visit(value)

    visit(schema)

def test_production_acceptance_rejects_real_order_without_explicit_confirm():
    out = __import__("asyncio").run(
        production_acceptance_runner.run(mode="real_order", confirm_real_order="", manage_seconds=0)
    )

    assert out["ok"] is False
    assert out["result"]["blocked"] == "real_order_requires_explicit_confirm"
    assert out["stages"][0]["name"] == "production_safety"
    assert out["stages"][0]["ok"] is False


def test_production_acceptance_rejects_open_decision_when_execution_mode_is_paper(monkeypatch):
    item = high_quality_item(symbol="PAPERMODEUSDT", side="LONG", price=100)
    radar_engine.top50 = [item]

    async def fake_scan(force_refresh=False):
        radar_engine.top50 = [item]
        radar_engine.last_scan_id = "scan_paper_mode_test"
        radar_engine.last_scan_time = "12:00:00"
        return radar_engine.top50

    async def fake_generate(candidate, position_context=None):
        plan = rule_strategy_generator.generate(candidate)
        plan.raw["provider"] = "codex_cli"
        return plan

    async def fake_account_context(open_positions):
        return (
            {"mode": "live", "configured": True, "canTrade": True, "walletBalance": 1000, "availableBalance": 1000},
            {"equity": 1000, "available_balance": 1000, "trade_mode": "live", "execution_context": "live"},
        )

    monkeypatch.setattr(radar_engine, "scan", fake_scan)
    monkeypatch.setattr(radar_engine, "select_ai_candidates", lambda items: [item])
    monkeypatch.setattr(autotrader, "_candidate_batch", lambda performance: ([item], "strict"))
    monkeypatch.setattr(openai_strategy_client, "generate", fake_generate)
    monkeypatch.setattr(autotrader, "_account_context", fake_account_context)
    monkeypatch.setattr(
        auto_trading_risk_model,
        "decide",
        lambda item, plan, account, market, paper_probe=False: ExecutionPlan(
            decision="OPEN",
            mode="paper",
            symbol=plan.symbol,
            side=plan.side,
            dynamic_margin=25,
            dynamic_leverage=2,
            quantity=0.5,
            entry_price=plan.ideal_entry_price,
            stop_loss=plan.stop_loss,
            tp1=plan.tp1,
            tp2=plan.tp2,
            tp1_close_ratio=0.5,
            tp2_close_ratio=1.0,
            management_mode="TEST",
            cooldown_after_trade=60,
            reason="paper mode only",
            notional=50,
            risk_usdt=1,
            risk_pct=0.1,
            strategy_contract=plan.raw.get("strategy_contract", {}),
        ),
    )

    out = __import__("asyncio").run(production_acceptance_runner.run(mode="preflight", manage_seconds=0))
    stages = {stage["name"]: stage for stage in out["stages"]}

    assert out["result"]["blocked"] == "risk_model_not_open"
    assert stages["risk_model"]["ok"] is False
    assert stages["risk_model"]["evidence"]["decision"] == "OPEN"
    assert stages["risk_model"]["evidence"]["mode"] == "paper"
    assert stages["risk_model"]["evidence"]["risk_attempts"][0]["live_mode_ok"] is False


def test_production_acceptance_uses_strict_candidates_not_paper_top(monkeypatch):
    strict_item = high_quality_item(symbol="STRICTUSDT", side="LONG", price=100)
    paper_item = high_quality_item(symbol="PAPERUSDT", side="LONG", price=100)
    paper_item.score = 30
    paper_item.fund_confirm_count = 2
    radar_engine.top50 = [strict_item, paper_item]

    async def fake_scan(force_refresh=False):
        radar_engine.top50 = [strict_item, paper_item]
        radar_engine.last_scan_id = "scan_strict_test"
        radar_engine.last_scan_time = "12:00:00"
        return radar_engine.top50

    seen = {}

    async def fake_generate(item, position_context=None):
        seen["symbol"] = item.symbol
        return StrategyPlan(
            strategy_id="fake_wait",
            action="WAIT",
            symbol=item.symbol,
            side="NEUTRAL",
            entry_zone_low=item.price,
            entry_zone_high=item.price,
            ideal_entry_price=item.price,
            stop_loss=0,
            tp1=0,
            tp2=0,
            confidence=0,
            reason="test wait",
            wait_type="TEST",
            expire_after_seconds=30,
            raw={"provider": "codex_cli", "model": "test"},
        )

    monkeypatch.setattr(radar_engine, "scan", fake_scan)
    monkeypatch.setattr(autotrader, "_candidate_batch", lambda performance: ([paper_item], "paper_top"))
    monkeypatch.setattr(openai_strategy_client, "generate", fake_generate)

    out = __import__("asyncio").run(production_acceptance_runner.run(mode="preflight", manage_seconds=0))
    stages = {stage["name"]: stage for stage in out["stages"]}

    assert stages["candidate_selection"]["evidence"]["candidate_source"] == "strict"
    assert stages["candidate_selection"]["evidence"]["configured_candidate_source"] == "paper_top"
    assert stages["candidate_selection"]["evidence"]["candidate_symbols"] == ["STRICTUSDT"]
    assert seen["symbol"] == "STRICTUSDT"

def test_production_acceptance_tries_next_strict_candidate_after_wait(monkeypatch):
    first = high_quality_item(symbol="FIRSTUSDT", side="LONG", price=100)
    second = high_quality_item(symbol="SECONDUSDT", side="SHORT", price=100)
    radar_engine.top50 = [first, second]

    async def fake_scan(force_refresh=False):
        radar_engine.top50 = [first, second]
        radar_engine.last_scan_id = "scan_multi_plan_test"
        radar_engine.last_scan_time = "12:00:00"
        return radar_engine.top50

    seen = []

    async def fake_generate(item, position_context=None):
        seen.append(item.symbol)
        if item.symbol == "FIRSTUSDT":
            return StrategyPlan(
                strategy_id="fake_wait",
                action="WAIT",
                symbol=item.symbol,
                side="NEUTRAL",
                entry_zone_low=item.price,
                entry_zone_high=item.price,
                ideal_entry_price=item.price,
                stop_loss=0,
                tp1=0,
                tp2=0,
                confidence=0,
                reason="first wait",
                wait_type="TEST",
                expire_after_seconds=30,
                raw={"provider": "codex_cli", "model": "test"},
            )
        plan = rule_strategy_generator.generate(item)
        plan.raw["provider"] = "codex_cli"
        return plan

    async def fake_account_context(open_positions):
        return (
            {"mode": "test", "configured": True, "canTrade": True, "walletBalance": 1000, "availableBalance": 1000},
            {"equity": 1000, "available_balance": 1000, "execution_context": "test"},
        )

    monkeypatch.setattr(radar_engine, "scan", fake_scan)
    monkeypatch.setattr(radar_engine, "select_ai_candidates", lambda items: [first, second])
    monkeypatch.setattr(autotrader, "_candidate_batch", lambda performance: ([first], "strict"))
    monkeypatch.setattr(openai_strategy_client, "generate", fake_generate)
    monkeypatch.setattr(autotrader, "_account_context", fake_account_context)
    monkeypatch.setattr(
        auto_trading_risk_model,
        "decide",
        lambda item, plan, account, market, paper_probe=False: ExecutionPlan(
            decision="OPEN",
            mode="paper",
            symbol=plan.symbol,
            side=plan.side,
            dynamic_margin=25,
            dynamic_leverage=2,
            quantity=0.5,
            entry_price=plan.ideal_entry_price,
            stop_loss=plan.stop_loss,
            tp1=plan.tp1,
            tp2=plan.tp2,
            tp1_close_ratio=0.5,
            tp2_close_ratio=1.0,
            management_mode="TEST",
            cooldown_after_trade=60,
            reason="test open",
            notional=50,
            risk_usdt=1,
            risk_pct=0.001,
            strategy_contract=plan.raw.get("strategy_contract", {}),
        ),
    )

    out = __import__("asyncio").run(production_acceptance_runner.run(mode="preflight", manage_seconds=0))
    stages = {stage["name"]: stage for stage in out["stages"]}

    assert seen == ["FIRSTUSDT", "SECONDUSDT"]
    assert stages["ai_strategy_plan"]["ok"] is True
    assert stages["ai_strategy_plan"]["evidence"]["symbol"] == "SECONDUSDT"
    assert [row["action"] for row in stages["ai_strategy_plan"]["evidence"]["plan_attempts"]] == ["WAIT", "OPEN_SHORT"]

def test_production_acceptance_tries_next_candidate_after_risk_reject(monkeypatch):
    first = high_quality_item(symbol="RISKFIRSTUSDT", side="LONG", price=100)
    second = high_quality_item(symbol="RISKSECONDUSDT", side="LONG", price=100)
    radar_engine.top50 = [first, second]

    async def fake_scan(force_refresh=False):
        radar_engine.top50 = [first, second]
        radar_engine.last_scan_id = "scan_risk_next_test"
        radar_engine.last_scan_time = "12:00:00"
        return radar_engine.top50

    async def fake_generate(item, position_context=None):
        plan = rule_strategy_generator.generate(item)
        plan.raw["provider"] = "codex_cli"
        return plan

    async def fake_account_context(open_positions):
        return (
            {"mode": "test", "configured": True, "canTrade": True, "walletBalance": 1000, "availableBalance": 1000},
            {"equity": 1000, "available_balance": 1000, "execution_context": "test"},
        )

    def fake_decide(item, plan, account, market, paper_probe=False):
        return ExecutionPlan(
            decision="OBSERVE" if item.symbol == "RISKFIRSTUSDT" else "OPEN",
            mode="paper" if item.symbol == "RISKFIRSTUSDT" else "live",
            symbol=plan.symbol,
            side=plan.side,
            dynamic_margin=0 if item.symbol == "RISKFIRSTUSDT" else 25,
            dynamic_leverage=0 if item.symbol == "RISKFIRSTUSDT" else 2,
            quantity=0 if item.symbol == "RISKFIRSTUSDT" else 0.5,
            entry_price=plan.ideal_entry_price,
            stop_loss=plan.stop_loss,
            tp1=plan.tp1,
            tp2=plan.tp2,
            tp1_close_ratio=0.5,
            tp2_close_ratio=1.0,
            management_mode="NONE" if item.symbol == "RISKFIRSTUSDT" else "TEST",
            cooldown_after_trade=60,
            reason="risk reject first" if item.symbol == "RISKFIRSTUSDT" else "risk open second",
            notional=0 if item.symbol == "RISKFIRSTUSDT" else 50,
            risk_usdt=0 if item.symbol == "RISKFIRSTUSDT" else 1,
            risk_pct=0 if item.symbol == "RISKFIRSTUSDT" else 0.001,
            strategy_contract=plan.raw.get("strategy_contract", {}),
        )

    monkeypatch.setattr(radar_engine, "scan", fake_scan)
    monkeypatch.setattr(radar_engine, "select_ai_candidates", lambda items: [first, second])
    monkeypatch.setattr(autotrader, "_candidate_batch", lambda performance: ([first, second], "strict"))
    monkeypatch.setattr(openai_strategy_client, "generate", fake_generate)
    monkeypatch.setattr(autotrader, "_account_context", fake_account_context)
    monkeypatch.setattr(auto_trading_risk_model, "decide", fake_decide)

    out = __import__("asyncio").run(production_acceptance_runner.run(mode="preflight", manage_seconds=0))
    stages = {stage["name"]: stage for stage in out["stages"]}

    assert stages["ai_strategy_plan"]["ok"] is True
    assert stages["ai_strategy_plan"]["evidence"]["symbol"] == "RISKSECONDUSDT"
    assert stages["risk_model"]["ok"] is True
    assert [row["decision"] for row in stages["risk_model"]["evidence"]["risk_attempts"]] == ["OBSERVE", "OPEN"]

def test_production_acceptance_shadow_reviews_when_no_strict_candidate(monkeypatch):
    review_item = high_quality_item(symbol="REVIEWUSDT", side="SHORT", price=100)
    review_item.fund_confirm_count = 2
    radar_engine.top50 = [review_item]

    async def fake_scan(force_refresh=False):
        radar_engine.top50 = [review_item]
        radar_engine.last_scan_id = "scan_review_shadow_test"
        radar_engine.last_scan_time = "12:00:00"
        return radar_engine.top50

    async def fake_generate(item, position_context=None):
        plan = rule_strategy_generator.generate_probe(item)
        plan.raw["provider"] = "codex_cli"
        return plan

    monkeypatch.setattr(radar_engine, "scan", fake_scan)
    monkeypatch.setattr(radar_engine, "select_ai_candidates", lambda items: [])
    monkeypatch.setattr(radar_engine, "select_ai_review_candidates", lambda items: [review_item])
    monkeypatch.setattr(autotrader, "_candidate_batch", lambda performance: ([review_item], "strict_review"))
    monkeypatch.setattr(openai_strategy_client, "generate", fake_generate)

    out = __import__("asyncio").run(production_acceptance_runner.run(mode="preflight", manage_seconds=0))
    stages = {stage["name"]: stage for stage in out["stages"]}

    assert out["result"]["blocked"] == "no_strict_production_candidates"
    assert stages["candidate_selection"]["ok"] is False
    assert stages["shadow_strategy_plan"]["ok"] is True
    assert stages["shadow_strategy_plan"]["evidence"]["not_counted_as_production"] is True
    assert stages["shadow_strategy_plan"]["evidence"]["action"] == "OPEN_SHORT"

def test_live_readiness_shows_paper_is_not_terminal_while_recovery_blocks_live(monkeypatch):
    monkeypatch.setattr(settings, "paper_probe_enabled", True)
    monkeypatch.setattr(settings, "live_trading_enabled", False)
    monkeypatch.setattr(settings, "trade_mode", "live")
    monkeypatch.setattr(settings, "live_use_test_order", True)
    monkeypatch.setattr(settings, "auto_trading_use_performance_guard", True)
    monkeypatch.setattr(settings, "max_open_positions", 1)
    monkeypatch.setattr(settings, "attach_protection_orders", True)
    monkeypatch.setattr(binance_rest, "last_public_source", "mainnet")
    monkeypatch.setattr(binance_futures, "configured", lambda: True)
    monkeypatch.setattr(
        performance_guard,
        "summary",
        lambda: {"trades": 143, "win_rate": 0.3776, "recent_win_rate": 0.38, "pnl": -6.9796, "loss_streak": 0, "recovery_mode": True},
    )
    monkeypatch.setattr(
        trade_attributor,
        "summary",
        lambda: {"sample_count": 143, "global_win_rate": 0.3776, "global_profit_factor": 0.8, "global_pnl": -6.9796},
    )
    monkeypatch.setattr(
        learning_data_audit,
        "summary",
        lambda: {
            "production_grade": True,
            "trust_level": "PRODUCTION",
            "reasons": [],
            "sources": {"combined_samples": 143, "replay_samples": 0, "real_closed_samples_with_radar": 143},
        },
    )
    monkeypatch.setattr(position_manager, "summary", lambda: {"open_count": 1, "floating_pnl": -0.1, "total_pnl": -6.9, "used_margin": 25})
    monkeypatch.setattr(position_registry, "list_open", lambda: [object()])

    report = live_readiness.summary()

    assert report["paper_is_terminal"] is False
    assert report["current_stage"] == "shadow_live"
    phase = next(x for x in report["phases"] if x["name"] == "live_test_order")
    assert phase["allowed"] is False
    blocker_codes = {block["code"] for block in report["blockers"]}
    assert "performance_recovery_mode" in blocker_codes
    assert "open_position_exists" in blocker_codes

def test_live_readiness_allows_test_order_stage_when_paper_graduates(monkeypatch):
    monkeypatch.setattr(settings, "paper_probe_enabled", True)
    monkeypatch.setattr(settings, "live_trading_enabled", False)
    monkeypatch.setattr(settings, "trade_mode", "live")
    monkeypatch.setattr(settings, "live_use_test_order", True)
    monkeypatch.setattr(settings, "auto_trading_use_performance_guard", True)
    monkeypatch.setattr(settings, "max_open_positions", 1)
    monkeypatch.setattr(settings, "attach_protection_orders", True)
    monkeypatch.setattr(binance_rest, "last_public_source", "mainnet")
    monkeypatch.setattr(binance_futures, "configured", lambda: True)
    monkeypatch.setattr(
        performance_guard,
        "summary",
        lambda: {"trades": 80, "win_rate": 0.56, "recent_win_rate": 0.54, "pnl": 4.2, "loss_streak": 0, "recovery_mode": False},
    )
    monkeypatch.setattr(
        trade_attributor,
        "summary",
        lambda: {"sample_count": 120, "global_win_rate": 0.55, "global_profit_factor": 1.22, "global_pnl": 3.1},
    )
    monkeypatch.setattr(
        learning_data_audit,
        "summary",
        lambda: {
            "production_grade": True,
            "trust_level": "PRODUCTION",
            "reasons": [],
            "sources": {"combined_samples": 120, "replay_samples": 0, "real_closed_samples_with_radar": 120},
        },
    )
    monkeypatch.setattr(position_manager, "summary", lambda: {"open_count": 0, "floating_pnl": 0, "total_pnl": 4.2, "used_margin": 0})
    monkeypatch.setattr(position_registry, "list_open", lambda: [])

    report = live_readiness.summary()

    assert report["current_stage"] == "live_test_order"
    phase = next(x for x in report["phases"] if x["name"] == "live_test_order")
    assert phase["allowed"] is True
    assert report["paper_is_terminal"] is False

def test_live_readiness_blocks_when_learning_data_is_not_production_grade(monkeypatch):
    monkeypatch.setattr(settings, "paper_probe_enabled", True)
    monkeypatch.setattr(settings, "live_trading_enabled", False)
    monkeypatch.setattr(settings, "trade_mode", "live")
    monkeypatch.setattr(settings, "live_use_test_order", True)
    monkeypatch.setattr(settings, "auto_trading_use_performance_guard", True)
    monkeypatch.setattr(settings, "max_open_positions", 1)
    monkeypatch.setattr(settings, "attach_protection_orders", True)
    monkeypatch.setattr(binance_rest, "last_public_source", "mainnet")
    monkeypatch.setattr(binance_futures, "configured", lambda: True)
    monkeypatch.setattr(
        performance_guard,
        "summary",
        lambda: {"trades": 80, "win_rate": 0.56, "recent_win_rate": 0.54, "pnl": 4.2, "loss_streak": 0, "recovery_mode": False},
    )
    monkeypatch.setattr(
        trade_attributor,
        "summary",
        lambda: {"sample_count": 120, "global_win_rate": 0.55, "global_profit_factor": 1.22, "global_pnl": 3.1},
    )
    monkeypatch.setattr(
        learning_data_audit,
        "summary",
        lambda: {
            "production_grade": False,
            "trust_level": "LOW",
            "reasons": ["replay_dominated", "market_backtest_missing"],
            "sources": {"combined_samples": 120, "replay_samples": 119, "real_closed_samples_with_radar": 1},
        },
    )
    monkeypatch.setattr(position_manager, "summary", lambda: {"open_count": 0, "floating_pnl": 0, "total_pnl": 4.2, "used_margin": 0})
    monkeypatch.setattr(position_registry, "list_open", lambda: [])

    report = live_readiness.summary()

    phase = next(x for x in report["phases"] if x["name"] == "live_test_order")
    assert phase["allowed"] is False
    blocker_codes = {block["code"] for block in report["blockers"]}
    assert "learning_data_not_production_grade" in blocker_codes
    assert report["metrics"]["learning_data_quality"]["trust_level"] == "LOW"


def test_exchange_reconciliation_flags_local_exchange_drift(monkeypatch):
    monkeypatch.setattr(settings, "trade_mode", "live")
    monkeypatch.setattr(settings, "binance_testnet", False)
    monkeypatch.setattr(binance_futures, "configured", lambda: True)
    p = Position(
        position_id="livepos_local",
        strategy_id="strategy",
        source_signal_id="scan",
        symbol="BTCUSDT",
        side="LONG",
        status="OPEN",
        stage="Stage 1",
        score=80,
        entry_price=100,
        current_price=100,
        quantity=0.001,
        initial_quantity=0.001,
        margin=25,
        leverage=1,
        stop_loss=99,
        tp1=101,
        tp2=103,
        best_price=100,
        exchange_open_order={"orderId": 10, "clientOrderId": "hy_open_strategy"},
        exchange_stop_order={"orderId": 11, "clientOrderId": "hy_sl_strategy"},
        exchange_tp_order={"orderId": 12, "clientOrderId": "hy_tp_strategy"},
    )

    async def fake_exchange_positions():
        return [
            {
                "symbol": "ETHUSDT",
                "side": "SHORT",
                "positionAmt": -0.5,
                "entryPrice": 2000,
                "markPrice": 1990,
                "unRealizedProfit": 1.5,
                "positionSide": "BOTH",
            }
        ]

    async def fake_open_orders(symbol=None):
        return [
            {
                "symbol": "ETHUSDT",
                "clientOrderId": "hy_sl_orphan",
                "orderId": 99,
                "type": "STOP_MARKET",
            }
        ]

    monkeypatch.setattr(position_registry, "list_open", lambda: [p])
    monkeypatch.setattr(account_service, "get_exchange_positions", fake_exchange_positions)
    monkeypatch.setattr(account_service, "get_open_orders", fake_open_orders)

    report = __import__("asyncio").run(exchange_reconciliation.refresh(force=True))

    codes = {issue["code"] for issue in report["issues"]}
    assert report["ok"] is False
    assert "exchange_position_without_local_record" in codes
    assert "local_live_position_missing_on_exchange" in codes
    assert "live_position_missing_stop_order" in codes
    assert "live_position_missing_tp_order" in codes
    assert "orphan_strategy_protection_order" in codes


def test_exchange_reconciliation_flags_replaced_protection_orphans(monkeypatch):
    monkeypatch.setattr(settings, "trade_mode", "live")
    monkeypatch.setattr(settings, "binance_testnet", False)
    monkeypatch.setattr(binance_futures, "configured", lambda: True)
    monkeypatch.setattr(position_registry, "list_open", lambda: [])

    async def fake_exchange_positions():
        return []

    async def fake_open_orders(symbol=None):
        return [
            {"symbol": "BTCUSDT", "clientOrderId": "hy_slr_livepos_orphan", "orderId": 111, "type": "STOP_MARKET"},
            {"symbol": "BTCUSDT", "clientOrderId": "hy_tpr_livepos_orphan", "orderId": 112, "type": "TAKE_PROFIT_MARKET"},
        ]

    monkeypatch.setattr(account_service, "get_exchange_positions", fake_exchange_positions)
    monkeypatch.setattr(account_service, "get_open_orders", fake_open_orders)

    report = __import__("asyncio").run(exchange_reconciliation.refresh(force=True))

    orphan_ids = {issue.get("clientOrderId") for issue in report["issues"] if issue["code"] == "orphan_strategy_protection_order"}
    assert "hy_slr_livepos_orphan" in orphan_ids
    assert "hy_tpr_livepos_orphan" in orphan_ids


def test_live_readiness_blocks_exchange_reconciliation_failure(monkeypatch):
    monkeypatch.setattr(settings, "paper_probe_enabled", True)
    monkeypatch.setattr(settings, "live_trading_enabled", False)
    monkeypatch.setattr(settings, "trade_mode", "live")
    monkeypatch.setattr(settings, "live_use_test_order", True)
    monkeypatch.setattr(settings, "auto_trading_use_performance_guard", True)
    monkeypatch.setattr(settings, "max_open_positions", 1)
    monkeypatch.setattr(settings, "attach_protection_orders", True)
    monkeypatch.setattr(binance_rest, "last_public_source", "mainnet")
    monkeypatch.setattr(binance_futures, "configured", lambda: True)
    monkeypatch.setattr(
        performance_guard,
        "summary",
        lambda: {"trades": 80, "win_rate": 0.56, "recent_win_rate": 0.54, "pnl": 4.2, "loss_streak": 0, "recovery_mode": False},
    )
    monkeypatch.setattr(
        trade_attributor,
        "summary",
        lambda: {"sample_count": 120, "global_win_rate": 0.55, "global_profit_factor": 1.22, "global_pnl": 3.1},
    )
    monkeypatch.setattr(
        learning_data_audit,
        "summary",
        lambda: {
            "production_grade": True,
            "trust_level": "PRODUCTION",
            "reasons": [],
            "sources": {"combined_samples": 120, "replay_samples": 0, "real_closed_samples_with_radar": 120},
        },
    )
    monkeypatch.setattr(position_manager, "summary", lambda: {"open_count": 0, "floating_pnl": 0, "total_pnl": 4.2, "used_margin": 0})
    monkeypatch.setattr(position_registry, "list_open", lambda: [])
    monkeypatch.setattr(
        exchange_reconciliation,
        "cached",
        lambda: {
            "ok": False,
            "ts_ms": now_ms(),
            "age_seconds": 0.0,
            "skipped": False,
            "reason": "",
            "local_live_positions": [],
            "exchange_positions": [],
            "open_order_count": 0,
            "issues": [{"code": "local_live_position_missing_on_exchange"}],
        },
    )

    report = live_readiness.summary()

    phase = next(x for x in report["phases"] if x["name"] == "live_test_order")
    blocker_codes = {block["code"] for block in report["blockers"]}
    assert phase["allowed"] is False
    assert "exchange_reconciliation_failed" in blocker_codes
    assert report["metrics"]["execution"]["exchange_reconciliation"]["issue_count"] == 1


def test_production_acceptance_real_gate_does_not_deadlock_on_live_enabled(monkeypatch):
    monkeypatch.setattr(settings, "trade_mode", "live")
    monkeypatch.setattr(settings, "live_trading_enabled", True)
    monkeypatch.setattr(settings, "live_use_test_order", False)
    monkeypatch.setattr(binance_futures, "configured", lambda: True)
    monkeypatch.setattr(binance_rest, "last_public_source", "mainnet")

    readiness = {
        "phases": [
            {
                "name": "micro_live",
                "allowed": False,
                "blockers": [{"code": "live_trading_already_enabled", "stage": "all"}],
            }
        ]
    }

    ok, reason = production_acceptance_runner._live_gate("real_order", readiness)

    assert ok is True
    assert reason == "ok"


def test_production_acceptance_real_gate_keeps_other_readiness_blockers(monkeypatch):
    monkeypatch.setattr(settings, "trade_mode", "live")
    monkeypatch.setattr(settings, "live_trading_enabled", True)
    monkeypatch.setattr(settings, "live_use_test_order", False)
    monkeypatch.setattr(binance_futures, "configured", lambda: True)
    monkeypatch.setattr(binance_rest, "last_public_source", "mainnet")

    readiness = {
        "phases": [
            {
                "name": "micro_live",
                "allowed": False,
                "blockers": [
                    {"code": "live_trading_already_enabled", "stage": "all"},
                    {"code": "learning_data_not_production_grade", "stage": "live_test_order"},
                ],
            }
        ]
    }

    ok, reason = production_acceptance_runner._live_gate("real_order", readiness)

    assert ok is False
    assert "learning_data_not_production_grade" in reason


def _prg_pass_readiness(phases=None):
    prg_metrics = {"sharpe": 1.2, "max_drawdown": 0.05, "winrate": 0.62, "profit_factor": 1.35}
    return {
        "prg": {
            "score": 100,
            "level": "FULL_LIVE_READY",
            "allowed": True,
            "mode": "FULL_LIVE",
            "reason": "",
            "metrics": prg_metrics,
        },
        "metrics": {"prg": prg_metrics},
        "phases": phases or [{"name": "micro_live", "blockers": []}],
    }


def test_autotrader_real_live_guard_allows_supervised_enable_marker(monkeypatch):
    monkeypatch.setattr(settings, "trade_mode", "live")
    monkeypatch.setattr(settings, "live_trading_enabled", True)
    monkeypatch.setattr(settings, "live_use_test_order", False)
    monkeypatch.setattr(settings, "production_acceptance_max_age_seconds", 60)
    db.set_kv("live_executor.trading_freeze", {"active": False})
    db.set_kv(
        "production_acceptance.last_report",
        {
            "ok": True,
            "mode": "real_order",
            "finished_ms": now_ms(),
            "production_acceptance": {"passed": True},
        },
    )
    monkeypatch.setattr(
        live_readiness,
        "summary",
        lambda: _prg_pass_readiness(
            [
                {
                    "name": "micro_live",
                    "blockers": [{"code": "live_trading_already_enabled", "stage": "all"}],
                }
            ]
        ),
    )

    ok, reason, _ = autotrader._real_live_execution_guard()

    assert ok is True
    assert reason == "ok"


def test_autotrader_real_live_guard_blocks_readiness_failures(monkeypatch):
    monkeypatch.setattr(settings, "trade_mode", "live")
    monkeypatch.setattr(settings, "live_trading_enabled", True)
    monkeypatch.setattr(settings, "live_use_test_order", False)
    monkeypatch.setattr(
        live_readiness,
        "summary",
        lambda: {
            "phases": [
                {
                    "name": "micro_live",
                    "blockers": [
                        {"code": "live_trading_already_enabled", "stage": "all"},
                        {"code": "learning_data_not_production_grade", "stage": "live_test_order"},
                    ],
                }
            ]
        },
    )

    ok, reason, _ = autotrader._real_live_execution_guard()

    assert ok is False
    assert "learning_data_not_production_grade" in reason


def test_autotrader_real_live_guard_requires_recent_real_order_acceptance(monkeypatch):
    monkeypatch.setattr(settings, "trade_mode", "live")
    monkeypatch.setattr(settings, "live_trading_enabled", True)
    monkeypatch.setattr(settings, "live_use_test_order", False)
    monkeypatch.setattr(settings, "production_acceptance_max_age_seconds", 60)
    monkeypatch.setattr(
        live_readiness,
        "summary",
        lambda: _prg_pass_readiness(
            [
                {
                    "name": "micro_live",
                    "blockers": [{"code": "live_trading_already_enabled", "stage": "all"}],
                }
            ]
        ),
    )

    db.set_kv("production_acceptance.last_report", {"ok": False, "mode": "preflight", "finished_ms": now_ms()})
    ok, reason, _ = autotrader._real_live_execution_guard()
    assert ok is False
    assert reason == "production_acceptance_not_passed"

    db.set_kv(
        "production_acceptance.last_report",
        {
            "ok": True,
            "mode": "preflight",
            "finished_ms": now_ms(),
            "production_acceptance": {"passed": True},
        },
    )
    ok, reason, _ = autotrader._real_live_execution_guard()
    assert ok is False
    assert reason == "production_acceptance_mode_not_real_order"

    db.set_kv(
        "production_acceptance.last_report",
        {
            "ok": True,
            "mode": "real_order",
            "finished_ms": now_ms() - 120_000,
            "production_acceptance": {"passed": True},
        },
    )
    ok, reason, _ = autotrader._real_live_execution_guard()
    assert ok is False
    assert reason == "production_acceptance_stale"

    db.set_kv(
        "production_acceptance.last_report",
        {
            "ok": True,
            "mode": "real_order",
            "finished_ms": now_ms(),
            "production_acceptance": {"passed": True},
        },
    )
    ok, reason, _ = autotrader._real_live_execution_guard()
    assert ok is True
    assert reason == "ok"


def test_autotrader_real_live_guard_blocks_after_unprotected_live_freeze(monkeypatch):
    monkeypatch.setattr(settings, "trade_mode", "live")
    monkeypatch.setattr(settings, "live_trading_enabled", True)
    monkeypatch.setattr(settings, "live_use_test_order", False)
    monkeypatch.setattr(settings, "production_acceptance_max_age_seconds", 60)
    monkeypatch.setattr(
        live_readiness,
        "summary",
        lambda: {
            "phases": [
                {
                    "name": "micro_live",
                    "blockers": [{"code": "live_trading_already_enabled", "stage": "all"}],
                }
            ]
        },
    )
    db.set_kv(
        "production_acceptance.last_report",
        {
            "ok": True,
            "mode": "real_order",
            "finished_ms": now_ms(),
            "production_acceptance": {"passed": True},
        },
    )
    db.set_kv(
        "live_executor.trading_freeze",
        {"active": True, "reason": "UNPROTECTED_LIVE_FORCE_CLOSE_FAILED", "symbol": "BTCUSDT"},
    )

    try:
        ok, reason, evidence = autotrader._real_live_execution_guard()
    finally:
        db.set_kv("live_executor.trading_freeze", {"active": False})

    assert ok is False
    assert reason == "live_trading_freeze:UNPROTECTED_LIVE_FORCE_CLOSE_FAILED"
    assert evidence["trading_freeze"]["symbol"] == "BTCUSDT"


def test_live_executor_requires_protection_orders_for_real_order(monkeypatch):
    monkeypatch.setattr(settings, "live_trading_enabled", True)
    monkeypatch.setattr(settings, "live_use_test_order", False)
    monkeypatch.setattr(settings, "attach_protection_orders", False)
    monkeypatch.setattr(binance_futures, "configured", lambda: True)
    plan = ExecutionPlan(
        decision="OPEN",
        mode="live",
        symbol="BTCUSDT",
        side="LONG",
        dynamic_margin=25,
        dynamic_leverage=1,
        quantity=0.001,
        entry_price=100,
        stop_loss=99,
        tp1=101,
        tp2=103,
        tp1_close_ratio=0.5,
        tp2_close_ratio=1.0,
        management_mode="RISK_LOCK_AND_TRAIL",
        cooldown_after_trade=300,
        reason="test",
    )

    with pytest.raises(RuntimeError, match="PROTECTION_ORDERS_REQUIRED_FOR_REAL_ORDER"):
        __import__("asyncio").run(live_executor.open_position("scan", "strategy", 80, plan))


def test_live_executor_does_not_send_close_orders_for_test_order_positions(monkeypatch):
    async def fail_market_close(*args, **kwargs):
        raise AssertionError("market_close should not be called for live test-order positions")

    monkeypatch.setattr(binance_futures, "market_close", fail_market_close)
    p = Position(
        position_id="livepos_test",
        strategy_id="strategy",
        source_signal_id="scan",
        symbol="BTCUSDT",
        side="LONG",
        status="OPEN",
        stage="Stage 1",
        score=80,
        entry_price=100,
        current_price=100,
        quantity=0.001,
        initial_quantity=0.001,
        margin=25,
        leverage=1,
        stop_loss=99,
        tp1=101,
        tp2=103,
        best_price=100,
        lock_status="LIVE_TEST_ORDER",
        exchange_open_order={"testOrder": True},
    )

    close_result = __import__("asyncio").run(live_executor.close_position(p))
    reduce_result = __import__("asyncio").run(live_executor.reduce_position(p, 0.0005))

    assert close_result["closeSkipped"] is True
    assert reduce_result["reduceSkipped"] is True


def test_live_executor_requires_real_fill_quantity_and_price():
    plan = ExecutionPlan(
        decision="OPEN",
        mode="live",
        symbol="BTCUSDT",
        side="LONG",
        dynamic_margin=25,
        dynamic_leverage=1,
        quantity=0.001,
        entry_price=100,
        stop_loss=99,
        tp1=101,
        tp2=103,
        tp1_close_ratio=0.5,
        tp2_close_ratio=1.0,
        management_mode="RISK_LOCK_AND_TRAIL",
        cooldown_after_trade=300,
        reason="test",
    )

    with pytest.raises(RuntimeError, match="LIVE_ORDER_NOT_FILLED"):
        live_executor._fill_from_order({"orderId": 1, "executedQty": "0", "avgPrice": "100"}, plan)
    with pytest.raises(RuntimeError, match="LIVE_ORDER_FILL_PRICE_MISSING"):
        live_executor._fill_from_order({"orderId": 1, "executedQty": "0.001", "avgPrice": "0"}, plan)

    qty, price = live_executor._fill_from_order({"orderId": 1, "executedQty": "0.002", "cumQuote": "0.202"}, plan)
    assert qty == 0.002
    assert price == 101.0


def test_live_executor_blocks_order_below_exchange_constraints(monkeypatch):
    monkeypatch.setattr(settings, "live_trading_enabled", True)
    monkeypatch.setattr(settings, "live_use_test_order", False)
    monkeypatch.setattr(settings, "attach_protection_orders", True)
    monkeypatch.setattr(binance_futures, "configured", lambda: True)
    monkeypatch.setattr(live_readiness, "summary", lambda: _prg_pass_readiness())
    monkeypatch.setattr(
        binance_futures,
        "_symbol_filters",
        {
            "BTCUSDT": {
                "raw": {"symbol": "BTCUSDT"},
                "filters": {
                    "LOT_SIZE": {"minQty": "0.01", "maxQty": "1000", "stepSize": "0.01"},
                    "MARKET_LOT_SIZE": {"minQty": "0.01", "maxQty": "1000", "stepSize": "0.01"},
                    "PRICE_FILTER": {"tickSize": "0.01"},
                    "MIN_NOTIONAL": {"notional": "5"},
                },
            }
        },
    )

    async def fake_exchange_info():
        return {}

    async def fail_change_margin_type(*args, **kwargs):
        raise AssertionError("exchange settings must not be touched after local constraint failure")

    monkeypatch.setattr(binance_futures, "exchange_info", fake_exchange_info)
    monkeypatch.setattr(binance_futures, "change_margin_type", fail_change_margin_type)
    plan = ExecutionPlan(
        decision="OPEN",
        mode="live",
        symbol="BTCUSDT",
        side="LONG",
        dynamic_margin=25,
        dynamic_leverage=1,
        quantity=0.001,
        entry_price=100,
        stop_loss=99,
        tp1=101,
        tp2=103,
        tp1_close_ratio=0.5,
        tp2_close_ratio=1.0,
        management_mode="RISK_LOCK_AND_TRAIL",
        cooldown_after_trade=300,
        reason="test",
    )

    with pytest.raises(RuntimeError, match="EXCHANGE_ORDER_CONSTRAINT_FAILED"):
        __import__("asyncio").run(live_executor.open_position("scan", "strategy", 80, plan))


def test_live_executor_blocks_real_order_when_account_is_hedge_mode(monkeypatch):
    monkeypatch.setattr(settings, "trade_mode", "live")
    monkeypatch.setattr(settings, "live_trading_enabled", True)
    monkeypatch.setattr(settings, "live_use_test_order", False)
    monkeypatch.setattr(settings, "attach_protection_orders", True)
    monkeypatch.setattr(binance_futures, "configured", lambda: True)
    monkeypatch.setattr(live_readiness, "summary", lambda: _prg_pass_readiness())

    async def fake_exchange_info():
        return {}

    async def fake_position_side_dual():
        return True

    async def fail_change_margin_type(*args, **kwargs):
        raise AssertionError("hedge mode must block before exchange settings or orders")

    monkeypatch.setattr(binance_futures, "exchange_info", fake_exchange_info)
    monkeypatch.setattr(binance_futures, "position_side_dual", fake_position_side_dual, raising=False)
    monkeypatch.setattr(binance_futures, "change_margin_type", fail_change_margin_type)
    plan = ExecutionPlan(
        decision="OPEN",
        mode="live",
        symbol="BTCUSDT",
        side="LONG",
        dynamic_margin=25,
        dynamic_leverage=1,
        quantity=0.01,
        entry_price=100,
        stop_loss=99,
        tp1=101,
        tp2=103,
        tp1_close_ratio=0.5,
        tp2_close_ratio=1.0,
        management_mode="RISK_LOCK_AND_TRAIL",
        cooldown_after_trade=300,
        reason="test",
    )

    with pytest.raises(RuntimeError, match="BINANCE_HEDGE_MODE_UNSUPPORTED"):
        __import__("asyncio").run(live_executor.open_position("scan", "strategy", 80, plan))


def test_market_order_uses_same_market_step_for_check_and_payload(monkeypatch):
    monkeypatch.setattr(
        binance_futures,
        "_symbol_filters",
        {
            "STEPMKTUSDT": {
                "raw": {"symbol": "STEPMKTUSDT"},
                "filters": {
                    "LOT_SIZE": {"minQty": "0.001", "maxQty": "1000", "stepSize": "0.001"},
                    "MARKET_LOT_SIZE": {"minQty": "0.01", "maxQty": "1000", "stepSize": "0.01"},
                    "PRICE_FILTER": {"tickSize": "0.01"},
                    "MIN_NOTIONAL": {"notional": "1"},
                },
            }
        },
    )
    captured = {}

    async def fake_new_order(**params):
        captured.update(params)
        return {"orderId": 1}

    monkeypatch.setattr(binance_futures, "new_order", fake_new_order)

    constraints = binance_futures.market_order_constraints("STEPMKTUSDT", 0.019, 100)
    __import__("asyncio").run(binance_futures.market_open("STEPMKTUSDT", "BUY", 0.019))

    assert constraints["formatted_quantity"] == "0.01"
    assert captured["quantity"] == constraints["formatted_quantity"]


def test_live_executor_cancels_own_protection_orders_after_full_close(monkeypatch):
    monkeypatch.setattr(settings, "live_trading_enabled", True)
    monkeypatch.setattr(binance_futures, "configured", lambda: True)
    cancel_calls = []

    async def fake_exchange_info():
        return {}

    async def fake_market_close(*args, **kwargs):
        return {"orderId": 333, "status": "FILLED"}

    async def fake_cancel_order(symbol, order_id=None, orig_client_order_id=None):
        cancel_calls.append((symbol, order_id, orig_client_order_id))
        return {"symbol": symbol, "orderId": order_id, "clientOrderId": orig_client_order_id}

    async def fake_open_orders(symbol=None):
        return []

    monkeypatch.setattr(binance_futures, "exchange_info", fake_exchange_info)
    monkeypatch.setattr(binance_futures, "market_close", fake_market_close)
    monkeypatch.setattr(binance_futures, "cancel_order", fake_cancel_order)
    monkeypatch.setattr(binance_futures, "open_orders", fake_open_orders)
    p = Position(
        position_id="livepos_real",
        strategy_id="strategy",
        source_signal_id="scan",
        symbol="BTCUSDT",
        side="LONG",
        status="OPEN",
        stage="Stage 1",
        score=80,
        entry_price=100,
        current_price=100,
        quantity=0.001,
        initial_quantity=0.001,
        margin=25,
        leverage=1,
        stop_loss=99,
        tp1=101,
        tp2=103,
        best_price=100,
        exchange_stop_order={"orderId": 111, "clientOrderId": "hy_sl_strategy"},
        exchange_tp_order={"clientOrderId": "hy_tp_strategy"},
    )

    result = __import__("asyncio").run(live_executor.close_position(p))

    assert result["protection_cancel"][0]["ok"] is True
    assert ("BTCUSDT", 111, None) in cancel_calls
    assert ("BTCUSDT", None, "hy_tp_strategy") in cancel_calls


def test_live_executor_cancels_attached_stop_after_take_profit_attach_failure(monkeypatch):
    monkeypatch.setattr(settings, "live_trading_enabled", True)
    monkeypatch.setattr(settings, "live_use_test_order", False)
    monkeypatch.setattr(settings, "attach_protection_orders", True)
    monkeypatch.setattr(binance_futures, "configured", lambda: True)
    monkeypatch.setattr(live_readiness, "summary", lambda: _prg_pass_readiness())
    cancel_calls = []

    async def fake_exchange_info():
        return {}

    async def fake_change_margin_type(*args, **kwargs):
        return {}

    async def fake_change_leverage(*args, **kwargs):
        return {}

    async def fake_market_open(*args, **kwargs):
        return {"orderId": 10, "executedQty": "0.001", "avgPrice": "100"}

    async def fake_stop_market(*args, **kwargs):
        return {"orderId": 111, "clientOrderId": "hy_sl_strategy"}

    async def fake_take_profit_market(*args, **kwargs):
        raise RuntimeError("tp_failed")

    async def fake_market_close(*args, **kwargs):
        return {"orderId": 222, "status": "FILLED"}

    async def fake_cancel_order(symbol, order_id=None, orig_client_order_id=None):
        cancel_calls.append((symbol, order_id, orig_client_order_id))
        return {"symbol": symbol, "orderId": order_id, "clientOrderId": orig_client_order_id}

    async def fake_open_orders(symbol=None):
        return []

    monkeypatch.setattr(binance_futures, "exchange_info", fake_exchange_info)
    monkeypatch.setattr(binance_futures, "change_margin_type", fake_change_margin_type)
    monkeypatch.setattr(binance_futures, "change_leverage", fake_change_leverage)
    monkeypatch.setattr(binance_futures, "market_open", fake_market_open)
    monkeypatch.setattr(binance_futures, "stop_market", fake_stop_market)
    monkeypatch.setattr(binance_futures, "take_profit_market", fake_take_profit_market)
    monkeypatch.setattr(binance_futures, "market_close", fake_market_close)
    monkeypatch.setattr(binance_futures, "cancel_order", fake_cancel_order)
    monkeypatch.setattr(binance_futures, "open_orders", fake_open_orders)
    plan = ExecutionPlan(
        decision="OPEN",
        mode="live",
        symbol="BTCUSDT",
        side="LONG",
        dynamic_margin=25,
        dynamic_leverage=1,
        quantity=0.001,
        entry_price=100,
        stop_loss=99,
        tp1=101,
        tp2=103,
        tp1_close_ratio=0.5,
        tp2_close_ratio=1.0,
        management_mode="RISK_LOCK_AND_TRAIL",
        cooldown_after_trade=300,
        reason="test",
    )

    with pytest.raises(RuntimeError, match="PROTECTION_ORDER_FAILED_FORCE_CLOSE_ATTEMPTED"):
        __import__("asyncio").run(live_executor.open_position("scan", "strategy", 80, plan))

    assert ("BTCUSDT", 111, None) in cancel_calls


def test_live_executor_records_unprotected_live_position_when_force_close_fails(monkeypatch):
    position_registry.open.clear()
    cleanup_symbol("BTCUSDT")
    db.set_kv("live_executor.trading_freeze", {"active": False})
    monkeypatch.setattr(settings, "live_trading_enabled", True)
    monkeypatch.setattr(settings, "live_use_test_order", False)
    monkeypatch.setattr(settings, "attach_protection_orders", True)
    monkeypatch.setattr(binance_futures, "configured", lambda: True)
    monkeypatch.setattr(live_readiness, "summary", lambda: _prg_pass_readiness())

    async def fake_exchange_info():
        return {}

    async def fake_change_margin_type(*args, **kwargs):
        return {}

    async def fake_change_leverage(*args, **kwargs):
        return {}

    async def fake_market_open(*args, **kwargs):
        return {"orderId": 10, "executedQty": "0.001", "avgPrice": "100", "clientOrderId": "hy_open_strategy"}

    async def fake_stop_market(*args, **kwargs):
        return {"orderId": 111, "clientOrderId": "hy_sl_strategy"}

    async def fake_take_profit_market(*args, **kwargs):
        raise RuntimeError("tp_failed")

    async def fake_market_close(*args, **kwargs):
        raise RuntimeError("force_close_failed")

    async def fake_cancel_order(*args, **kwargs):
        return {}

    async def fake_open_orders(symbol=None):
        return []

    monkeypatch.setattr(binance_futures, "exchange_info", fake_exchange_info)
    monkeypatch.setattr(binance_futures, "change_margin_type", fake_change_margin_type)
    monkeypatch.setattr(binance_futures, "change_leverage", fake_change_leverage)
    monkeypatch.setattr(binance_futures, "market_open", fake_market_open)
    monkeypatch.setattr(binance_futures, "stop_market", fake_stop_market)
    monkeypatch.setattr(binance_futures, "take_profit_market", fake_take_profit_market)
    monkeypatch.setattr(binance_futures, "market_close", fake_market_close)
    monkeypatch.setattr(binance_futures, "cancel_order", fake_cancel_order)
    monkeypatch.setattr(binance_futures, "open_orders", fake_open_orders)
    plan = ExecutionPlan(
        decision="OPEN",
        mode="live",
        symbol="BTCUSDT",
        side="LONG",
        dynamic_margin=25,
        dynamic_leverage=1,
        quantity=0.001,
        entry_price=100,
        stop_loss=99,
        tp1=101,
        tp2=103,
        tp1_close_ratio=0.5,
        tp2_close_ratio=1.0,
        management_mode="RISK_LOCK_AND_TRAIL",
        cooldown_after_trade=300,
        reason="test",
    )

    try:
        with pytest.raises(RuntimeError, match="PROTECTION_ORDER_FAILED_FORCE_CLOSE_FAILED"):
            __import__("asyncio").run(live_executor.open_position("scan", "strategy", 80, plan))

        open_positions = position_registry.list_open()
        freeze = db.get_kv("live_executor.trading_freeze", {})
    finally:
        cleanup_symbol("BTCUSDT")
        db.set_kv("live_executor.trading_freeze", {"active": False})

    assert len(open_positions) == 1
    assert open_positions[0].lock_status == "UNPROTECTED_LIVE_FORCE_CLOSE_FAILED"
    assert open_positions[0].exchange_stop_order["orderId"] == 111
    assert freeze["active"] is True
    assert freeze["symbol"] == "BTCUSDT"


def test_live_executor_replace_protection_freezes_when_old_cancel_fails(monkeypatch):
    db.set_kv("live_executor.trading_freeze", {"active": False})
    monkeypatch.setattr(settings, "live_trading_enabled", True)
    monkeypatch.setattr(binance_futures, "configured", lambda: True)

    async def fake_exchange_info():
        return {}

    async def fake_stop_market(*args, **kwargs):
        return {"orderId": 31, "clientOrderId": "hy_slr_livepos_cancel_fail"}

    async def fake_take_profit_market(*args, **kwargs):
        return {"orderId": 32, "clientOrderId": "hy_tpr_livepos_cancel_fail"}

    async def fake_cancel_order(*args, **kwargs):
        raise RuntimeError("cancel_failed")

    monkeypatch.setattr(binance_futures, "exchange_info", fake_exchange_info)
    monkeypatch.setattr(binance_futures, "stop_market", fake_stop_market)
    monkeypatch.setattr(binance_futures, "take_profit_market", fake_take_profit_market)
    monkeypatch.setattr(binance_futures, "cancel_order", fake_cancel_order)
    p = Position(
        position_id="livepos_cancel_fail",
        strategy_id="strategy_cancel_fail",
        source_signal_id="scan",
        symbol="BTCUSDT",
        side="LONG",
        status="OPEN",
        stage="Stage 1",
        score=80,
        entry_price=100,
        current_price=100,
        quantity=0.001,
        initial_quantity=0.001,
        margin=25,
        leverage=1,
        stop_loss=99,
        tp1=101,
        tp2=103,
        best_price=100,
        exchange_open_order={"orderId": 10},
        exchange_stop_order={"orderId": 11, "clientOrderId": "hy_sl_old"},
        exchange_tp_order={"orderId": 12, "clientOrderId": "hy_tp_old"},
    )

    try:
        with pytest.raises(RuntimeError, match="PROTECTION_REPLACE_OLD_CANCEL_FAILED"):
            __import__("asyncio").run(live_executor.replace_protection_orders(p, "TEST_REPLACE"))
        freeze = db.get_kv("live_executor.trading_freeze", {})
    finally:
        db.set_kv("live_executor.trading_freeze", {"active": False})

    assert freeze["active"] is True
    assert freeze["reason"] == "PROTECTION_REPLACE_OLD_CANCEL_FAILED"
    assert freeze["position_id"] == "livepos_cancel_fail"


def test_manual_close_refuses_local_archive_for_real_live_position_when_disabled(monkeypatch):
    from backend.main import api_manual_close

    position_registry.open.clear()
    monkeypatch.setattr(settings, "trade_mode", "live")
    monkeypatch.setattr(settings, "live_trading_enabled", False)
    p = Position(
        position_id="livepos_manual_guard",
        strategy_id="strategy",
        source_signal_id="scan",
        symbol="BTCUSDT",
        side="LONG",
        status="OPEN",
        stage="Stage 1",
        score=80,
        entry_price=100,
        current_price=100,
        quantity=0.001,
        initial_quantity=0.001,
        margin=25,
        leverage=1,
        stop_loss=99,
        tp1=101,
        tp2=103,
        best_price=100,
        exchange_open_order={"orderId": 10, "clientOrderId": "hy_open_strategy"},
    )
    position_registry.add(p)

    def fail_local_close(*args, **kwargs):
        raise AssertionError("real live position must not be locally archived while exchange close is disabled")

    monkeypatch.setattr(position_manager, "close_position", fail_local_close)

    try:
        result = __import__("asyncio").run(api_manual_close(p.position_id))

        assert result["ok"] is False
        assert result["error"] == "real_live_position_requires_exchange_close"
        assert p.position_id in position_registry.open
    finally:
        position_registry.remove(p.position_id)


def test_position_registry_restores_open_positions(monkeypatch):
    from backend.positions import position_registry as registry_module

    payload = Position(
        position_id="pos_restore",
        strategy_id="strat_restore",
        source_signal_id="scan_restore",
        symbol="RESTOREUSDT",
        side="LONG",
        status="OPEN",
        stage="Stage 1",
        score=70,
        entry_price=100,
        current_price=100,
        quantity=1,
        initial_quantity=1,
        margin=50,
        leverage=2,
        stop_loss=99,
        tp1=101,
        tp2=103,
        best_price=100,
    ).asdict()

    class FakeDB:
        def list_positions(self):
            return [payload]

        def save_position(self, row):
            pass

        def delete_position(self, position_id):
            pass

        def list_closed(self, limit=10000):
            return []

    monkeypatch.setattr(registry_module, "db", FakeDB())
    restored = registry_module.PositionRegistry()
    assert restored.has_symbol("RESTOREUSDT")
    assert restored.list_open()[0].position_id == "pos_restore"
    assert restored.list_open()[0].notional == 100.0
    assert restored.list_open()[0].entry_fee > 0


def test_position_registry_close_archive_uses_atomic_db_transaction(monkeypatch):
    from backend.positions import position_registry as registry_module

    closed = ClosedPosition(
        position_id="pos_atomic_close",
        strategy_id="strat_atomic_close",
        symbol="ATOMUSDT",
        side="LONG",
        entry_price=100,
        exit_price=101,
        quantity=1,
        margin=100,
        pnl=1,
        roi=1,
        close_reason="TEST",
        score_at_entry=70,
        open_time=1,
        close_time=2,
        source_signal_id="scan",
    )
    calls = []

    class FakeDB:
        def list_positions(self):
            return []

        def archive_closed_position(self, row):
            calls.append(("archive_closed_position", row["position_id"]))

        def save_closed(self, row):
            raise AssertionError("close_archive must not save closed outside the atomic archive call")

        def delete_position(self, position_id):
            raise AssertionError("close_archive must not delete open outside the atomic archive call")

        def list_closed(self, limit=10000):
            return []

    monkeypatch.setattr(registry_module, "db", FakeDB())
    registry = registry_module.PositionRegistry()
    registry.open[closed.position_id] = Position(
        position_id=closed.position_id,
        strategy_id=closed.strategy_id,
        source_signal_id=closed.source_signal_id,
        symbol=closed.symbol,
        side=closed.side,
        status="OPEN",
        stage="Stage 1",
        score=70,
        entry_price=100,
        current_price=100,
        quantity=1,
        initial_quantity=1,
        margin=100,
        leverage=1,
        stop_loss=99,
        tp1=101,
        tp2=103,
        best_price=100,
    )

    registry.close_archive(closed)

    assert calls == [("archive_closed_position", "pos_atomic_close")]
    assert closed.position_id not in registry.open


def test_db_archive_closed_position_rolls_back_when_delete_fails(tmp_path):
    from backend.storage.db import DB

    database = DB(str(tmp_path / "positions.sqlite"))
    open_row = {
        "position_id": "pos_rollback",
        "status": "OPEN",
        "symbol": "ROLLUSDT",
        "open_time": 1,
    }
    closed_row = {
        "position_id": "pos_rollback",
        "status": "CLOSED",
        "symbol": "ROLLUSDT",
        "close_time": 2,
    }
    database.save_position(open_row)
    with database.conn() as c:
        c.execute(
            """
            CREATE TRIGGER fail_position_delete
            BEFORE DELETE ON positions
            BEGIN
                SELECT RAISE(ABORT, 'delete failed');
            END;
            """
        )

    with pytest.raises(sqlite3.IntegrityError):
        database.archive_closed_position(closed_row)

    assert database.list_positions()[0]["position_id"] == "pos_rollback"
    assert database.list_closed() == []


def test_performance_guard_excludes_reconciled_stale_positions():
    rows = [
        {"symbol": "AUSDT", "side": "LONG", "pnl": 1, "close_reason": "TP2"},
        {"symbol": "BUSDT", "side": "SHORT", "pnl": -10, "close_reason": "RESTORED_STALE_RECONCILE"},
    ]
    assert performance_guard.performance_rows(rows) == [rows[0]]

def test_position_summary_uses_configured_paper_equity(monkeypatch):
    monkeypatch.setattr(settings, "paper_account_equity_usdt", 201.0)
    monkeypatch.setattr(position_registry, "list_open", lambda: [])
    monkeypatch.setattr(position_registry, "list_closed", lambda: [])
    summary = position_manager.summary()
    assert summary["available_balance"] == 201.0

def test_paper_pnl_includes_slippage_and_fees(monkeypatch):
    symbol = "COSTTESTUSDT"
    cleanup_symbol(symbol)
    position_registry.open.clear()
    monkeypatch.setattr(settings, "require_codex_strategy_for_entry", False)
    monkeypatch.setattr(performance_guard, "_closed_rows", lambda: [])
    monkeypatch.setattr(settings, "paper_taker_fee_rate", 0.001)
    monkeypatch.setattr(settings, "paper_slippage_pct", 0.001)
    try:
        plan = auto_trading_risk_model.decide(
            high_quality_item(symbol=symbol, side="LONG", price=100),
            StrategyPlan(
                strategy_id="cost_plan",
                action="OPEN_LONG",
                symbol=symbol,
                side="LONG",
                entry_zone_low=100,
                entry_zone_high=100,
                ideal_entry_price=100,
                stop_loss=99,
                tp1=101.5,
                tp2=103,
                confidence=80,
                reason="cost test",
            ),
            {"equity": 1000, "available_balance": 1000, "loss_streak": 0, "open_positions": 0, "max_open_positions": 3, "trade_mode": "paper"},
            {"market_heat": 60, "volatility_regime": "normal"},
        )
        p = __import__("asyncio").run(paper_executor.open_position("scan_cost", "cost_plan", 80, plan))
        assert p.entry_price > 100
        assert p.entry_fee > 0
        gross_without_cost = (101 - p.entry_price) * p.quantity
        position_manager.update_position(p, 101)
        assert p.unrealized_pnl < gross_without_cost
    finally:
        cleanup_symbol(symbol)

def test_performance_guard_blocks_losing_symbol_side(monkeypatch):
    monkeypatch.setattr(settings, "require_codex_strategy_for_entry", False)
    symbol = "PERFTESTUSDT"
    cleanup_symbol(symbol)
    try:
        now = now_ms()
        for idx in range(3):
            position_registry.closed.insert(
                0,
                ClosedPosition(
                    position_id=f"closed_perf_{idx}",
                    strategy_id=f"perf_{idx}",
                    symbol=symbol,
                    side="LONG",
                    entry_price=100,
                    exit_price=99,
                    quantity=1,
                    margin=50,
                    pnl=-1,
                    roi=-2,
                    close_reason="SL",
                    score_at_entry=70,
                    open_time=now - 10_000,
                    close_time=now - idx * 1_000,
                    source_signal_id="scan_perf",
                ),
            )
        plan = StrategyPlan(
            strategy_id="perf_block",
            action="OPEN_LONG",
            symbol=symbol,
            side="LONG",
            entry_zone_low=100,
            entry_zone_high=100,
            ideal_entry_price=100,
            stop_loss=99,
            tp1=101.5,
            tp2=103,
            confidence=90,
            reason="blocked by history",
        )
        exec_plan = auto_trading_risk_model.decide(
            high_quality_item(symbol=symbol, side="LONG", price=100),
            plan,
            {"equity": 1000, "available_balance": 1000, "loss_streak": 0, "open_positions": 0, "max_open_positions": 3, "trade_mode": "paper"},
            {"market_heat": 60, "volatility_regime": "normal"},
        )
        assert exec_plan.decision == "OBSERVE"
        assert "performance guard rejected" in exec_plan.reason
    finally:
        cleanup_symbol(symbol)

def test_performance_guard_releases_recovery_after_recent_window_recovers():
    now = now_ms()
    recent_wins = [
        {"symbol": "RECOVERYUSDT", "side": "LONG", "pnl": 1.0, "close_time": now - idx * 1000, "close_reason": "TP"}
        for idx in range(50)
    ]
    old_losses = [
        {"symbol": "RECOVERYUSDT", "side": "LONG", "pnl": -2.0, "close_time": now - 100000 - idx * 1000, "close_reason": "SL"}
        for idx in range(60)
    ]

    assert performance_guard.recovery_mode(recent_wins + old_losses) is False

def test_performance_guard_releases_symbol_side_block_after_recent_side_recovers():
    now = now_ms()
    recent_wins = [
        {"symbol": "SIDEFIXUSDT", "side": "LONG", "pnl": 1.0, "close_time": now - idx * 1000, "close_reason": "TP"}
        for idx in range(3)
    ]
    old_losses = [
        {"symbol": "SIDEFIXUSDT", "side": "LONG", "pnl": -1.0, "close_time": now - 100000 - idx * 1000, "close_reason": "SL"}
        for idx in range(10)
    ]

    assert performance_guard._symbol_side_blocked(recent_wins + old_losses) is False

def test_strategy_filter_matches_directional_rules():
    strategy = {
        "filters": {
            "min_score": 70,
            "min_fund_confirm": 3,
            "allowed_fake_risks": ["LOW"],
            "min_direction_confirmations": 5,
            "min_volume_spike": 1.5,
            "max_wick_ratio": 0.65,
            "require_oi_positive": True,
            "require_timeframe_alignment": True,
            "require_taker_alignment": True,
            "require_depth_alignment": True,
            "require_sm_delta_alignment": True,
            "allowed_sides": ["LONG"],
        }
    }
    sample = sample_trade(side="LONG", pnl=1.0)
    assert strategy_matches(strategy, sample)
    sample["depth_imbalance"] = -0.2
    sample["radar"]["depth_imbalance"] = -0.2
    assert not strategy_matches(strategy, sample)

def test_autotrader_can_select_eligible_strategy_matching_candidate_side(monkeypatch):
    active_long = {
        "strategy_id": "active_long",
        "status": "ACTIVE",
        "filters": {
            **loose_test_strategy()["filters"],
            "allowed_sides": ["LONG"],
        },
        "metrics": {"eligible": True, "pnl": 1, "profit_factor": 1.2, "holdout": {"pnl": 1, "win_rate": 0.6}},
    }
    eligible_short = {
        "strategy_id": "eligible_short",
        "status": "PASS",
        "filters": {
            **loose_test_strategy()["filters"],
            "allowed_sides": ["SHORT"],
        },
        "metrics": {"eligible": True, "pnl": 1, "profit_factor": 1.2, "holdout": {"pnl": 1, "win_rate": 0.6}},
    }
    item = high_quality_item(symbol="SELECTSHORTUSDT", side="SHORT")
    monkeypatch.setattr(strategy_registry, "list", lambda limit=50: [active_long, eligible_short])

    selected = autotrader._best_matching_strategy(item, active_long)

    assert selected["strategy_id"] == "eligible_short"

def test_backtest_engine_uses_holdout_promotion_gate(monkeypatch):
    monkeypatch.setattr(settings, "evolve_train_split", 0.5)
    monkeypatch.setattr(settings, "evolve_min_backtest_trades", 6)
    monkeypatch.setattr(settings, "evolve_min_holdout_trades", 2)
    monkeypatch.setattr(settings, "evolve_min_win_rate", 0.55)
    monkeypatch.setattr(settings, "evolve_min_holdout_win_rate", 0.50)
    monkeypatch.setattr(settings, "evolve_min_profit_factor", 1.05)
    monkeypatch.setattr(settings, "replay_enabled", False)
    samples = [
        sample_trade(pnl=1.0, close_time=1),
        sample_trade(pnl=-0.2, close_time=2),
        sample_trade(pnl=0.8, close_time=3),
        sample_trade(pnl=0.5, close_time=4),
        sample_trade(pnl=1.0, close_time=5),
        sample_trade(pnl=-0.1, close_time=6),
        sample_trade(pnl=0.9, close_time=7),
        sample_trade(pnl=0.7, close_time=8),
    ]
    metrics = backtest_engine.evaluate(loose_test_strategy(), samples)
    assert metrics["eligible"] is True
    assert metrics["trades"] == 8
    assert metrics["holdout"]["trades"] == 4
    assert metrics["holdout"]["pnl"] > 0

def test_radar_score_auditor_reports_score_bands_and_factors(monkeypatch):
    samples = []
    for idx in range(8):
        sample = sample_trade(side="LONG", pnl=1.0 if idx < 6 else -0.4, close_time=idx + 1)
        sample["score"] = 82
        sample["radar"]["score"] = 82
        sample["fund_confirm_count"] = 3
        sample["radar"]["fund_confirm_count"] = 3
        samples.append(sample)
    for idx in range(8, 16):
        sample = sample_trade(side="LONG", pnl=-0.5 if idx < 13 else 0.3, close_time=idx + 1)
        sample["score"] = 45
        sample["radar"]["score"] = 45
        sample["fund_confirm_count"] = 1
        sample["radar"]["fund_confirm_count"] = 1
        samples.append(sample)

    monkeypatch.setattr(settings, "replay_enabled", True)
    monkeypatch.setattr(replay_memory, "samples", lambda limit=None: samples)
    monkeypatch.setattr(trade_memory, "samples", lambda limit=10000, require_radar=True: [])

    report = radar_score_auditor.report(limit=100)
    bands = {row["band"]: row for row in report["by_score_band"]}
    factors = {row["factor"]: row for row in report["factor_buckets"]}

    assert report["sample_count"] == 16
    assert bands["score_70_plus"]["win_rate"] > bands["score_40_50"]["win_rate"]
    assert "fund_confirm_3" in factors
    assert report["validation"]["verdict"] == "insufficient_samples"

def test_replay_memory_samples_return_newest_simulated_outcomes(monkeypatch):
    replay_memory._cache_until = 0
    replay_memory._cache_limit = 0
    replay_memory._sample_cache = []

    rows = []
    for idx in range(1, 9):
        row = high_quality_item(symbol="REPLAYUSDT", side="LONG", price=100 + idx).asdict()
        row.update({"ts_ms": idx, "score": 90, "scan_id": f"scan_{idx}"})
        rows.append(row)

    monkeypatch.setattr(settings, "replay_entry_stride", 1)
    monkeypatch.setattr(settings, "replay_horizon_steps", 1)
    monkeypatch.setattr(settings, "replay_min_score", 30.0)
    monkeypatch.setattr(replay_memory, "_load_snapshots", lambda sample_limit: rows)

    samples = replay_memory.samples(limit=3)

    assert [sample["close_time"] for sample in samples] == [8, 7, 6]

def test_radar_weight_calibrator_adjusts_validated_feature_weights(monkeypatch):
    samples = []
    good_features = {
        "trend_score": 90,
        "volume_score": 50,
        "volatility_score": 50,
        "oi_score": 50,
        "taker_score": 50,
        "timeframe_score": 55,
        "sm_score": 50,
        "heat_score": 50,
        "fake_penalty": 10,
    }
    bad_features = {
        "trend_score": 25,
        "volume_score": 50,
        "volatility_score": 50,
        "oi_score": 50,
        "taker_score": 50,
        "timeframe_score": 55,
        "sm_score": 50,
        "heat_score": 50,
        "fake_penalty": 85,
    }
    for idx in range(15):
        sample = sample_trade(side="LONG", pnl=1.0, close_time=idx + 1)
        sample["score_features"] = dict(good_features)
        sample["radar"]["score_features"] = dict(good_features)
        samples.append(sample)
    for idx in range(15, 30):
        sample = sample_trade(side="LONG", pnl=-0.8, close_time=idx + 1)
        sample["fake_breakout_risk"] = "HIGH"
        sample["radar"]["fake_breakout_risk"] = "HIGH"
        sample["score_features"] = dict(bad_features)
        sample["radar"]["score_features"] = dict(bad_features)
        samples.append(sample)

    monkeypatch.setattr(settings, "radar_weight_calibration_enabled", True)
    monkeypatch.setattr(settings, "radar_weight_use_replay", True)
    monkeypatch.setattr(settings, "radar_weight_use_closed_trades", False)
    monkeypatch.setattr(settings, "radar_weight_min_samples", 20)
    monkeypatch.setattr(settings, "radar_weight_bucket_min_samples", 8)
    monkeypatch.setattr(settings, "radar_weight_ttl_seconds", 1)
    monkeypatch.setattr(replay_memory, "samples", lambda limit=None: samples[: limit or len(samples)])
    monkeypatch.setattr(trade_memory, "samples", lambda limit=10000, require_radar=True: [])
    radar_weight_calibrator.clear_cache()

    report = radar_weight_calibrator.report(limit=100, force=True)

    assert report["active"] is True
    assert report["sample_count"] == 30
    assert report["effective_weights"]["trend_score"] > SCORE_WEIGHTS["trend_score"]
    assert report["effective_weights"]["fake_penalty"] < SCORE_WEIGHTS["fake_penalty"]
    assert {row["feature"] for row in report["adjustments"]} >= {"trend_score", "fake_penalty"}

def test_radar_weight_calibrator_samples_use_newest_outcomes(monkeypatch):
    samples = [sample_trade(side="LONG", pnl=1.0, close_time=idx) for idx in range(1, 31)]
    monkeypatch.setattr(settings, "replay_enabled", True)
    monkeypatch.setattr(settings, "radar_weight_use_replay", True)
    monkeypatch.setattr(settings, "radar_weight_use_closed_trades", False)
    monkeypatch.setattr(replay_memory, "samples", lambda limit=None: samples)
    monkeypatch.setattr(trade_memory, "samples", lambda limit=10000, require_radar=True: [])

    selected = radar_weight_calibrator._samples(20)

    assert [sample["close_time"] for sample in selected] == list(range(30, 10, -1))

def test_strategy_evolver_promotes_only_backtested_candidate(monkeypatch):
    samples = [
        sample_trade(pnl=1.0, close_time=1),
        sample_trade(pnl=-0.2, close_time=2),
        sample_trade(pnl=0.8, close_time=3),
        sample_trade(pnl=0.5, close_time=4),
        sample_trade(pnl=1.0, close_time=5),
        sample_trade(pnl=-0.1, close_time=6),
        sample_trade(pnl=0.9, close_time=7),
        sample_trade(pnl=0.7, close_time=8),
    ]
    stored = {}
    monkeypatch.setattr(settings, "evolve_train_split", 0.5)
    monkeypatch.setattr(settings, "evolve_min_backtest_trades", 6)
    monkeypatch.setattr(settings, "evolve_min_holdout_trades", 2)
    monkeypatch.setattr(settings, "evolve_min_win_rate", 0.55)
    monkeypatch.setattr(settings, "evolve_min_holdout_win_rate", 0.50)
    monkeypatch.setattr(settings, "evolve_min_profit_factor", 1.05)
    monkeypatch.setattr(settings, "replay_enabled", False)
    monkeypatch.setattr(trade_memory, "samples", lambda limit=10000, require_radar=True: samples)
    monkeypatch.setattr(trade_memory, "summary", lambda: {"closed_trades": 8, "joined_samples": 8, "weak_samples": 0, "win_rate": 0.75, "pnl": 4.6})
    monkeypatch.setattr(strategy_evolver, "_data_driven_candidates", lambda samples: [loose_test_strategy()])
    monkeypatch.setattr(strategy_registry, "save", lambda strategy: stored.setdefault(strategy["strategy_id"], strategy) or strategy)
    monkeypatch.setattr(strategy_registry, "save_run", lambda run: run)
    monkeypatch.setattr(strategy_registry, "activate", lambda strategy_id: {**stored[strategy_id], "status": "ACTIVE"})
    monkeypatch.setattr(strategy_registry, "active", lambda: None)
    monkeypatch.setattr(learning_data_audit, "summary", lambda force=False, limit=5000: {"production_grade": True, "reasons": []})
    out = strategy_evolver.evolve(use_codex=False, promote=True)
    assert out["run"]["promoted"]["status"] == "ACTIVE"
    assert out["run"]["best"]["metrics"]["eligible"] is True


def test_strategy_evolver_blocks_promotion_when_learning_data_not_production_grade(monkeypatch):
    samples = [sample_trade(pnl=1.0, close_time=idx) for idx in range(1, 13)]
    stored = {}
    monkeypatch.setattr(settings, "replay_enabled", False)
    monkeypatch.setattr(settings, "evolve_train_split", 0.7)
    monkeypatch.setattr(settings, "evolve_min_backtest_trades", 6)
    monkeypatch.setattr(settings, "evolve_min_holdout_trades", 2)
    monkeypatch.setattr(trade_memory, "samples", lambda limit=10000, require_radar=True: samples)
    monkeypatch.setattr(trade_memory, "summary", lambda: {"closed_trades": 12, "joined_samples": 12})
    monkeypatch.setattr(strategy_evolver, "_data_driven_candidates", lambda samples: [loose_test_strategy()])
    monkeypatch.setattr(
        backtest_engine,
        "evaluate",
        lambda candidate, samples: {
            "eligible": True,
            "eligible_reasons": [],
            "trades": len(samples),
            "wins": len(samples),
            "losses": 0,
            "win_rate": 1.0,
            "pnl": 12.0,
            "avg_pnl": 1.0,
            "profit_factor": 999.0,
            "max_drawdown": 0.0,
            "train": {"trades": 8},
            "holdout": {"trades": 4, "win_rate": 1.0, "pnl": 4.0, "profit_factor": 999.0},
        },
    )
    monkeypatch.setattr(learning_data_audit, "summary", lambda force=False, limit=5000: {"production_grade": False, "reasons": ["real_closed_samples_low"]})
    monkeypatch.setattr(strategy_registry, "save", lambda strategy: stored.setdefault(strategy["strategy_id"], strategy) or strategy)
    monkeypatch.setattr(strategy_registry, "save_run", lambda run: run)
    monkeypatch.setattr(strategy_registry, "activate", lambda strategy_id: (_ for _ in ()).throw(AssertionError("must not promote non-production-grade learning")))
    monkeypatch.setattr(strategy_registry, "active", lambda: None)

    out = strategy_evolver.evolve(use_codex=False, promote=True)

    assert out["run"]["promoted"] is None
    assert out["run"]["promotion_blocked"] == "learning_data_not_production_grade"


def test_strategy_evolver_combines_replay_and_closed_trade_samples(monkeypatch):
    replay_samples = [sample_trade(pnl=1.0, close_time=10), sample_trade(pnl=-0.2, close_time=11)]
    trade_samples = [sample_trade(pnl=0.8, close_time=20), sample_trade(pnl=0.7, close_time=21)]
    captured = {}

    def capture_candidates(samples):
        captured["samples"] = samples
        return [loose_test_strategy()]

    metrics = {
        "eligible": False,
        "eligible_reasons": ["sample_count_low"],
        "trades": 4,
        "wins": 3,
        "losses": 1,
        "win_rate": 0.75,
        "pnl": 2.3,
        "avg_pnl": 0.575,
        "profit_factor": 12.5,
        "max_drawdown": -0.2,
        "train": {},
        "holdout": {"trades": 2, "win_rate": 1.0, "pnl": 1.5, "profit_factor": 999.0},
    }
    monkeypatch.setattr(settings, "replay_enabled", True)
    monkeypatch.setattr(settings, "evolve_train_split", 0.5)
    monkeypatch.setattr(settings, "evolve_max_candidates", 1)
    monkeypatch.setattr(settings, "evolve_store_top_n", 1)
    monkeypatch.setattr(replay_memory, "samples", lambda limit=None: replay_samples)
    monkeypatch.setattr(replay_memory, "summary", lambda: {"replay_samples": 2})
    monkeypatch.setattr(trade_memory, "samples", lambda limit=10000, require_radar=True: trade_samples)
    monkeypatch.setattr(trade_memory, "summary", lambda: {"closed_trades": 2, "joined_samples": 2})
    monkeypatch.setattr(strategy_evolver, "_data_driven_candidates", capture_candidates)
    monkeypatch.setattr(backtest_engine, "evaluate", lambda candidate, samples: metrics)
    monkeypatch.setattr(strategy_registry, "save", lambda strategy: strategy)
    monkeypatch.setattr(strategy_registry, "save_run", lambda run: run)
    monkeypatch.setattr(strategy_registry, "active", lambda: None)

    out = strategy_evolver.evolve(use_codex=False, promote=False)

    assert [sample["close_time"] for sample in captured["samples"]] == [10, 11]
    assert out["run"]["sample_source"] == "replay+closed_trades"
    assert out["run"]["sample_count"] == 4
    assert out["run"]["train_sample_count"] == 2
    assert out["run"]["holdout_sample_count"] == 2


def test_strategy_evolver_generates_candidates_from_train_split_only(monkeypatch):
    samples = [sample_trade(pnl=1.0 if idx <= 6 else -1.0, close_time=idx) for idx in range(1, 11)]
    captured = {}

    def capture_candidates(candidate_samples):
        captured["close_times"] = [sample["close_time"] for sample in candidate_samples]
        return [loose_test_strategy()]

    monkeypatch.setattr(settings, "replay_enabled", False)
    monkeypatch.setattr(settings, "evolve_train_split", 0.6)
    monkeypatch.setattr(settings, "evolve_max_candidates", 1)
    monkeypatch.setattr(settings, "evolve_store_top_n", 1)
    monkeypatch.setattr(trade_memory, "samples", lambda limit=10000, require_radar=True: samples)
    monkeypatch.setattr(trade_memory, "summary", lambda: {"closed_trades": 10, "joined_samples": 10})
    monkeypatch.setattr(strategy_evolver, "_data_driven_candidates", capture_candidates)
    monkeypatch.setattr(
        backtest_engine,
        "evaluate",
        lambda candidate, evaluated_samples: {
            "eligible": True,
            "eligible_reasons": [],
            "trades": len(evaluated_samples),
            "wins": 6,
            "losses": 4,
            "win_rate": 0.6,
            "pnl": 2.0,
            "avg_pnl": 0.2,
            "profit_factor": 1.5,
            "max_drawdown": -1.0,
            "train": {"trades": 6},
            "holdout": {"trades": 4, "win_rate": 0.5, "pnl": 0.1, "profit_factor": 1.1},
        },
    )
    monkeypatch.setattr(strategy_registry, "save", lambda strategy: strategy)
    monkeypatch.setattr(strategy_registry, "save_run", lambda run: run)
    monkeypatch.setattr(strategy_registry, "active", lambda: None)

    out = strategy_evolver.evolve(use_codex=False, promote=False)

    assert captured["close_times"] == [1, 2, 3, 4, 5, 6]
    assert out["run"]["train_sample_count"] == 6
    assert out["run"]["holdout_sample_count"] == 4


def test_learning_evolve_requests_default_to_no_promotion():
    from backend.main import EvolveRequest, PaperRepairRequest

    assert EvolveRequest().promote is False
    assert PaperRepairRequest().promote is False


def test_frontend_learning_actions_do_not_force_promote_true():
    text = Path("backend/web/static/app.js").read_text(encoding="utf-8")

    assert "promote: true" not in text


def test_frontend_api_helper_attaches_api_token_header():
    text = Path("backend/web/static/app.js").read_text(encoding="utf-8")

    assert "X-API-Token" in text
    assert "localStorage.getItem('api_token')" in text or 'localStorage.getItem("api_token")' in text


def test_strategy_ai_template_fetch_attaches_api_token_header():
    text = Path("backend/web/templates/strategy_ai.html").read_text(encoding="utf-8")

    assert "X-API-Token" in text
    assert "localStorage.getItem('api_token')" in text or 'localStorage.getItem("api_token")' in text


def test_settings_page_has_api_token_local_storage_form():
    text = Path("backend/web/templates/settings.html").read_text(encoding="utf-8")

    assert 'id="apiTokenForm"' in text
    assert 'id="apiTokenInput"' in text
    assert "saveApiToken" in text
    assert "clearApiToken" in text


def test_frontend_api_token_manager_uses_local_storage():
    text = Path("backend/web/static/app.js").read_text(encoding="utf-8")

    assert "function saveApiToken" in text
    assert "function clearApiToken" in text
    assert "localStorage.setItem('api_token'" in text or 'localStorage.setItem("api_token"' in text
    assert "localStorage.removeItem('api_token')" in text or 'localStorage.removeItem("api_token")' in text


def test_monitor_action_buttons_have_busy_and_confirm_metadata():
    radar = Path("backend/web/templates/radar.html").read_text(encoding="utf-8")
    settings = Path("backend/web/templates/settings.html").read_text(encoding="utf-8")

    assert 'aria-live="polite"' in radar
    assert 'onclick="scanNow(event)"' in radar
    assert 'data-busy-label=' in radar
    assert 'onclick="scanNow(event)"' in settings
    assert 'onclick="runAutoOnce(event)"' in settings
    assert 'onclick="startAuto(event)"' in settings
    assert 'onclick="stopAuto(event)"' in settings
    assert 'data-confirm="start-auto-loop"' in settings
    assert 'data-confirm="stop-auto-loop"' in settings
    assert settings.count("data-busy-label=") >= 8


def test_frontend_button_manager_blocks_duplicate_actions():
    text = Path("backend/web/static/app.js").read_text(encoding="utf-8")

    assert "const buttonActionLocks = new Set()" in text
    assert "function resolveActionButton" in text
    assert "async function withButtonBusy" in text
    assert "buttonActionLocks.has" in text
    assert "buttonActionLocks.add" in text
    assert "buttonActionLocks.delete" in text
    assert "aria-busy" in text
    assert "data-busy-label" in text
    assert "dataset.confirm" in text
    assert "manual-close-position" in text


def test_frontend_button_css_has_operable_states():
    text = Path("backend/web/static/app.css").read_text(encoding="utf-8")

    assert ".btn:focus-visible" in text
    assert '.btn[aria-busy="true"]' in text
    assert ".btn::before" in text
    assert "@keyframes button-spin" in text
    assert "overflow-wrap: anywhere" in text


def test_env_example_declares_api_token():
    text = Path(".env.example").read_text(encoding="utf-8")

    assert "\nAPI_TOKEN=" in f"\n{text}"


def test_sensitive_config_post_requires_api_token_when_unconfigured(monkeypatch):
    from backend.main import app

    monkeypatch.setattr(settings, "api_token", "")

    def fail_update_env_values(*args, **kwargs):
        raise AssertionError("unauthenticated request must not write .env")

    monkeypatch.setattr("backend.main.update_env_values", fail_update_env_values)
    client = TestClient(app)

    response = client.post("/api/config/mainnet", json={"api_key": "key", "api_secret": "secret"})

    assert response.status_code == 503
    assert response.json()["detail"] == "api_token_not_configured"


def test_sensitive_config_post_accepts_valid_api_token(monkeypatch):
    from backend.main import app

    writes = []
    monkeypatch.setattr(settings, "api_token", "test-token")
    monkeypatch.setattr("backend.main.update_env_values", lambda path, values: writes.append(values))
    monkeypatch.setattr(binance_futures, "reload_from_settings", lambda: None)
    client = TestClient(app)

    response = client.post(
        "/api/config/mainnet",
        headers={"X-API-Token": "test-token"},
        json={"api_key": "key123456", "api_secret": "secret123456"},
    )

    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert writes


def test_strategy_alpha_api_exposes_status_and_run_cycle(monkeypatch):
    from backend.main import app

    calls = []

    def fake_status():
        return {
            "ok": True,
            "sample_source": "research_alpha",
            "pool_size": 2,
            "strategy_pool_score": 72.5,
            "prg": {"score": 72.5, "level": "MICRO_LIVE_CANDIDATE", "allowed": False},
        }

    def fake_run_cycle(*, generation_size=20, mutation_size=5):
        calls.append({"generation_size": generation_size, "mutation_size": mutation_size})
        return {
            "ok": True,
            "run": {"stored_count": 3, "strategy_pool_score": 72.5},
            "status": fake_status(),
        }

    monkeypatch.setattr(settings, "api_token", "test-token")
    monkeypatch.setattr("backend.main.strategy_alpha_status", fake_status, raising=False)
    monkeypatch.setattr("backend.main.run_strategy_alpha_cycle", fake_run_cycle, raising=False)
    client = TestClient(app)

    status = client.get("/api/strategy-alpha/status")
    started = client.post(
        "/api/strategy-alpha/run-cycle",
        headers={"X-API-Token": "test-token"},
        json={"generation_size": 2, "mutation_size": 1},
    )

    assert status.status_code == 200
    assert status.json()["pool_size"] == 2
    assert status.json()["prg"]["level"] == "MICRO_LIVE_CANDIDATE"
    assert started.status_code == 200
    assert started.json()["status"]["strategy_pool_score"] == 72.5
    assert calls == [{"generation_size": 2, "mutation_size": 1}]


def test_radar_scan_now_is_not_blocked_by_api_token_middleware(monkeypatch):
    from backend import main

    class FakeRadar:
        top50 = [object()]
        last_scan_time = "11:22:33"

        def scan_in_progress(self):
            return False

        def scan_status(self):
            return {"in_progress": False, "top50_count": 1}

    monkeypatch.setattr(settings, "api_token", "test-token")
    monkeypatch.setattr(main, "radar_engine", FakeRadar())
    monkeypatch.setattr(main, "_start_radar_scan_background", lambda **kwargs: True)
    client = TestClient(main.app)

    response = client.post("/api/radar/scan-now")

    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert response.json()["count"] == 1


def test_radar_scan_now_starts_background_scan_without_waiting(monkeypatch):
    from backend import main

    calls = []

    class FakeRadar:
        top50 = []
        last_scan_time = "--:--:--"

        def scan_in_progress(self):
            return False

        def scan_status(self):
            return {"in_progress": True, "top50_count": 0}

    async def fail_if_waited(**kwargs):
        raise AssertionError("scan-now must not wait for full radar scan")

    monkeypatch.setattr(main, "radar_engine", FakeRadar())
    monkeypatch.setattr(main, "_radar_scan_with_timeout", fail_if_waited)
    monkeypatch.setattr(main, "_start_radar_scan_background", lambda **kwargs: calls.append(kwargs) or True)

    result = __import__("asyncio").run(main.api_scan_now())

    assert result["ok"] is True
    assert result["started"] is True
    assert result["error"] == ""
    assert calls == [{"force_refresh": True}]


def test_radar_scan_now_does_not_queue_or_cancel_when_scan_is_running(monkeypatch):
    from backend import main

    class RunningRadar:
        top50 = [object()]
        last_scan_time = "11:22:33"
        scan_called = False

        def scan_in_progress(self):
            return True

        async def scan(self, force_refresh=False):
            self.scan_called = True
            return []

        def scan_status(self):
            return {"in_progress": True, "running_seconds": 12.3, "last_error": ""}

    fake = RunningRadar()
    monkeypatch.setattr(main, "radar_engine", fake)

    result = __import__("asyncio").run(main.api_scan_now())

    assert result["ok"] is True
    assert result["started"] is False
    assert result["error"] == "radar_scan_already_running"
    assert fake.scan_called is False


def test_api_radar_starts_background_scan_without_blocking_when_cache_empty(monkeypatch):
    from backend import main

    class EmptyRadar:
        top50 = []
        top4 = []
        last_scan_id = ""
        last_scan_time = "--:--:--"
        scan_called = False

        def scan_in_progress(self):
            return False

        async def scan(self, force_refresh=False):
            self.scan_called = True
            return []

        def scan_status(self):
            return {"in_progress": False, "top50_count": 0}

    fake = EmptyRadar()
    monkeypatch.setattr(main, "radar_engine", fake)

    async def call_api():
        result = await main.api_radar()
        await __import__("asyncio").sleep(0)
        return result

    result = __import__("asyncio").run(call_api())

    assert result["ok"] is False
    assert result["error"] == "radar_scan_warming_up"
    assert result["top50"] == []
    assert fake.scan_called is True


def test_api_radar_returns_compact_stream_diagnostics(monkeypatch):
    from backend import main

    item = high_quality_item(symbol="COMPACTUSDT", side="LONG", price=100)

    class CachedRadar:
        top50 = [item]
        top4 = []
        last_scan_id = "scan_compact"

        def scan_in_progress(self):
            return False

        def scan_status(self):
            return {
                "in_progress": False,
                "top50_count": 1,
                "active_coins": {
                    "active_count": 120,
                    "active_symbols": ["AUSDT", "BUSDT"],
                    "active": [{"symbol": "AUSDT", "payload": "large"}],
                    "recent_removed": [{"symbol": "OLDUSDT", "reason": "idle"}],
                },
                "dynamic_stream": {
                    "active_count": 120,
                    "running": True,
                    "streams": [{"symbol": "AUSDT", "payload": "large"}],
                    "last_error": "",
                },
            }

    monkeypatch.setattr(main, "radar_engine", CachedRadar())

    result = __import__("asyncio").run(main.api_radar())

    assert result["top50"][0]["symbol"] == "COMPACTUSDT"
    assert result["active_coins"] == {
        "active_count": 120,
        "active_symbols": ["AUSDT", "BUSDT"],
        "recent_removed": [{"symbol": "OLDUSDT", "reason": "idle"}],
    }
    assert result["dynamic_stream"] == {"active_count": 120, "running": True, "last_error": ""}
    assert "active" not in result["scan_status"]["active_coins"]
    assert "streams" not in result["scan_status"]["dynamic_stream"]


def test_radar_scan_status_compact_does_not_build_full_stream_diagnostics(monkeypatch):
    from backend.radar import radar_engine as radar_module

    engine = RadarEngine()
    active_coin_registry.update_candidates(["FAST1USDT", "FAST2USDT"], now=100.0)
    dynamic_symbol_stream.sync(["FAST1USDT", "FAST2USDT"], now=100.0)
    monkeypatch.setattr(
        radar_module.active_coin_registry,
        "diagnostics",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("full active diagnostics called")),
    )
    monkeypatch.setattr(
        radar_module.dynamic_symbol_stream,
        "diagnostics",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("full stream diagnostics called")),
    )

    status = engine.scan_status(compact=True)

    assert status["active_coins"]["active_count"] == 2
    assert status["active_coins"]["active_symbols"] == ["FAST1USDT", "FAST2USDT"]
    assert "active" not in status["active_coins"]
    assert status["dynamic_stream"]["active_count"] == 2
    assert "subscriptions" not in status["dynamic_stream"]


def test_radar_scan_yields_while_processing_candidates(monkeypatch):
    from backend.radar import radar_engine as radar_module

    monkeypatch.setattr(settings, "radar_exclude_major_symbols_from_anomaly", False)
    monkeypatch.setattr(settings, "radar_require_short_term_anomaly", False)
    monkeypatch.setattr(radar_weight_calibrator, "report", lambda: {})
    monkeypatch.setattr("backend.radar.radar_engine.db.save_radar_items", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        radar_module.universal_anomaly_model,
        "predict",
        lambda item: {"direction": item.direction, "probabilities": {"LONG": 0.6, "SHORT": 0.2, "NEUTRAL": 0.2}},
    )
    yielded = []

    async def fake_snapshots(force_refresh=False):
        return [
            MarketSnapshot("YIELD1USDT", 1.0, 1.0, 1.1, 1.2, 2.0, 0.1, 0, 0.60, 0.40, 0.10, 1.0, 0.2),
            MarketSnapshot("YIELD2USDT", 1.0, 1.1, 1.2, 1.3, 2.1, 0.1, 0, 0.61, 0.39, 0.11, 1.0, 0.2),
        ]

    async def observer():
        await asyncio.sleep(0)
        yielded.append(True)

    def classify(item):
        assert yielded, "scan did not yield before classification"
        return {"action": "WAIT", "no_trade_reasons": []}

    monkeypatch.setattr(radar_module.market_service, "get_snapshots", fake_snapshots)
    monkeypatch.setattr(radar_module.market_classifier, "classify", classify)
    engine = RadarEngine()

    async def run_scan():
        observer_task = asyncio.create_task(observer())
        await engine.scan()
        await observer_task

    asyncio.run(run_scan())


def test_startup_starts_universal_anomaly_auto_train_thread(monkeypatch):
    from backend import main

    started = []

    class FakeThread:
        def __init__(self, *, target, name, daemon):
            self.target = target
            self.name = name
            self.daemon = daemon

        def is_alive(self):
            return False

        def start(self):
            started.append(self)

    monkeypatch.setattr(settings, "universal_anomaly_auto_train_enabled", True)
    monkeypatch.setattr(main, "_universal_anomaly_auto_train_thread", None, raising=False)
    monkeypatch.setattr(main.threading, "Thread", FakeThread)

    thread = main.start_universal_anomaly_auto_train_thread()

    assert thread is started[0]
    assert thread.target is main.run_auto_train_loop
    assert thread.name == "universal-anomaly-auto-train"
    assert thread.daemon is True


def test_position_policy_client_has_no_hardcoded_api_key_shape():
    leaked = "sk-" + "a1be81e0bb4f40ec888c1d12ca6c38fc"
    text = Path("backend/ai_strategy/position_policy_client.py").read_text(encoding="utf-8")

    assert leaked not in text


def test_binance_signed_url_redaction_removes_signature_and_secret_values():
    from backend.exchange.binance_futures import redact_sensitive_url

    raw = "https://fapi.binance.com/fapi/v1/order?symbol=BTCUSDT&timestamp=1&signature=abcdef&apiSecret=secret"

    redacted = redact_sensitive_url(raw)

    assert "abcdef" not in redacted
    assert "secret" not in redacted
    assert "signature=<redacted>" in redacted
    assert "apiSecret=<redacted>" in redacted


def test_http_client_request_logging_is_quiet_during_market_scan():
    import logging

    assert logging.getLogger("httpx").getEffectiveLevel() >= logging.WARNING
    assert logging.getLogger("httpcore").getEffectiveLevel() >= logging.WARNING


def test_binance_log_redaction_preserves_logging_argument_types():
    from backend.exchange.binance_futures import RedactingLogFilter

    record = logging.LogRecord(
        "httpx",
        logging.INFO,
        "test.py",
        1,
        'HTTP Request: %s %s "%s %d %s"',
        (
            "GET",
            "https://fapi.binance.com/fapi/v1/openOrders?timestamp=1&signature=abcdef",
            "HTTP/1.1",
            200,
            "OK",
        ),
        None,
    )

    assert RedactingLogFilter().filter(record) is True
    assert record.args[3] == 200
    message = record.getMessage()
    assert "signature=<redacted>" in message
    assert "abcdef" not in message


def test_position_manager_marks_defensive_on_minor_reverse_signal(monkeypatch):
    position_registry.open.clear()
    symbol = "REVTESTUSDT"
    cleanup_symbol(symbol)
    try:
        p = Position(
            position_id="pos_reverse_test",
            strategy_id="strat_reverse_test",
            source_signal_id="scan_reverse_test",
            symbol=symbol,
            side="LONG",
            status="OPEN",
            stage="Stage 1",
            score=72,
            entry_price=100,
            current_price=100,
            quantity=1,
            initial_quantity=1,
            margin=100,
            leverage=1,
            stop_loss=95,
            tp1=105,
            tp2=110,
            best_price=100,
        )
        position_registry.add(p)
        market_service.last_snapshots[symbol] = MarketSnapshot(symbol, 98, -1, -2, -3, 2, 1, 0, 0.2, 0.8, -0.2, 1, 0.2)
        radar_engine.top50 = [high_quality_item(symbol=symbol, side="SHORT", price=98)]
        patch_position_quote(monkeypatch, 98)
        __import__("asyncio").run(position_manager.manage_all())
        assert position_registry.has_symbol(symbol)
        held = position_registry.list_open()[0]
        assert held.lifecycle_state == "DEFENSIVE"
        assert held.lock_status == "DEFENSIVE_REVERSE_SIGNAL"
        assert not [row for row in position_registry.list_closed() if row["symbol"] == symbol]
    finally:
        cleanup_symbol(symbol)

def test_position_manager_holds_noise_inside_live_thesis(monkeypatch):
    position_registry.open.clear()
    symbol = "NOISEHOLDUSDT"
    cleanup_symbol(symbol)
    try:
        p = Position(
            position_id="pos_noise_hold",
            strategy_id="strat_noise_hold",
            source_signal_id="scan_noise_hold",
            symbol=symbol,
            side="LONG",
            status="OPEN",
            stage="Stage 1",
            score=72,
            entry_price=100,
            current_price=100,
            quantity=1,
            initial_quantity=1,
            margin=100,
            leverage=1,
            stop_loss=95,
            tp1=105,
            tp2=110,
            best_price=100,
            risk_usdt=5,
        )
        position_registry.add(p)
        signal = high_quality_item(symbol=symbol, side="LONG", price=96.8)
        signal.fake_breakout_risk = "MEDIUM"
        signal.score = 68
        radar_engine.top50 = [signal]

        patch_position_quote(monkeypatch, 96.8)

        __import__("asyncio").run(position_manager.manage_all())

        assert position_registry.has_symbol(symbol)
        held = position_registry.list_open()[0]
        assert held.thesis_alive is True
        assert held.defense_level == "DEFENSIVE"
        assert held.last_decision["action"] == "PROTECT"
        assert held.noise_budget_r > 0
        assert held.adverse_r > held.noise_budget_r
        assert held.decision_log
        assert not [row for row in position_registry.list_closed() if row["symbol"] == symbol]
    finally:
        cleanup_symbol(symbol)


def test_position_manager_uses_strategy_contract_time_stop(monkeypatch):
    position_registry.open.clear()
    symbol = "CONTRACTTIMEUSDT"
    cleanup_symbol(symbol)
    monkeypatch.setattr(settings, "position_max_hold_seconds", 21600)
    try:
        p = Position(
            position_id="pos_contract_time_stop",
            strategy_id="strat_contract_time_stop",
            source_signal_id="scan_contract_time_stop",
            symbol=symbol,
            side="LONG",
            status="OPEN",
            stage="Stage 1",
            score=72,
            entry_price=100,
            current_price=100,
            quantity=1,
            initial_quantity=1,
            margin=100,
            leverage=1,
            stop_loss=90,
            tp1=105,
            tp2=110,
            best_price=100,
            risk_usdt=10,
            open_time=now_ms() - 181_000,
            strategy_contract={
                "time_stop": {
                    "seconds": 180,
                    "rule": "Exit if the paper probe does not develop before the time stop.",
                }
            },
        )
        position_registry.add(p)
        radar_engine.top50 = [high_quality_item(symbol=symbol, side="LONG", price=99)]
        patch_position_quote(monkeypatch, 99)

        __import__("asyncio").run(position_manager.manage_all())

        assert not position_registry.has_symbol(symbol)
        closed = [row for row in position_registry.list_closed() if row["position_id"] == "pos_contract_time_stop"]
        assert closed[0]["close_reason"] == "MAX_HOLD_TIMEOUT"
    finally:
        cleanup_symbol(symbol)


def test_position_manager_blocks_snapshot_cache_for_position_valuation(monkeypatch):
    position_registry.open.clear()
    symbol = "SNAPPRICEUSDT"
    cleanup_symbol(symbol)
    try:
        p = Position(
            position_id="pos_snapshot_price",
            strategy_id="strat_snapshot_price",
            source_signal_id="scan_snapshot_price",
            symbol=symbol,
            side="SHORT",
            status="OPEN",
            stage="Stage 1",
            score=72,
            entry_price=1913.14295,
            current_price=1903,
            quantity=0.026122,
            initial_quantity=0.026122,
            margin=24.9876,
            leverage=2,
            stop_loss=1909.7,
            tp1=1889.5,
            tp2=1872.0,
            best_price=1903,
            risk_usdt=0.5106,
        )
        position_registry.add(p)
        radar_engine.top50 = [high_quality_item(symbol=symbol, side="SHORT", price=1903)]

        async def fake_quote(symbol_arg, side_arg=None):
            return PriceQuote(
                symbol=symbol_arg,
                price=1903.0,
                source="snapshot:rest_ticker:degraded",
                ts_ms=now_ms(),
                age_seconds=0.1,
                stale=False,
            )

        monkeypatch.setattr(market_service, "price_quote", fake_quote)

        __import__("asyncio").run(position_manager.manage_all())

        assert position_registry.has_symbol(symbol)
        held = position_registry.list_open()[0]
        assert held.current_price == 1903
        assert held.price_source == "snapshot:rest_ticker:degraded"
        assert held.price_stale is True
        assert held.last_decision["action"] == "NOOP"
        assert held.last_decision["reason"] == "PRICE_SOURCE_STALE"
        assert "price_source_not_safe_for_position_valuation" in held.last_decision["evidence"]
    finally:
        cleanup_symbol(symbol)

def test_ai_position_review_can_hold_noise_when_safety_allows(monkeypatch):
    position_registry.open.clear()
    symbol = "AIHOLDUSDT"
    cleanup_symbol(symbol)
    try:
        monkeypatch.setattr(settings, "ai_enabled", True)
        monkeypatch.setattr(settings, "ai_strategy_provider", "codex_cli")
        monkeypatch.setattr(settings, "ai_position_review_enabled", True)
        monkeypatch.setattr(settings, "ai_position_review_min_interval_seconds", 1)
        monkeypatch.setattr("backend.positions.position_manager.shutil_which_codex", lambda: True)
        p = Position(
            position_id="pos_ai_hold",
            strategy_id="strat_ai_hold",
            source_signal_id="scan_ai_hold",
            symbol=symbol,
            side="LONG",
            status="OPEN",
            stage="Stage 1",
            score=72,
            entry_price=100,
            current_price=100,
            quantity=1,
            initial_quantity=1,
            margin=100,
            leverage=1,
            stop_loss=95,
            tp1=105,
            tp2=110,
            best_price=100,
            risk_usdt=5,
        )
        position_registry.add(p)
        signal = high_quality_item(symbol=symbol, side="LONG", price=99.8)
        signal.fake_breakout_risk = "MEDIUM"
        radar_engine.top50 = [signal]

        async def fake_review(position, signal_arg, rule_decision):
            return PositionPolicyReview(
                ts_ms=now_ms(),
                action="HOLD",
                thesis_alive=True,
                confidence=0.82,
                reason="normal noise inside budget",
                noise_interpretation="normal_noise",
                invalidation="wait for structure break",
                reduce_ratio=0.0,
                stop_loss=0.0,
                learning_note="do not exit small pullback",
                safety_note="paper only",
                provider="test",
            )

        patch_position_quote(monkeypatch, 99.8)
        monkeypatch.setattr("backend.positions.position_manager.ai_position_policy_client.review", fake_review)

        __import__("asyncio").run(position_manager.manage_all())

        held = position_registry.list_open()[0]
        assert held.last_decision["action"] == "HOLD"
        assert held.last_decision["reason"] == "AI_HOLD_NORMAL_NOISE"
        assert held.last_ai_review["applied_action"] == "HOLD"
        assert held.last_ai_review["safety_override"] == ""
    finally:
        cleanup_symbol(symbol)

def test_ai_position_review_exit_inside_noise_is_blocked(monkeypatch):
    position_registry.open.clear()
    symbol = "AIEXITBLOCKUSDT"
    cleanup_symbol(symbol)
    try:
        monkeypatch.setattr(settings, "ai_enabled", True)
        monkeypatch.setattr(settings, "ai_strategy_provider", "codex_cli")
        monkeypatch.setattr(settings, "ai_position_review_enabled", True)
        monkeypatch.setattr(settings, "ai_position_review_min_interval_seconds", 1)
        monkeypatch.setattr("backend.positions.position_manager.shutil_which_codex", lambda: True)
        p = Position(
            position_id="pos_ai_exit_block",
            strategy_id="strat_ai_exit_block",
            source_signal_id="scan_ai_exit_block",
            symbol=symbol,
            side="LONG",
            status="OPEN",
            stage="Stage 1",
            score=72,
            entry_price=100,
            current_price=100,
            quantity=1,
            initial_quantity=1,
            margin=100,
            leverage=1,
            stop_loss=95,
            tp1=105,
            tp2=110,
            best_price=100,
            risk_usdt=5,
        )
        position_registry.add(p)
        signal = high_quality_item(symbol=symbol, side="LONG", price=99.8)
        signal.fake_breakout_risk = "MEDIUM"
        radar_engine.top50 = [signal]

        async def fake_review(position, signal_arg, rule_decision):
            return PositionPolicyReview(
                ts_ms=now_ms(),
                action="EXIT",
                thesis_alive=False,
                confidence=0.95,
                reason="exit requested by ai",
                noise_interpretation="thesis_invalidated",
                invalidation="ai says invalid",
                reduce_ratio=0.0,
                stop_loss=0.0,
                learning_note="blocked by safety",
                safety_note="test",
                provider="test",
            )

        patch_position_quote(monkeypatch, 99.8)
        monkeypatch.setattr("backend.positions.position_manager.ai_position_policy_client.review", fake_review)

        __import__("asyncio").run(position_manager.manage_all())

        assert position_registry.has_symbol(symbol)
        held = position_registry.list_open()[0]
        assert held.last_decision["action"] == "PROTECT"
        assert held.last_ai_review["applied_action"] == "PROTECT"
        assert held.last_ai_review["safety_override"] == "ai_exit_blocked_inside_safety_kernel"
    finally:
        cleanup_symbol(symbol)

def test_position_manager_reduces_profitable_defensive_position(monkeypatch):
    position_registry.open.clear()
    symbol = "DEFREDUCEUSDT"
    cleanup_symbol(symbol)
    try:
        p = Position(
            position_id="pos_def_reduce",
            strategy_id="strat_def_reduce",
            source_signal_id="scan_def_reduce",
            symbol=symbol,
            side="LONG",
            status="OPEN",
            stage="Stage 1",
            score=72,
            entry_price=100,
            current_price=100,
            quantity=1,
            initial_quantity=1,
            margin=100,
            leverage=1,
            stop_loss=95,
            tp1=105,
            tp2=110,
            best_price=100,
            risk_usdt=5,
        )
        position_registry.add(p)
        reverse = high_quality_item(symbol=symbol, side="SHORT", price=103)
        reverse.score = 72
        reverse.fund_confirm_count = 3
        radar_engine.top50 = [reverse]

        patch_position_quote(monkeypatch, 103)

        __import__("asyncio").run(position_manager.manage_all())

        assert position_registry.has_symbol(symbol)
        held = position_registry.list_open()[0]
        assert held.quantity == 0.75
        assert held.realized_pnl > 0
        assert held.last_decision["action"] == "REDUCE"
        assert held.last_decision["reason"] == "DEFENSIVE_PARTIAL_REDUCE"
        assert held.lock_status == "DEFENSIVE_PARTIAL_REDUCE"
    finally:
        cleanup_symbol(symbol)


def test_position_manager_uses_exchange_fill_for_live_managed_close(monkeypatch):
    position_registry.open.clear()
    symbol = "LIVECLOSEFILLUSDT"
    cleanup_symbol(symbol)
    monkeypatch.setattr(settings, "trade_mode", "live")
    monkeypatch.setattr(settings, "live_trading_enabled", True)
    p = Position(
        position_id="livepos_fill_close",
        strategy_id="strategy_fill_close",
        source_signal_id="scan_fill_close",
        symbol=symbol,
        side="LONG",
        status="OPEN",
        stage="Stage 1",
        score=80,
        entry_price=100,
        current_price=101,
        quantity=1,
        initial_quantity=1,
        margin=100,
        leverage=1,
        stop_loss=95,
        tp1=105,
        tp2=110,
        best_price=101,
        exchange_open_order={"orderId": 10},
    )
    position_registry.add(p)

    async def fake_close_position(position):
        return {"orderId": 20, "executedQty": "1", "avgPrice": "103", "cumQuote": "103"}

    monkeypatch.setattr("backend.trading.live_executor.live_executor.close_position", fake_close_position)

    try:
        closed = __import__("asyncio").run(position_manager.managed_close(p, "TEST_CLOSE", exit_price=101))
    finally:
        cleanup_symbol(symbol)

    assert closed.exit_price == 103
    assert closed.exchange_close_order["orderId"] == 20
    assert closed.pnl > 2.0


def test_position_manager_freezes_when_live_managed_close_fails(monkeypatch):
    position_registry.open.clear()
    db.set_kv("live_executor.trading_freeze", {"active": False})
    symbol = "LIVECLOSEFAILUSDT"
    cleanup_symbol(symbol)
    monkeypatch.setattr(settings, "trade_mode", "live")
    monkeypatch.setattr(settings, "live_trading_enabled", True)
    p = Position(
        position_id="livepos_close_fail",
        strategy_id="strategy_close_fail",
        source_signal_id="scan_close_fail",
        symbol=symbol,
        side="LONG",
        status="OPEN",
        stage="Stage 1",
        score=80,
        entry_price=100,
        current_price=101,
        quantity=1,
        initial_quantity=1,
        margin=100,
        leverage=1,
        stop_loss=95,
        tp1=105,
        tp2=110,
        best_price=101,
        exchange_open_order={"orderId": 10},
    )
    position_registry.add(p)

    async def fake_close_position(position):
        raise RuntimeError("binance_close_failed")

    monkeypatch.setattr("backend.trading.live_executor.live_executor.close_position", fake_close_position)

    try:
        with pytest.raises(RuntimeError, match="binance_close_failed"):
            __import__("asyncio").run(position_manager.managed_close(p, "TEST_CLOSE", exit_price=101))
        freeze = db.get_kv("live_executor.trading_freeze", {})
        held = position_registry.list_open()[0]
    finally:
        cleanup_symbol(symbol)
        db.set_kv("live_executor.trading_freeze", {"active": False})

    assert freeze["active"] is True
    assert freeze["reason"] == "LIVE_CLOSE_FAILED_MANUAL_REVIEW"
    assert held.lock_status == "LIVE_CLOSE_FAILED_MANUAL_REVIEW"
    assert held.lifecycle_state == "LIVE_MANAGEMENT_FAILED"
    assert held.exchange_close_order["live_management_failed"] is True


def test_position_manager_freezes_when_live_partial_reduce_fails(monkeypatch):
    position_registry.open.clear()
    db.set_kv("live_executor.trading_freeze", {"active": False})
    symbol = "LIVEREDUCEFAILUSDT"
    cleanup_symbol(symbol)
    monkeypatch.setattr(settings, "trade_mode", "live")
    monkeypatch.setattr(settings, "live_trading_enabled", True)
    p = Position(
        position_id="livepos_reduce_fail",
        strategy_id="strategy_reduce_fail",
        source_signal_id="scan_reduce_fail",
        symbol=symbol,
        side="LONG",
        status="OPEN",
        stage="Stage 1",
        score=80,
        entry_price=100,
        current_price=102,
        quantity=2,
        initial_quantity=2,
        margin=200,
        leverage=1,
        stop_loss=95,
        tp1=102,
        tp2=108,
        best_price=102,
        exchange_open_order={"orderId": 10},
    )
    position_registry.add(p)

    async def fake_reduce_position(position, quantity, reason="reduce"):
        raise RuntimeError("binance_reduce_failed")

    monkeypatch.setattr("backend.trading.live_executor.live_executor.reduce_position", fake_reduce_position)

    try:
        with pytest.raises(RuntimeError, match="binance_reduce_failed"):
            __import__("asyncio").run(position_manager.partial_reduce(p, 0.5, 102, "TP1_PARTIAL"))
        freeze = db.get_kv("live_executor.trading_freeze", {})
        held = position_registry.list_open()[0]
    finally:
        cleanup_symbol(symbol)
        db.set_kv("live_executor.trading_freeze", {"active": False})

    assert freeze["active"] is True
    assert freeze["reason"] == "LIVE_REDUCE_FAILED_MANUAL_REVIEW"
    assert held.lock_status == "LIVE_REDUCE_FAILED_MANUAL_REVIEW"
    assert held.lifecycle_state == "LIVE_MANAGEMENT_FAILED"
    assert held.quantity == 2


def test_position_manager_does_not_locally_close_real_live_position_when_live_switch_is_off(monkeypatch):
    position_registry.open.clear()
    symbol = "LIVEOFFSWITCHUSDT"
    cleanup_symbol(symbol)
    monkeypatch.setattr(settings, "trade_mode", "live")
    monkeypatch.setattr(settings, "live_trading_enabled", False)
    p = Position(
        position_id="livepos_off_switch",
        strategy_id="strategy_off_switch",
        source_signal_id="scan_off_switch",
        symbol=symbol,
        side="LONG",
        status="OPEN",
        stage="Stage 1",
        score=80,
        entry_price=100,
        current_price=101,
        quantity=1,
        initial_quantity=1,
        margin=100,
        leverage=1,
        stop_loss=95,
        tp1=105,
        tp2=110,
        best_price=101,
        exchange_open_order={"orderId": 10, "clientOrderId": "hy_open_strategy_off_switch"},
    )
    position_registry.add(p)

    try:
        with pytest.raises(RuntimeError, match="LIVE_TRADING_DISABLED"):
            __import__("asyncio").run(position_manager.managed_close(p, "TEST_CLOSE", exit_price=101))
        assert position_registry.has_symbol(symbol)
        assert not [row for row in position_registry.list_closed() if row["position_id"] == "livepos_off_switch"]
    finally:
        cleanup_symbol(symbol)


def test_position_manager_stage1_soft_lock_syncs_live_protection(monkeypatch):
    position_registry.open.clear()
    symbol = "LIVESOFTLOCKUSDT"
    cleanup_symbol(symbol)
    monkeypatch.setattr(settings, "trade_mode", "live")
    monkeypatch.setattr(settings, "live_trading_enabled", True)
    p = Position(
        position_id="livepos_soft_lock",
        strategy_id="strategy_soft_lock",
        source_signal_id="scan_soft_lock",
        symbol=symbol,
        side="LONG",
        status="OPEN",
        stage="Stage 1",
        score=80,
        entry_price=100,
        current_price=100,
        quantity=1,
        initial_quantity=1,
        margin=100,
        leverage=1,
        stop_loss=95,
        tp1=110,
        tp2=115,
        best_price=100,
        exchange_open_order={"orderId": 10},
        exchange_stop_order={"orderId": 11, "clientOrderId": "hy_sl_strategy_soft_lock"},
        exchange_tp_order={"orderId": 12, "clientOrderId": "hy_tp_strategy_soft_lock"},
    )
    position_registry.add(p)
    radar_engine.top50 = [high_quality_item(symbol=symbol, side="LONG", price=104)]
    patch_position_quote(monkeypatch, 104)
    calls = []

    async def fake_replace_protection_orders(position, reason):
        calls.append({"reason": reason, "stop_loss": position.stop_loss})
        return {"stop_order": {"orderId": 31}, "tp_order": {"orderId": 32}}

    monkeypatch.setattr("backend.trading.live_executor.live_executor.replace_protection_orders", fake_replace_protection_orders)

    try:
        __import__("asyncio").run(position_manager.manage_all())
    finally:
        cleanup_symbol(symbol)

    assert calls == [{"reason": "NET_BREAKEVEN_LOCK", "stop_loss": p.stop_loss}]
    assert p.lock_status == "NET_BREAKEVEN_LOCK"


def test_position_manager_rejects_testnet_fallback_price_for_real_live_position(monkeypatch):
    position_registry.open.clear()
    symbol = "LIVEFALLBACKPRICEUSDT"
    cleanup_symbol(symbol)
    monkeypatch.setattr(settings, "trade_mode", "live")
    monkeypatch.setattr(settings, "live_trading_enabled", True)
    monkeypatch.setattr(settings, "binance_testnet", False)
    binance_rest.last_public_source = "testnet_fallback"
    p = Position(
        position_id="livepos_fallback_price",
        strategy_id="strategy_fallback_price",
        source_signal_id="scan_fallback_price",
        symbol=symbol,
        side="LONG",
        status="OPEN",
        stage="Stage 1",
        score=80,
        entry_price=100,
        current_price=100,
        quantity=1,
        initial_quantity=1,
        margin=100,
        leverage=1,
        stop_loss=95,
        tp1=105,
        tp2=110,
        best_price=100,
        exchange_open_order={"orderId": 10},
    )
    quote = PriceQuote(symbol=symbol, price=104, source="book_ticker_bid_close_long", ts_ms=now_ms(), age_seconds=0, stale=False)

    try:
        assert position_manager._quote_not_safe_for_position(quote, p) is True
    finally:
        binance_rest.last_public_source = "mainnet"
        cleanup_symbol(symbol)


def test_position_manager_partial_tp1_syncs_exchange_protection_after_local_stop_move(monkeypatch):
    position_registry.open.clear()
    symbol = "LIVEPARTIALSYNCUSDT"
    cleanup_symbol(symbol)
    monkeypatch.setattr(settings, "trade_mode", "live")
    monkeypatch.setattr(settings, "live_trading_enabled", True)
    p = Position(
        position_id="livepos_partial_sync",
        strategy_id="strategy_partial_sync",
        source_signal_id="scan_partial_sync",
        symbol=symbol,
        side="LONG",
        status="OPEN",
        stage="Stage 1",
        score=80,
        entry_price=100,
        current_price=102,
        quantity=2,
        initial_quantity=2,
        margin=200,
        leverage=1,
        stop_loss=95,
        tp1=102,
        tp2=108,
        best_price=102,
        exchange_open_order={"orderId": 10},
        exchange_stop_order={"orderId": 11, "clientOrderId": "hy_sl_strategy_partial_sync"},
        exchange_tp_order={"orderId": 12, "clientOrderId": "hy_tp_strategy_partial_sync"},
    )
    calls = []

    async def fake_reduce_position(position, quantity, reason="reduce"):
        return {"orderId": 21, "executedQty": str(quantity), "avgPrice": "102"}

    async def fake_replace_protection_orders(position, reason):
        calls.append(
            {
                "reason": reason,
                "quantity": position.quantity,
                "stop_loss": position.stop_loss,
                "tp2": position.tp2,
            }
        )
        return {"stop_order": {"orderId": 31}, "tp_order": {"orderId": 32}}

    monkeypatch.setattr("backend.trading.live_executor.live_executor.reduce_position", fake_reduce_position)
    monkeypatch.setattr("backend.trading.live_executor.live_executor.replace_protection_orders", fake_replace_protection_orders, raising=False)

    try:
        __import__("asyncio").run(position_manager.partial_tp1(p))
    finally:
        cleanup_symbol(symbol)

    assert p.quantity == 1
    assert calls == [
        {
            "reason": "TP1_PARTIAL",
            "quantity": 1,
            "stop_loss": p.stop_loss,
            "tp2": 108,
        }
    ]


def test_position_manager_exits_when_reverse_signal_invalidates_thesis(monkeypatch):
    position_registry.open.clear()
    symbol = "REVSEVEREUSDT"
    cleanup_symbol(symbol)
    try:
        p = Position(
            position_id="pos_reverse_severe",
            strategy_id="strat_reverse_severe",
            source_signal_id="scan_reverse_severe",
            symbol=symbol,
            side="LONG",
            status="OPEN",
            stage="Stage 1",
            score=72,
            entry_price=100,
            current_price=100,
            quantity=1,
            initial_quantity=1,
            margin=100,
            leverage=1,
            stop_loss=95,
            tp1=105,
            tp2=110,
            best_price=100,
        )
        position_registry.add(p)
        market_service.last_snapshots[symbol] = MarketSnapshot(symbol, 97, -1, -2, -3, 2, 1, 0, 0.2, 0.8, -0.2, 1, 0.2)
        radar_engine.top50 = [high_quality_item(symbol=symbol, side="SHORT", price=97)]
        patch_position_quote(monkeypatch, 97)
        __import__("asyncio").run(position_manager.manage_all())
        assert not position_registry.has_symbol(symbol)
        closed = [row for row in position_registry.list_closed() if row["symbol"] == symbol]
        assert closed[0]["close_reason"] == "REVERSE_THESIS_INVALIDATED"
    finally:
        cleanup_symbol(symbol)

class FakeRunner:
    def __init__(self, payload):
        self.payload = payload
        self.cmd = []

    def __call__(self, cmd, **kwargs):
        self.cmd = cmd
        output_path = Path(cmd[cmd.index("--output-last-message") + 1])
        output_path.write_text(json.dumps(self.payload), encoding="utf-8")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")


def _exchange_symbol_meta(symbol: str) -> dict:
    return {
        "symbol": symbol,
        "status": "TRADING",
        "contractType": "PERPETUAL",
        "quoteAsset": "USDT",
        "marginAsset": "USDT",
        "underlyingType": "COIN",
        "underlyingSubType": ["Test"],
    }


class FakeBinanceMarketClient:
    async def exchange_info(self):
        return {
            "symbols": [
                {
                    "symbol": "BTCUSDT",
                    "status": "TRADING",
                    "contractType": "PERPETUAL",
                    "quoteAsset": "USDT",
                    "marginAsset": "USDT",
                    "underlyingType": "COIN",
                    "underlyingSubType": ["PoW"],
                },
                {
                    "symbol": "ETHUSDT",
                    "status": "TRADING",
                    "contractType": "PERPETUAL",
                    "quoteAsset": "USDT",
                    "marginAsset": "USDT",
                    "underlyingType": "COIN",
                    "underlyingSubType": ["Layer-1"],
                },
            ]
        }

    async def premium_index(self):
        return [
            {"symbol": "BTCUSDT", "markPrice": "129", "lastFundingRate": "0.0001"},
            {"symbol": "ETHUSDT", "markPrice": "229", "lastFundingRate": "-0.0001"},
        ]

    async def ticker_24hr(self, symbol=None):
        return [
            {"symbol": "BTCUSDT", "lastPrice": "129", "quoteVolume": "2000000"},
            {"symbol": "ETHUSDT", "lastPrice": "229", "quoteVolume": "1000000"},
        ]

    async def klines(self, symbol, interval="5m", limit=30):
        self.kline_calls = getattr(self, "kline_calls", 0) + 1
        base = 100 if symbol == "BTCUSDT" else 200
        rows = []
        for i in range(30):
            open_price = base + i
            close = open_price + 1
            high = close + 1
            low = open_price - 1
            quote_volume = 1000 if i < 27 else 4000
            taker_buy_quote = quote_volume * 0.7
            rows.append([
                i,
                str(open_price),
                str(high),
                str(low),
                str(close),
                "10",
                i + 1,
                str(quote_volume),
                100,
                "7",
                str(taker_buy_quote),
                "0",
            ])
        return rows

    async def depth(self, symbol, limit=50):
        return {
            "bids": [["128", "10"], ["127", "8"]],
            "asks": [["130", "2"], ["131", "2"]],
        }

    async def open_interest(self, symbol):
        return {"symbol": symbol, "openInterest": "120"}

    async def open_interest_hist(self, symbol, period="5m", limit=30):
        return [{"sumOpenInterest": "100"}, {"sumOpenInterest": "120"}]

    async def taker_long_short_ratio(self, symbol, period="5m", limit=5):
        return [{"buyVol": "70", "sellVol": "30"}]

def high_quality_item(symbol="XUSDT", side="LONG", price=100):
    is_long = side == "LONG"
    return RadarItem(
        rank=1,
        symbol=symbol,
        base_asset=symbol.replace("USDT", ""),
        price=price,
        direction=side,
        stage="确认中",
        trigger_mode="评分加速",
        score=74,
        score_history=[40, 55, 74],
        rank_history=[8, 3, 1],
        heat_slope=8,
        slope_score=90,
        fake_breakout_risk="LOW",
        change_5m=1.2 if is_long else -1.2,
        change_15m=2.1 if is_long else -2.1,
        change_1h=1.0 if is_long else -1.0,
        oi_change=1.1,
        fund_confirm_count=3,
        fund_confirm_total=3,
        dealer_radar="多延" if is_long else "空延",
        sm_position=62,
        sm_delta=0.8 if is_long else -0.8,
        volume_spike=2.4,
        funding_rate=0.0002,
        taker_buy_ratio=0.68 if is_long else 0.32,
        taker_sell_ratio=0.32 if is_long else 0.68,
        depth_imbalance=0.22 if is_long else -0.22,
        atr_pct=1.2,
        wick_ratio=0.25,
    )

def stable_candidate_feature_report(item):
    score = float(getattr(item, "score", 0.0) or 0.0)
    feature_score = 80.0
    estimated_win_rate = 0.60 + score / 1000.0
    selection_score = 70.0 + score / 10.0
    return SimpleNamespace(
        feature_score=feature_score,
        estimated_win_rate=estimated_win_rate,
        selection_score=selection_score,
        reasons=["test_stable_feature_report"],
        asdict=lambda: {
            "feature_score": feature_score,
            "estimated_win_rate": estimated_win_rate,
            "selection_score": selection_score,
            "positive_factors": ["estimated_win_rate_above_paper_gate", "fund_confirm_full"],
            "failure_risks": [],
        },
    )

def sample_trade(side="LONG", pnl=1.0, close_time=1):
    is_long = side == "LONG"
    radar = high_quality_item(symbol="SAMPLEUSDT", side=side, price=100).asdict()
    return {
        "sample_id": f"sample_{side}_{close_time}_{pnl}",
        "symbol": "SAMPLEUSDT",
        "side": side,
        "direction": side,
        "pnl": pnl,
        "win": pnl > 0,
        "close_reason": "TP2" if pnl > 0 else "SL",
        "close_time": close_time,
        "open_time": close_time - 1,
        "strategy_id": "sample_strategy",
        "source_signal_id": "sample_scan",
        "radar": radar,
        "score": 82,
        "rank": 1,
        "fund_confirm_count": 3,
        "fake_breakout_risk": "LOW",
        "change_5m": 1.2 if is_long else -1.2,
        "change_15m": 2.1 if is_long else -2.1,
        "change_1h": 1.0 if is_long else -1.0,
        "oi_change": 1.1,
        "volume_spike": 2.4,
        "funding_rate": 0.0002,
        "taker_buy_ratio": 0.68 if is_long else 0.32,
        "taker_sell_ratio": 0.32 if is_long else 0.68,
        "depth_imbalance": 0.22 if is_long else -0.22,
        "atr_pct": 1.2,
        "wick_ratio": 0.25,
        "sm_delta": 0.8 if is_long else -0.8,
        "sm_position": 62,
        "heat_slope": 8,
        "slope_score": 90,
    }

def loose_test_strategy():
    return {
        "strategy_id": "test_evolved_strategy",
        "name": "test_evolved_strategy",
        "source": "test",
        "status": "WATCH",
        "filters": {
            "min_score": 70,
            "min_fund_confirm": 3,
            "allowed_fake_risks": ["LOW"],
            "min_direction_confirmations": 5,
            "min_volume_spike": 1.5,
            "max_wick_ratio": 0.65,
            "require_oi_positive": True,
            "require_timeframe_alignment": True,
            "require_taker_alignment": True,
            "require_depth_alignment": True,
            "require_sm_delta_alignment": True,
            "allowed_sides": ["LONG", "SHORT"],
        },
    }


def feedback_exec_plan(plan):
    return ExecutionPlan(
        decision="PAPER_ONLY",
        mode="paper",
        symbol=plan.symbol,
        side=plan.side,
        dynamic_margin=20,
        dynamic_leverage=2,
        quantity=0.2,
        entry_price=plan.ideal_entry_price,
        stop_loss=plan.stop_loss,
        tp1=plan.tp1,
        tp2=plan.tp2,
        tp1_close_ratio=0.5,
        tp2_close_ratio=1.0,
        management_mode="RISK_LOCK_AND_TRAIL",
        cooldown_after_trade=300,
        reason="test execution",
        notional=40,
        risk_usdt=1,
        risk_pct=0.1,
        strategy_contract=plan.raw.get("strategy_contract", {}),
    )


def feedback_position(strategy_id: str, symbol: str, position_id: str = "pos_ai_feedback"):
    return Position(
        position_id=position_id,
        strategy_id=strategy_id,
        source_signal_id="scan_ai_feedback",
        symbol=symbol,
        side="LONG",
        status="OPEN",
        stage="Stage 1",
        score=74,
        entry_price=100,
        current_price=100,
        quantity=0.2,
        initial_quantity=0.2,
        margin=20,
        leverage=2,
        stop_loss=99,
        tp1=101.2,
        tp2=102.5,
        best_price=100,
        notional=40,
        risk_usdt=1,
        risk_pct=0.1,
    )


def cleanup_symbol(symbol: str):
    for position_id, position in list(position_registry.open.items()):
        if position.symbol == symbol:
            position_registry.open.pop(position_id, None)
    position_registry.closed = [p for p in position_registry.closed if p.symbol != symbol]
    with db.conn() as conn:
        conn.execute("DELETE FROM positions WHERE symbol=?", (symbol,))
        conn.execute("DELETE FROM closed_positions WHERE symbol=?", (symbol,))


def patch_position_quote(monkeypatch, price: float, source: str = "book_ticker_mid"):
    async def fake_quote(symbol_arg, side_arg=None):
        return PriceQuote(
            symbol=symbol_arg,
            price=price,
            source=source,
            ts_ms=now_ms(),
            age_seconds=0.0,
            stale=False,
            bid=price,
            ask=price,
        )

    monkeypatch.setattr(market_service, "price_quote", fake_quote)


def cleanup_acceptance_strategies():
    with db.conn() as conn:
        conn.execute("DELETE FROM evolved_strategies WHERE strategy_id LIKE 'codex_acceptance_%'")
        conn.execute(
            "DELETE FROM evolved_strategies WHERE payload LIKE '%TOPVALIDATEUSDT%' OR payload LIKE '%ACCEPTUSDT%'"
        )
