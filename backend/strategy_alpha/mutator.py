from __future__ import annotations

import random
from typing import Any

from backend.models import now_ms
from backend.strategy_alpha.generator import FAKE_RISK_LEVELS, PARAM_BOUNDS, strategy_alpha_id


class StrategyAlphaMutator:
    def __init__(self, seed: int | None = None) -> None:
        self.random = random.Random(seed)

    def mutate(self, parent: dict[str, Any]) -> dict[str, Any]:
        parent_params = dict(parent.get("params") or {})
        params = dict(parent_params)
        for key, bounds in PARAM_BOUNDS.items():
            if key not in params:
                continue
            low, high = bounds
            if key in {"min_fund_confirm", "min_direction_confirmations", "horizon_steps"}:
                step = self.random.choice([-1, 0, 1])
                params[key] = int(_clamp(int(params[key]) + step, low, high))
            else:
                span = high - low
                delta = self.random.uniform(-0.12 * span, 0.12 * span)
                params[key] = round(_clamp(float(params[key]) + delta, low, high), 6)
        if self.random.random() < 0.2:
            params["max_fake_breakout_risk"] = self.random.choice(FAKE_RISK_LEVELS)
        if params == parent_params:
            params["min_score"] = round(_clamp(float(params.get("min_score", 50)) + 1.0, *PARAM_BOUNDS["min_score"]), 2)
        return {
            "strategy_id": strategy_alpha_id(params, salt=str(now_ms())),
            "parent_strategy_id": parent.get("strategy_id"),
            "source": "strategy_alpha",
            "status": "RESEARCH_ALPHA",
            "params": params,
            "created_at": now_ms(),
        }


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))
