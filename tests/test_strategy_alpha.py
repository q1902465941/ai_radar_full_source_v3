from pathlib import Path

import pytest

from backend.config import settings
from backend.storage.db import DB
from backend.trading.prg.readiness_engine import readiness_engine


def _row(ts, price, *, score=80, direction="LONG", symbol="BTCUSDT", **extra):
    payload = {
        "scan_id": f"scan_{ts}",
        "symbol": symbol,
        "ts_ms": ts,
        "price": price,
        "direction": direction,
        "score": score,
        "wick_ratio": 0.2,
        "fund_confirm_count": 3,
        "direction_confirmations": 6,
        "volume_spike": 2.0,
        "depth_imbalance": 0.25 if direction == "LONG" else -0.25,
        "taker_buy_ratio": 0.65,
        "taker_sell_ratio": 0.65,
        "fake_breakout_risk": "LOW",
    }
    payload.update(extra)
    return payload


def test_generator_validity():
    from backend.strategy_alpha.generator import StrategyAlphaGenerator

    strategies = StrategyAlphaGenerator(seed=7).generate(count=5)

    assert len(strategies) == 5
    assert len({strategy["strategy_id"] for strategy in strategies}) == 5
    for strategy in strategies:
        params = strategy["params"]
        assert strategy["source"] == "strategy_alpha"
        assert strategy["status"] == "RESEARCH_ALPHA"
        assert 30 <= params["min_score"] <= 95
        assert 0.05 <= params["max_wick_ratio"] <= 0.8
        assert 1 <= params["min_fund_confirm"] <= 3
        assert 3 <= params["min_direction_confirmations"] <= 8
        assert 0.8 <= params["min_volume_spike"] <= 3.5
        assert 0.0 <= params["min_depth_alignment"] <= 0.5
        assert 0.5 <= params["min_taker_ratio"] <= 0.75
        assert 1.0 <= params["tp_r"] <= 3.5
        assert 0.006 <= params["risk_pct"] <= 0.025


def test_seed_bank_initial_strategies():
    from backend.strategy_alpha.seed_bank import SeedBank

    seeds = SeedBank().get_initial_strategies()

    assert [seed["alpha_type"] for seed in seeds] == ["momentum", "mean_reversion", "breakout", "radar_flow"]
    for seed in seeds:
        params = seed["params"]
        assert seed["source"] == "strategy_alpha_seed"
        assert seed["status"] == "RESEARCH_ALPHA"
        assert params["alpha_type"] == seed["alpha_type"]
        assert params["lookback"] > 0
        assert params["threshold"] > 0
        assert 30 <= params["min_score"] <= 95
        assert 0.006 <= params["risk_pct"] <= 0.025


def test_replay_engine_no_live_dependency(monkeypatch):
    from backend.strategy_alpha.generator import StrategyAlphaGenerator
    from backend.strategy_alpha.replay_engine import StrategyAlphaReplayEngine
    from backend.trading.live_executor import live_executor

    async def fail_open_position(*args, **kwargs):
        raise AssertionError("strategy alpha replay must not call live execution")

    monkeypatch.setattr(live_executor, "open_position", fail_open_position)
    strategy = StrategyAlphaGenerator(seed=1).generate(count=1)[0]
    strategy["params"].update(
        {
            "min_score": 50,
            "max_wick_ratio": 0.5,
            "min_fund_confirm": 1,
            "min_direction_confirmations": 1,
            "min_volume_spike": 1.0,
            "min_depth_alignment": 0.0,
            "min_taker_ratio": 0.5,
            "max_fake_breakout_risk": "LOW",
            "tp_r": 1.0,
            "risk_pct": 0.01,
            "horizon_steps": 2,
        }
    )

    report = StrategyAlphaReplayEngine().simulate(
        strategy,
        [_row(1, 100), _row(2, 101.5), _row(3, 102)],
    )

    assert report["sample_source"] == "research_alpha"
    assert report["trades"]


def test_replay_engine_uses_only_forward_market_slices():
    from backend.strategy_alpha.replay_engine import StrategyAlphaReplayEngine

    strategy = {
        "strategy_id": "alpha_forward_only",
        "params": {
            "min_score": 70,
            "max_wick_ratio": 0.5,
            "min_fund_confirm": 3,
            "min_direction_confirmations": 5,
            "min_volume_spike": 1.0,
            "min_depth_alignment": 0.1,
            "min_taker_ratio": 0.6,
            "max_fake_breakout_risk": "LOW",
            "tp_r": 1.0,
            "risk_pct": 0.01,
            "horizon_steps": 2,
        },
    }
    rows = [
        _row(1, 100, score=20),
        _row(2, 100),
        _row(3, 101.5),
        _row(4, 99),
    ]

    report = StrategyAlphaReplayEngine(window_count=2).simulate(strategy, rows)

    assert all(trade["open_time"] > 1 for trade in report["trades"])
    assert all(trade["close_time"] > trade["open_time"] for trade in report["trades"])
    assert len(report["windows"]) == 2


def test_replay_engine_applies_fees_and_slippage_to_net_r(monkeypatch):
    from backend.strategy_alpha.replay_engine import StrategyAlphaReplayEngine

    monkeypatch.setattr(settings, "paper_taker_fee_rate", 0.001)
    monkeypatch.setattr(settings, "paper_slippage_pct", 0.001)
    strategy = {
        "strategy_id": "alpha_cost_adjusted",
        "params": {
            "min_score": 70,
            "max_wick_ratio": 0.5,
            "min_fund_confirm": 3,
            "min_direction_confirmations": 5,
            "min_volume_spike": 1.0,
            "min_depth_alignment": 0.0,
            "min_taker_ratio": 0.6,
            "max_fake_breakout_risk": "LOW",
            "tp_r": 3.5,
            "risk_pct": 0.01,
            "horizon_steps": 1,
        },
    }

    report = StrategyAlphaReplayEngine().simulate(
        strategy,
        [_row(1, 100), _row(2, 101.5, direction="NEUTRAL", score=10)],
    )

    trade = report["trades"][0]
    assert trade["gross_r"] == pytest.approx(1.5)
    assert trade["cost_r"] == pytest.approx(0.4)
    assert trade["pnl"] == pytest.approx(1.1)


def test_replay_engine_prevents_overlapping_symbol_trades():
    from backend.strategy_alpha.replay_engine import StrategyAlphaReplayEngine

    strategy = {
        "strategy_id": "alpha_non_overlap",
        "params": {
            "min_score": 70,
            "max_wick_ratio": 0.5,
            "min_fund_confirm": 3,
            "min_direction_confirmations": 5,
            "min_volume_spike": 1.0,
            "min_depth_alignment": 0.0,
            "min_taker_ratio": 0.6,
            "max_fake_breakout_risk": "LOW",
            "tp_r": 3.5,
            "risk_pct": 0.05,
            "horizon_steps": 2,
        },
    }

    report = StrategyAlphaReplayEngine().simulate(
        strategy,
        [
            _row(1, 100),
            _row(2, 100.1),
            _row(3, 100.2),
            _row(4, 100.3),
        ],
    )

    assert [trade["open_time"] for trade in report["trades"]] == [1, 3]
    assert all(
        previous["close_time"] <= current["open_time"]
        for previous, current in zip(report["trades"], report["trades"][1:])
    )


def test_replay_engine_reads_nested_direction_confirmations_from_radar_payload():
    from backend.strategy_alpha.replay_engine import StrategyAlphaReplayEngine

    strategy = {
        "strategy_id": "alpha_nested_direction_confirmations",
        "params": {
            "min_score": 70,
            "max_wick_ratio": 0.5,
            "min_fund_confirm": 3,
            "min_direction_confirmations": 5,
            "min_volume_spike": 1.0,
            "min_depth_alignment": 0.0,
            "min_taker_ratio": 0.6,
            "max_fake_breakout_risk": "LOW",
            "tp_r": 1.0,
            "risk_pct": 0.01,
            "horizon_steps": 2,
        },
    }
    entry = _row(1, 100, score=90)
    entry.pop("direction_confirmations")
    entry["score_explain"] = {"direction_confirmations": 6}

    report = StrategyAlphaReplayEngine().simulate(
        strategy,
        [
            entry,
            _row(2, 102, direction="NEUTRAL", score=10),
            _row(3, 103, direction="NEUTRAL", score=10),
        ],
    )

    assert len(report["trades"]) == 1


def test_alpha_score_calculation():
    from backend.strategy_alpha.evaluator import StrategyAlphaEvaluator

    evaluation = StrategyAlphaEvaluator().evaluate(
        [
            {"pnl": 1.2},
            {"pnl": -0.4},
            {"pnl": 1.4},
            {"pnl": 0.6},
        ]
    )

    assert evaluation["pnl"] == pytest.approx(2.8)
    assert evaluation["winrate"] == pytest.approx(0.75)
    assert evaluation["profit_factor"] == pytest.approx(8.0)
    assert evaluation["max_drawdown"] == pytest.approx(0.4)
    assert evaluation["alpha_score"] >= 80


def test_mutation_stability():
    from backend.strategy_alpha.generator import StrategyAlphaGenerator
    from backend.strategy_alpha.mutator import StrategyAlphaMutator

    parent = StrategyAlphaGenerator(seed=9).generate(count=1)[0]
    child = StrategyAlphaMutator(seed=9).mutate(parent)

    assert child["parent_strategy_id"] == parent["strategy_id"]
    assert child["strategy_id"] != parent["strategy_id"]
    assert child["params"] != parent["params"]
    assert 30 <= child["params"]["min_score"] <= 95
    assert 0.006 <= child["params"]["risk_pct"] <= 0.025
    assert 1.0 <= child["params"]["tp_r"] <= 3.5


def test_registry_isolated_storage(tmp_path):
    from backend.strategy_alpha.registry import StrategyAlphaRegistry

    alpha_db = DB(str(tmp_path / "alpha.sqlite"))
    registry = StrategyAlphaRegistry(db_obj=alpha_db)
    registry.save({"strategy_id": "alpha_a", "params": {}}, {"alpha_score": 88, "stability_score": 0.7, "overfit_risk": 0.2})

    with alpha_db.conn() as conn:
        evolved_count = conn.execute("SELECT COUNT(*) AS n FROM evolved_strategies").fetchone()["n"]

    assert evolved_count == 0
    assert registry.top(limit=1)[0]["strategy_id"] == "alpha_a"
    assert registry.strategy_pool_score() == 88


def test_promotion_policy_blocks_high_score_overfit():
    from backend.strategy_alpha.promotion import StrategyPromotionPolicy

    policy = StrategyPromotionPolicy()

    assert policy.can_promote_to_micro_live({"alpha_score": 95, "stability_score": 0.5, "overfit_risk": 0.1}) is False
    assert policy.can_promote_to_micro_live({"alpha_score": 72, "stability_score": 0.7, "overfit_risk": 0.2}) is True


def test_prg_strategy_pool_gate():
    candidate = readiness_engine.gate({"strategy_pool_score": 80})
    allowed = readiness_engine.gate({"strategy_pool_score": 90})

    assert candidate["score"] == 80
    assert candidate["level"] == "MICRO_LIVE_CANDIDATE"
    assert candidate["allowed"] is False
    assert candidate["mode"] == "MICRO_LIVE_CANDIDATE"
    assert allowed["level"] == "MICRO_LIVE_ALLOWED"
    assert allowed["allowed"] is True


def test_no_live_executor_call_from_alpha(monkeypatch, tmp_path):
    from backend.strategy_alpha.orchestrator import StrategyAlphaOrchestrator
    from backend.strategy_alpha.registry import StrategyAlphaRegistry
    from backend.trading.live_executor import live_executor

    async def fail_open_position(*args, **kwargs):
        raise AssertionError("alpha orchestrator must not call live executor")

    monkeypatch.setattr(live_executor, "open_position", fail_open_position)
    registry = StrategyAlphaRegistry(db_obj=DB(str(Path(tmp_path) / "alpha.sqlite")))
    out = StrategyAlphaOrchestrator(registry=registry, seed=3).run_cycle(
        market_data=[_row(1, 100), _row(2, 102), _row(3, 101), _row(4, 103)],
        generation_size=2,
        mutation_size=1,
    )

    assert out["stored_count"] >= 1
    assert out["sample_source"] == "research_alpha"


def test_orchestrator_warm_starts_from_seed_bank(tmp_path):
    from backend.strategy_alpha.orchestrator import StrategyAlphaOrchestrator
    from backend.strategy_alpha.registry import StrategyAlphaRegistry

    registry = StrategyAlphaRegistry(db_obj=DB(str(Path(tmp_path) / "alpha.sqlite")))
    out = StrategyAlphaOrchestrator(registry=registry, seed=4).run_cycle(
        market_data=[_row(1, 100), _row(2, 102), _row(3, 101), _row(4, 103)],
        generation_size=0,
        mutation_size=0,
    )

    stored = registry.top(limit=10)
    assert out["seed_count"] == 4
    assert out["generated_count"] == 0
    assert out["stored_count"] == 4
    assert {row["alpha_type"] for row in stored} == {"momentum", "mean_reversion", "breakout", "radar_flow"}


def test_orchestrator_loads_default_market_data_once_per_cycle(tmp_path):
    from backend.strategy_alpha.orchestrator import StrategyAlphaOrchestrator
    from backend.strategy_alpha.registry import StrategyAlphaRegistry

    class FakeReplayEngine:
        def __init__(self):
            self.loads = 0
            self.market_data_seen = []

        def _load_market_data(self, *, limit):
            self.loads += 1
            return [_row(1, 100), _row(2, 102)]

        def simulate(self, strategy, market_data):
            assert market_data is not None
            self.market_data_seen.append(market_data)
            return {
                "trades": [{"pnl": 1.0}],
                "windows": [{"window": 0, "trades": 1, "pnl": 1.0}],
            }

    registry = StrategyAlphaRegistry(db_obj=DB(str(Path(tmp_path) / "alpha.sqlite")))
    replay_engine = FakeReplayEngine()

    out = StrategyAlphaOrchestrator(registry=registry, replay_engine=replay_engine, seed=5).run_cycle(
        generation_size=2,
        mutation_size=0,
    )

    assert out["stored_count"] == 6
    assert replay_engine.loads == 1
    assert len(replay_engine.market_data_seen) == 6
    assert len({id(rows) for rows in replay_engine.market_data_seen}) == 1


def test_strategy_alpha_service_run_cycle_updates_prg_source(tmp_path):
    from backend.strategy_alpha.registry import POOL_KEY, RUNS_KEY, StrategyAlphaRegistry
    from backend.strategy_alpha.service import run_strategy_alpha_cycle, strategy_alpha_status

    alpha_db = DB(str(Path(tmp_path) / "alpha.sqlite"))
    registry = StrategyAlphaRegistry(db_obj=alpha_db)

    before = strategy_alpha_status(registry=registry)
    out = run_strategy_alpha_cycle(
        registry=registry,
        market_data=[_row(1, 100), _row(2, 102), _row(3, 101), _row(4, 103)],
        generation_size=0,
        mutation_size=0,
    )
    after = strategy_alpha_status(registry=registry)

    with alpha_db.conn() as conn:
        keys = [row["key"] for row in conn.execute("SELECT key FROM kv WHERE key LIKE 'strategy_alpha.%'").fetchall()]

    assert before["pool_size"] == 0
    assert out["ok"] is True
    assert out["run"]["stored_count"] == 4
    assert after["pool_size"] == 4
    assert after["prg"]["score"] == after["strategy_pool_score"]
    assert after["sample_source"] == "research_alpha"
    assert POOL_KEY in keys
    assert RUNS_KEY in keys
