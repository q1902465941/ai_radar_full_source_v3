from __future__ import annotations

from typing import Any


class StrategyPromotionPolicy:
    def can_promote_to_micro_live(self, strategy: dict[str, Any]) -> bool:
        return self.review(strategy)["allowed"]

    def review(self, strategy: dict[str, Any]) -> dict[str, Any]:
        alpha_score = _metric(strategy, "alpha_score")
        stability_score = _metric(strategy, "stability_score")
        overfit_risk = _metric(strategy, "overfit_risk", default=1.0)
        reasons: list[str] = []
        if alpha_score < 70:
            reasons.append("ALPHA_SCORE_LOW")
        if stability_score <= 0.6:
            reasons.append("STABILITY_SCORE_LOW")
        if overfit_risk >= 0.3:
            reasons.append("OVERFIT_RISK_HIGH")
        return {
            "allowed": not reasons,
            "reasons": reasons,
            "alpha_score": alpha_score,
            "stability_score": stability_score,
            "overfit_risk": overfit_risk,
            "target_stage": "MICRO_LIVE_CANDIDATE" if not reasons else "RESEARCH_ALPHA",
        }


def _metric(strategy: dict[str, Any], key: str, default: float = 0.0) -> float:
    value = strategy.get(key)
    if value is None and isinstance(strategy.get("evaluation"), dict):
        value = strategy["evaluation"].get(key)
    try:
        return float(value)
    except Exception:
        return default


strategy_promotion_policy = StrategyPromotionPolicy()
