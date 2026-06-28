from __future__ import annotations

from typing import Any

from strategy.adapters import EvolvedStrategyAdapter


class StrategyUniverse:
    def __init__(self):
        self.strategies = []
        self.weights: dict[str, float] = {}

    def add(self, strategy):
        self.strategies.append(strategy)
        return strategy

    def load_registry(self, registry: Any | None = None, limit: int = 50) -> int:
        if registry is None:
            from backend.learning.strategy_registry import strategy_registry

            registry = strategy_registry
        loaded = 0
        for strategy in registry.list(limit=limit):
            self.add(EvolvedStrategyAdapter(strategy))
            loaded += 1
        return loaded

    def generate_all(self, market):
        signals = []

        for s in self.strategies:
            sig = s.generate_signal(market)
            if sig:
                signals.append(sig)

        return signals

    def update_weights(self, weights: dict[str, float]) -> None:
        self.weights = dict(weights)
        for strategy in self.strategies:
            if hasattr(strategy, "update_weights"):
                strategy.update_weights(self.weights)
            elif hasattr(strategy, "weights"):
                strategy.weights = dict(self.weights)
