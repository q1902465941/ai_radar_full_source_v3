from __future__ import annotations

from typing import Any

from backend.config import settings
from backend.learning.strategy_filter import strategy_matches


class BacktestEngine:
    def evaluate(self, strategy: dict[str, Any], samples: list[dict[str, Any]]) -> dict[str, Any]:
        ordered = sorted(samples, key=lambda x: int(x.get("close_time") or 0))
        split_at = int(len(ordered) * min(max(settings.evolve_train_split, 0.1), 0.9))
        selected = []
        train = []
        holdout = []
        for index, sample in enumerate(ordered):
            if not strategy_matches(strategy, sample):
                continue
            selected.append(sample)
            if index < split_at:
                train.append(sample)
            else:
                holdout.append(sample)
        metrics = self._metrics(selected)
        train_metrics = self._metrics(train)
        holdout_metrics = self._metrics(holdout)
        eligible = self._eligible(metrics, holdout_metrics)
        return {
            **metrics,
            "eligible": eligible,
            "eligible_reasons": self._eligibility_reasons(metrics, holdout_metrics),
            "train": train_metrics,
            "holdout": holdout_metrics,
        }

    def _metrics(self, samples: list[dict[str, Any]]) -> dict[str, Any]:
        pnls = [float(sample.get("pnl", 0.0) or 0.0) for sample in samples]
        wins = [pnl for pnl in pnls if pnl > 0]
        losses = [pnl for pnl in pnls if pnl < 0]
        gross_profit = sum(wins)
        gross_loss = abs(sum(losses))
        equity = 0.0
        peak = 0.0
        max_drawdown = 0.0
        for pnl in pnls:
            equity += pnl
            peak = max(peak, equity)
            max_drawdown = min(max_drawdown, equity - peak)
        count = len(pnls)
        return {
            "trades": count,
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": round(len(wins) / max(1, len(wins) + len(losses)), 4),
            "pnl": round(sum(pnls), 4),
            "avg_pnl": round(sum(pnls) / count, 6) if count else 0.0,
            "profit_factor": round(gross_profit / gross_loss, 4) if gross_loss > 0 else (999.0 if gross_profit > 0 else 0.0),
            "max_drawdown": round(max_drawdown, 4),
        }

    def _eligible(self, metrics: dict[str, Any], holdout: dict[str, Any]) -> bool:
        return not self._eligibility_reasons(metrics, holdout)

    def _eligibility_reasons(self, metrics: dict[str, Any], holdout: dict[str, Any]) -> list[str]:
        reasons: list[str] = []
        if metrics["trades"] < settings.evolve_min_backtest_trades:
            reasons.append("sample_count_low")
        if holdout["trades"] < settings.evolve_min_holdout_trades:
            reasons.append("holdout_sample_count_low")
        if metrics["win_rate"] < settings.evolve_min_win_rate:
            reasons.append("win_rate_low")
        if holdout["win_rate"] < settings.evolve_min_holdout_win_rate:
            reasons.append("holdout_win_rate_low")
        if metrics["profit_factor"] < settings.evolve_min_profit_factor:
            reasons.append("profit_factor_low")
        if holdout["profit_factor"] < 1.0:
            reasons.append("holdout_profit_factor_low")
        if metrics["pnl"] <= settings.evolve_min_net_pnl:
            reasons.append("net_pnl_low")
        if holdout["pnl"] <= 0:
            reasons.append("holdout_net_pnl_low")
        return reasons


backtest_engine = BacktestEngine()
