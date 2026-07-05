from __future__ import annotations

from typing import Any

from backend.config import settings

BLOCK_LIVE = "BLOCK_LIVE"
PAPER_PROBE = "PAPER_PROBE"
MICRO_LIVE_CANDIDATE = "MICRO_LIVE_CANDIDATE"
MICRO_LIVE_ALLOWED = "MICRO_LIVE_ALLOWED"
PRG_SCORE_BELOW_MICRO_LIVE = "PRG_SCORE_BELOW_MICRO_LIVE"
PRG_MICRO_LIVE_CANDIDATE_NOT_LIVE_ELIGIBLE = "PRG_MICRO_LIVE_CANDIDATE_NOT_LIVE_ELIGIBLE"


class ReadinessEngine:
    """Production Readiness Gate scoring and enforcement.

    The PRG score is intentionally conservative: missing metrics do not earn
    points, and real orders require at least MICRO_LIVE level.
    """

    def evaluate(self, metrics: dict[str, Any]) -> int:
        if "strategy_pool_score" in metrics:
            return int(max(0, min(100, round(_safe_float(metrics.get("strategy_pool_score"))))))
        score = 0
        if _safe_float(metrics.get("sharpe")) > 1.0:
            score += 30
        if _safe_float(metrics.get("max_drawdown"), 1.0) < 0.1:
            score += 25
        if _safe_float(metrics.get("winrate", metrics.get("win_rate"))) > 0.55:
            score += 20
        if _safe_float(metrics.get("profit_factor")) > 1.2:
            score += 25
        return score

    def level(self, score: int | float) -> str:
        value = float(score)
        if value <= 40:
            return BLOCK_LIVE
        if value < 70:
            return PAPER_PROBE
        if value < 85:
            return MICRO_LIVE_CANDIDATE
        return MICRO_LIVE_ALLOWED

    def gate(self, metrics: dict[str, Any]) -> dict[str, Any]:
        normalized = self.normalize_metrics(metrics)
        score = self.evaluate(normalized)
        level = self.level(score)
        return {
            "score": score,
            "level": level,
            "metrics": normalized,
            "allowed": score >= 85,
            "mode": self.execution_mode(score),
            "reason": self.reason(score),
        }

    def enforce(self, metrics: dict[str, Any], *, settings_obj: Any = settings) -> dict[str, Any]:
        report = self.gate(metrics)
        if not report["allowed"]:
            settings_obj.live_trading_enabled = False
            report["allowed"] = False
        return report

    def execution_mode(self, score: int | float) -> str:
        level = self.level(score)
        if level == BLOCK_LIVE:
            return "BLOCK_LIVE"
        if level == PAPER_PROBE:
            return "PAPER_PROBE"
        if level == MICRO_LIVE_CANDIDATE:
            return "MICRO_LIVE_CANDIDATE"
        return "MICRO_LIVE_ALLOWED"

    def reason(self, score: int | float) -> str:
        value = float(score)
        if value < 70:
            return PRG_SCORE_BELOW_MICRO_LIVE
        if value < 85:
            return PRG_MICRO_LIVE_CANDIDATE_NOT_LIVE_ELIGIBLE
        return ""

    def metrics_from_readiness(self, readiness: dict[str, Any]) -> dict[str, Any]:
        metrics = readiness.get("metrics") if isinstance(readiness, dict) else {}
        if not isinstance(metrics, dict):
            return self.normalize_metrics({})
        if "strategy_pool_score" in metrics:
            return self.normalize_metrics({"strategy_pool_score": metrics.get("strategy_pool_score")})
        explicit = readiness.get("prg") if isinstance(readiness.get("prg"), dict) else metrics.get("prg")
        if isinstance(explicit, dict):
            if "strategy_pool_score" in explicit:
                return self.normalize_metrics({"strategy_pool_score": explicit.get("strategy_pool_score")})
            if isinstance(explicit.get("metrics"), dict):
                return self.normalize_metrics(explicit["metrics"])
            return self.normalize_metrics(explicit)

        performance = metrics.get("performance") if isinstance(metrics.get("performance"), dict) else {}
        attribution = metrics.get("attribution") if isinstance(metrics.get("attribution"), dict) else {}
        return self.normalize_metrics(
            {
                "sharpe": performance.get("sharpe", attribution.get("sharpe", 0.0)),
                "max_drawdown": performance.get(
                    "max_drawdown",
                    performance.get("drawdown", attribution.get("max_drawdown", attribution.get("drawdown", 1.0))),
                ),
                "winrate": performance.get("win_rate", attribution.get("global_win_rate", 0.0)),
                "profit_factor": performance.get("profit_factor", attribution.get("global_profit_factor", 0.0)),
            }
        )

    def normalize_metrics(self, metrics: dict[str, Any]) -> dict[str, float]:
        if "strategy_pool_score" in metrics:
            return {"strategy_pool_score": _safe_float(metrics.get("strategy_pool_score"))}
        return {
            "sharpe": _safe_float(metrics.get("sharpe")),
            "max_drawdown": _safe_float(metrics.get("max_drawdown", metrics.get("drawdown")), 1.0),
            "winrate": _safe_float(metrics.get("winrate", metrics.get("win_rate"))),
            "profit_factor": _safe_float(metrics.get("profit_factor")),
        }


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


readiness_engine = ReadinessEngine()
