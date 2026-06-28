from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from backend.config import settings
from backend.models import RadarItem, StrategyPlan, now_ms

EXCLUDED_CLOSE_REASONS = {"RESTORED_STALE_RECONCILE", "PRICE_SOURCE_STALE_RECONCILE", "ACCEPTANCE_TP2"}


@dataclass
class PerformanceGuardReport:
    allow: bool
    recovery_mode: bool
    reasons: list[str]
    global_trades: int
    global_win_rate: float
    global_pnl: float
    recent_win_rate: float
    loss_streak: int
    symbol_side_trades: int
    symbol_side_win_rate: float
    symbol_side_pnl: float
    direction_confirmations: int

    def asdict(self) -> dict[str, Any]:
        return asdict(self)


class PerformanceGuard:
    def summary(self) -> dict[str, Any]:
        rows = self._closed_rows()
        stats = self._stats(rows)
        recent = self._stats(rows[:50])
        return {
            "trades": stats["count"],
            "win_rate": stats["win_rate"],
            "pnl": stats["pnl"],
            "recent_win_rate": recent["win_rate"],
            "loss_streak": self.loss_streak(rows),
            "recovery_mode": self.recovery_mode(rows),
            "worst_symbol_sides": self._worst_symbol_sides(rows),
        }

    def precheck_candidate(self, item: RadarItem) -> tuple[bool, str]:
        rows = self._closed_rows()
        if len(rows) < settings.strategy_block_symbol_side_min_trades:
            return True, ""
        side = item.direction
        if side not in {"LONG", "SHORT"}:
            return False, "neutral_direction"
        symbol_rows = [x for x in rows if x.get("symbol") == item.symbol and x.get("side") == side]
        if self._symbol_side_blocked(symbol_rows):
            return False, "symbol_side_history_blocked"
        if self._recent_symbol_loss(symbol_rows) and item.score < settings.strategy_recovery_min_score:
            return False, "symbol_recent_loss_cooldown"
        if self.recovery_mode(rows):
            confirms = self._direction_confirmations(item, side)
            if item.score < settings.strategy_recovery_min_score or item.fund_confirm_count < 3 or confirms < 5:
                return False, "global_recovery_precheck"
        return True, ""

    def evaluate(self, item: RadarItem, plan: StrategyPlan, quality) -> PerformanceGuardReport:
        rows = self._closed_rows()
        stats = self._stats(rows)
        recent = self._stats(rows[:50])
        loss_streak = self.loss_streak(rows)
        recovery_mode = self.recovery_mode(rows)
        symbol_rows = [x for x in rows if x.get("symbol") == item.symbol and x.get("side") == plan.side]
        symbol_stats = self._stats(symbol_rows)
        confirmations = self._direction_confirmations(item, plan.side)
        reasons: list[str] = []

        if self._symbol_side_blocked(symbol_rows):
            reasons.append("symbol_side_history_blocked")

        if self._recent_symbol_loss(symbol_rows) and item.score < max(88.0, settings.strategy_recovery_min_score):
            reasons.append("symbol_recent_loss_cooldown")

        if loss_streak >= 3 and (
            item.score < 88
            or getattr(quality, "expected_r", 0.0) < 0.45
            or getattr(quality, "tp2_r", 0.0) < 2.5
        ):
            reasons.append("global_loss_streak_requires_exceptional_setup")

        if recovery_mode:
            if item.score < settings.strategy_recovery_min_score:
                reasons.append("recovery_score_low")
            if plan.confidence < settings.strategy_recovery_min_confidence:
                reasons.append("recovery_confidence_low")
            if item.fund_confirm_count < 3:
                reasons.append("recovery_fund_confirm_low")
            if item.fake_breakout_risk != "LOW":
                reasons.append("recovery_fake_breakout_not_low")
            if getattr(quality, "expected_r", 0.0) < settings.strategy_recovery_min_expected_r:
                reasons.append("recovery_expected_r_low")
            if getattr(quality, "tp2_r", 0.0) < settings.strategy_recovery_min_tp2_r:
                reasons.append("recovery_tp2_r_low")
            if confirmations < 5:
                reasons.append("recovery_direction_confirmation_low")

        return PerformanceGuardReport(
            allow=not reasons,
            recovery_mode=recovery_mode,
            reasons=reasons,
            global_trades=stats["count"],
            global_win_rate=stats["win_rate"],
            global_pnl=stats["pnl"],
            recent_win_rate=recent["win_rate"],
            loss_streak=loss_streak,
            symbol_side_trades=symbol_stats["count"],
            symbol_side_win_rate=symbol_stats["win_rate"],
            symbol_side_pnl=symbol_stats["pnl"],
            direction_confirmations=confirmations,
        )

    def recovery_mode(self, rows: list[dict] | None = None) -> bool:
        rows = self._ordered_rows(rows if rows is not None else self._closed_rows())
        if len(rows) < 30:
            return False
        stats = self._stats(rows)
        recent = self._stats(rows[:50])
        release_min = max(12, int(settings.evolve_min_backtest_trades or 12))
        if len(rows[:50]) >= release_min and recent["pnl"] > 0 and recent["win_rate"] >= max(0.52, float(settings.evolve_min_win_rate or 0.52)):
            return False
        return stats["pnl"] < 0 or stats["win_rate"] < 0.45 or recent["win_rate"] < 0.42

    def loss_streak(self, rows: list[dict] | None = None) -> int:
        rows = self._ordered_rows(rows if rows is not None else self._closed_rows())
        streak = 0
        for closed in rows:
            if float(closed.get("pnl", 0.0) or 0.0) < 0:
                streak += 1
                continue
            break
        return streak

    def _closed_rows(self) -> list[dict]:
        from backend.positions.position_registry import position_registry

        return self.performance_rows(position_registry.list_closed())

    def performance_rows(self, rows: list[dict]) -> list[dict]:
        return self._ordered_rows([row for row in rows if str(row.get("close_reason") or "") not in EXCLUDED_CLOSE_REASONS])

    def _stats(self, rows: list[dict]) -> dict[str, float]:
        count = len(rows)
        pnl = sum(float(x.get("pnl", 0.0) or 0.0) for x in rows)
        wins = sum(1 for x in rows if float(x.get("pnl", 0.0) or 0.0) > 0)
        losses = sum(1 for x in rows if float(x.get("pnl", 0.0) or 0.0) < 0)
        decided = wins + losses
        return {
            "count": count,
            "pnl": round(pnl, 4),
            "win_rate": round(wins / decided, 4) if decided else 0.0,
        }

    def _symbol_side_blocked(self, rows: list[dict]) -> bool:
        rows = self._ordered_rows(rows)
        if len(rows) < settings.strategy_block_symbol_side_min_trades:
            return False
        if self._recent_symbol_side_recovered(rows):
            return False
        stats = self._stats(rows)
        return stats["pnl"] < 0 and stats["win_rate"] < settings.strategy_block_symbol_side_win_rate

    def _recent_symbol_loss(self, rows: list[dict]) -> bool:
        rows = self._ordered_rows(rows)
        if not rows:
            return False
        latest = rows[0]
        if float(latest.get("pnl", 0.0) or 0.0) >= 0:
            return False
        close_time = int(latest.get("close_time") or 0)
        if close_time <= 0:
            return True
        age_ms = now_ms() - close_time
        return age_ms <= settings.strategy_symbol_cooldown_hours * 3600 * 1000

    def _recent_symbol_side_recovered(self, rows: list[dict]) -> bool:
        min_trades = max(2, int(settings.strategy_block_symbol_side_min_trades or 3))
        recent = self._ordered_rows(rows)[:min_trades]
        if len(recent) < min_trades:
            return False
        stats = self._stats(recent)
        return stats["pnl"] > 0 and stats["win_rate"] >= max(0.50, float(settings.strategy_block_symbol_side_win_rate or 0.34))

    def _ordered_rows(self, rows: list[dict]) -> list[dict]:
        return sorted(rows, key=lambda row: int(row.get("close_time") or row.get("ts_ms") or 0), reverse=True)

    def _direction_confirmations(self, item: RadarItem, side: str) -> int:
        if side == "LONG":
            checks = [
                item.change_5m > 0,
                item.change_15m > 0,
                item.change_1h >= 0,
                item.taker_buy_ratio >= 0.58,
                item.depth_imbalance >= 0.12,
                item.sm_delta >= 0,
                item.volume_spike >= 1.5,
                item.oi_change >= 0,
                item.wick_ratio <= 0.55,
            ]
        elif side == "SHORT":
            checks = [
                item.change_5m < 0,
                item.change_15m < 0,
                item.change_1h <= 0,
                item.taker_sell_ratio >= 0.58,
                item.depth_imbalance <= -0.12,
                item.sm_delta <= 0,
                item.volume_spike >= 1.5,
                item.oi_change >= 0,
                item.wick_ratio <= 0.55,
            ]
        else:
            return 0
        return sum(1 for ok in checks if ok)

    def _worst_symbol_sides(self, rows: list[dict]) -> list[dict[str, Any]]:
        buckets: dict[tuple[str, str], list[dict]] = {}
        for row in rows:
            key = (str(row.get("symbol") or ""), str(row.get("side") or ""))
            buckets.setdefault(key, []).append(row)
        out = []
        for (symbol, side), bucket in buckets.items():
            if not symbol or not side:
                continue
            stats = self._stats(bucket)
            if stats["count"] >= 2 and stats["pnl"] < 0:
                out.append({"symbol": symbol, "side": side, **stats})
        return sorted(out, key=lambda x: x["pnl"])[:10]


performance_guard = PerformanceGuard()
