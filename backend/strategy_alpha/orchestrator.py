from __future__ import annotations

from typing import Any

from backend.models import new_id, now_ms
from backend.strategy_alpha.evaluator import StrategyAlphaEvaluator
from backend.strategy_alpha.generator import StrategyAlphaGenerator
from backend.strategy_alpha.mutator import StrategyAlphaMutator
from backend.strategy_alpha.registry import StrategyAlphaRegistry, strategy_alpha_registry
from backend.strategy_alpha.replay_engine import StrategyAlphaReplayEngine
from backend.strategy_alpha.seed_bank import SeedBank, seed_bank


class StrategyAlphaOrchestrator:
    def __init__(
        self,
        *,
        generator: StrategyAlphaGenerator | None = None,
        replay_engine: StrategyAlphaReplayEngine | None = None,
        evaluator: StrategyAlphaEvaluator | None = None,
        registry: StrategyAlphaRegistry = strategy_alpha_registry,
        mutator: StrategyAlphaMutator | None = None,
        seeds: SeedBank = seed_bank,
        seed: int | None = None,
    ) -> None:
        self.generator = generator or StrategyAlphaGenerator(seed=seed)
        self.replay_engine = replay_engine or StrategyAlphaReplayEngine(db_obj=registry.db)
        self.evaluator = evaluator or StrategyAlphaEvaluator()
        self.registry = registry
        self.mutator = mutator or StrategyAlphaMutator(seed=seed)
        self.seeds = seeds

    def run_cycle(
        self,
        *,
        market_data: list[dict[str, Any]] | None = None,
        generation_size: int = 20,
        mutation_size: int = 5,
    ) -> dict[str, Any]:
        stored: list[dict[str, Any]] = []
        cycle_market_data = self._cycle_market_data(market_data)
        seed_strategies = self.seeds.get_initial_strategies()
        for strategy in seed_strategies:
            stored.append(self._simulate_evaluate_store(strategy, cycle_market_data))

        generated = self.generator.generate(count=generation_size) if int(generation_size) > 0 else []
        for strategy in generated:
            stored.append(self._simulate_evaluate_store(strategy, cycle_market_data))

        parents = self.registry.top(limit=max(0, int(mutation_size))) if int(mutation_size) > 0 else []
        mutated = [self.mutator.mutate(parent) for parent in parents[: max(0, int(mutation_size))]]
        for strategy in mutated:
            stored.append(self._simulate_evaluate_store(strategy, cycle_market_data))

        run = {
            "run_id": new_id("alpha_run"),
            "created_at": now_ms(),
            "sample_source": "research_alpha",
            "seed_count": len(seed_strategies),
            "generated_count": len(generated),
            "mutated_count": len(mutated),
            "stored_count": len(stored),
            "strategy_pool_score": self.registry.strategy_pool_score(),
            "top_strategy_ids": [row.get("strategy_id") for row in self.registry.top(limit=5)],
        }
        self.registry.save_run(run)
        return run

    def _simulate_evaluate_store(self, strategy: dict[str, Any], market_data: list[dict[str, Any]] | None) -> dict[str, Any]:
        simulation = self.replay_engine.simulate(strategy, market_data)
        evaluation = self.evaluator.evaluate(simulation["trades"], simulation["windows"])
        return self.registry.save(strategy, evaluation)

    def _cycle_market_data(self, market_data: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
        if market_data is not None:
            return list(market_data)
        return self.replay_engine._load_market_data(limit=20000)
