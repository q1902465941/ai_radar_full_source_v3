from __future__ import annotations

from typing import Any

from backend.strategy_alpha.orchestrator import StrategyAlphaOrchestrator
from backend.strategy_alpha.registry import StrategyAlphaRegistry, strategy_alpha_registry
from backend.trading.prg.readiness_engine import readiness_engine


def strategy_alpha_status(
    *,
    registry: StrategyAlphaRegistry = strategy_alpha_registry,
    top_limit: int = 5,
    run_limit: int = 5,
) -> dict[str, Any]:
    pool_score = registry.strategy_pool_score()
    top = registry.top(limit=top_limit)
    runs = registry.runs(limit=run_limit)
    return {
        "ok": True,
        "sample_source": "research_alpha",
        "strategy_pool_score": pool_score,
        "pool_size": len(registry.list(limit=registry.pool_limit)),
        "top": top,
        "runs": runs,
        "latest_run": runs[0] if runs else None,
        "prg": readiness_engine.gate({"strategy_pool_score": pool_score}),
    }


def run_strategy_alpha_cycle(
    *,
    registry: StrategyAlphaRegistry = strategy_alpha_registry,
    market_data: list[dict[str, Any]] | None = None,
    generation_size: int = 20,
    mutation_size: int = 5,
) -> dict[str, Any]:
    orchestrator = StrategyAlphaOrchestrator(registry=registry)
    run = orchestrator.run_cycle(
        market_data=market_data,
        generation_size=_bounded_count(generation_size, default=20, maximum=200),
        mutation_size=_bounded_count(mutation_size, default=5, maximum=50),
    )
    return {
        "ok": True,
        "sample_source": "research_alpha",
        "run": run,
        "status": strategy_alpha_status(registry=registry),
    }


def _bounded_count(value: Any, *, default: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except Exception:
        parsed = default
    return max(0, min(maximum, parsed))
