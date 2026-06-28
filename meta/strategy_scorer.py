from __future__ import annotations

from typing import Any


class StrategyScorer:
    def __init__(self, weights: dict[str, float] | None = None):
        self.weights = dict(weights or {})

    def score(self, trades):
        pnl = sum(_float(t.get("pnl")) for t in trades)
        winrate = len([t for t in trades if _float(t.get("pnl")) > 0]) / max(len(trades), 1)
        drawdown = min([_float(t.get("pnl")) for t in trades], default=0)

        return round(pnl * 0.5 + winrate * 0.3 - abs(drawdown) * 0.2, 10)

    def rank(self, signals: list[dict[str, Any]]) -> list[dict[str, Any]]:
        ranked = []
        for signal in signals:
            row = dict(signal)
            name = str(row.get("name") or row.get("strategy") or row.get("strategy_id") or row.get("symbol") or "strategy")
            row["name"] = name
            base_score = _float(row.get("score"))
            trade_score = self.score(list(row.get("trades") or []))
            learning_weight = _float(row.get("learning_weight"), self.weights.get(name, 1.0))
            row["learning_weight"] = learning_weight
            row["trade_score"] = trade_score
            row["strategy_score"] = round(base_score * learning_weight + trade_score, 10)
            row["volatility"] = max(0.0, _float(row.get("volatility"), 1.0))
            ranked.append(row)
        return sorted(ranked, key=lambda row: row["strategy_score"], reverse=True)


def _float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default
