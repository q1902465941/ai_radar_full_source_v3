from __future__ import annotations

import math
from typing import Any


class StrategyAlphaEvaluator:
    def evaluate(self, trades: list[dict[str, Any]], windows: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        pnl_values = [_safe_float(row.get("pnl", row.get("pnl_r"))) for row in trades]
        wins = [value for value in pnl_values if value > 0]
        losses = [value for value in pnl_values if value < 0]
        pnl = round(sum(pnl_values), 6)
        gross_win = sum(wins)
        gross_loss = abs(sum(losses))
        profit_factor = 10.0 if gross_win > 0 and gross_loss <= 0 else (gross_win / gross_loss if gross_loss > 0 else 0.0)
        winrate = len(wins) / max(1, len(wins) + len(losses))
        max_drawdown = _max_drawdown(pnl_values)
        sharpe = _sharpe(pnl_values)
        stability_score = _stability_score(windows, pnl_values)
        overfit_risk = _overfit_risk(windows, len(pnl_values), stability_score)
        alpha_score = _alpha_score(pnl, winrate, profit_factor, max_drawdown, stability_score, overfit_risk)
        return {
            "sample_source": "research_alpha",
            "trades": len(trades),
            "wins": len(wins),
            "losses": len(losses),
            "pnl": pnl,
            "winrate": round(winrate, 6),
            "profit_factor": round(profit_factor, 6),
            "max_drawdown": round(max_drawdown, 6),
            "sharpe": round(sharpe, 6),
            "stability_score": round(stability_score, 6),
            "overfit_risk": round(overfit_risk, 6),
            "alpha_score": round(alpha_score, 4),
        }


def _max_drawdown(values: list[float]) -> float:
    peak = 0.0
    equity = 0.0
    max_dd = 0.0
    for value in values:
        equity += value
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)
    return max_dd


def _sharpe(values: list[float]) -> float:
    if len(values) < 2:
        return 0.0
    mean = sum(values) / len(values)
    variance = sum((value - mean) ** 2 for value in values) / (len(values) - 1)
    std = math.sqrt(variance)
    if std <= 0:
        return 0.0
    return mean / std * math.sqrt(len(values))


def _stability_score(windows: list[dict[str, Any]] | None, values: list[float]) -> float:
    if windows:
        decided = [row for row in windows if int(row.get("trades") or 0) > 0]
        if not decided:
            return 0.0
        positive = sum(1 for row in decided if _safe_float(row.get("pnl")) > 0)
        return max(0.0, min(1.0, positive / len(decided)))
    pnl = sum(values)
    drawdown = _max_drawdown(values)
    if pnl <= 0:
        return 0.0
    return max(0.0, min(1.0, 1.0 - drawdown / max(abs(pnl) + drawdown, 0.0001)))


def _overfit_risk(windows: list[dict[str, Any]] | None, trade_count: int, stability_score: float) -> float:
    sample_penalty = 0.35 if trade_count < 10 else 0.0
    window_penalty = 0.25
    if windows:
        active_windows = sum(1 for row in windows if int(row.get("trades") or 0) > 0)
        window_penalty = 0.0 if active_windows >= 3 else 0.15
    return max(0.0, min(1.0, (1.0 - stability_score) * 0.5 + sample_penalty + window_penalty))


def _alpha_score(pnl: float, winrate: float, profit_factor: float, max_drawdown: float, stability_score: float, overfit_risk: float) -> float:
    pnl_score = min(25.0, max(0.0, pnl) * 8.0)
    win_score = min(25.0, max(0.0, winrate) / 0.65 * 25.0)
    pf_score = min(30.0, max(0.0, profit_factor - 1.0) / 2.0 * 30.0)
    dd_score = max(0.0, 20.0 * (1.0 - max_drawdown / 2.0))
    raw = pnl_score + win_score + pf_score + dd_score
    penalty = max(0.0, overfit_risk - 0.3) * 25.0
    stability_bonus = max(0.0, stability_score - 0.6) * 10.0
    return max(0.0, min(100.0, raw - penalty + stability_bonus))


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default
