from __future__ import annotations

from typing import Any


class QualityGates:
    def check(self, trade_stats: dict[str, Any]) -> tuple[bool, str]:
        if _safe_float(trade_stats.get("profit_factor")) < 1.0:
            return False, "PF_TOO_LOW"
        if _safe_float(trade_stats.get("drawdown", trade_stats.get("max_drawdown"))) > 0.15:
            return False, "DRAWDOWN_TOO_HIGH"
        if _safe_float(trade_stats.get("winrate", trade_stats.get("win_rate"))) < 0.5:
            return False, "WINRATE_TOO_LOW"
        return True, "PASS"


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


quality_gates = QualityGates()
