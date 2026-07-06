from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
from typing import Any

from backend.config import settings
from backend.learning.learning_data_audit import learning_data_audit
from backend.learning.trade_attributor import trade_attributor
from backend.models import RadarItem, StrategyPlan


@dataclass
class LearnedRiskReport:
    enabled: bool
    allow_paper: bool
    allow_live: bool
    severity: str
    reasons: list[str]
    advice: list[str]
    current_factors: list[str] = field(default_factory=list)
    matched_samples: int = 0
    match_level: str = ""
    win_rate: float = 0.0
    profit_factor: float = 0.0
    pnl: float = 0.0
    hard_blocks: list[dict[str, Any]] = field(default_factory=list)
    attribution: dict[str, Any] = field(default_factory=dict)
    data_quality: dict[str, Any] = field(default_factory=dict)

    def asdict(self) -> dict[str, Any]:
        return asdict(self)


class LearnedRiskGuard:
    """Turns historical loss attribution into a pre-trade execution guard."""

    def evaluate(
        self,
        item: RadarItem,
        plan: StrategyPlan | None = None,
        *,
        recovery_mode: bool = False,
    ) -> LearnedRiskReport:
        if not settings.trade_learning_guard_enabled or not settings.trade_attribution_enabled:
            return LearnedRiskReport(
                enabled=False,
                allow_paper=True,
                allow_live=False,
                severity="DISABLED",
                reasons=["trade_learning_guard_disabled"],
                advice=[],
            )

        attribution = trade_attributor.evaluate(item, plan)
        current_factors = set(attribution.current_factors or [])
        reasons = list(attribution.reasons or [])
        advice = list(attribution.advice or [])
        data_quality = learning_data_audit.compact()
        low_trust_learning = not bool(data_quality.get("can_hard_block_from_learning"))
        positive_match = self._positive_matched_attribution(attribution)
        if low_trust_learning:
            reasons.append("learning_data_not_production_grade")
            hard_blocks = []
            advice.append("Learning evidence is not production-grade; keep negative matched patterns as review evidence, not as a paper-sampling hard block.")
        elif positive_match:
            reasons = [reason for reason in reasons if not reason.startswith("causal_factor_negative:")]
            hard_blocks = []
            advice.append("positive matched attribution overrides factor-level learned blocks for paper execution.")
        else:
            hard_blocks = self._hard_blocks(current_factors)

        for block in hard_blocks:
            reasons.append(f"learned_block:{block['code']}")
            if block.get("advice"):
                advice.append(str(block["advice"]))

        blocking_reasons = [] if low_trust_learning else [
            reason
            for reason in reasons
            if reason != "trade_attribution_samples_low"
            and (
                reason.startswith("learned_block:")
                or reason.startswith("causal_pattern_")
                or reason.startswith("causal_factor_negative:")
            )
        ]
        strict = bool(recovery_mode and settings.trade_learning_guard_recovery_strict)
        allow_paper = True if low_trust_learning else (not blocking_reasons and bool(attribution.paper_ok))
        allow_live = False if low_trust_learning else (allow_paper and attribution.live_ok and not hard_blocks)
        severity = "PASS"
        if blocking_reasons:
            severity = "BLOCK"
        elif low_trust_learning:
            severity = "REVIEW"
        elif strict and attribution.matched_samples < int(settings.trade_attribution_min_samples):
            severity = "REVIEW"

        return LearnedRiskReport(
            enabled=True,
            allow_paper=allow_paper,
            allow_live=allow_live,
            severity=severity,
            reasons=_unique(reasons)[:10],
            advice=_unique(advice)[:8],
            current_factors=attribution.current_factors,
            matched_samples=attribution.matched_samples,
            match_level=attribution.match_level,
            win_rate=attribution.win_rate,
            profit_factor=attribution.profit_factor,
            pnl=attribution.pnl,
            hard_blocks=hard_blocks,
            attribution=attribution.asdict(),
            data_quality=data_quality,
        )

    def _positive_matched_attribution(self, attribution: Any) -> bool:
        min_samples = max(1, int(settings.trade_attribution_min_samples))
        return (
            int(getattr(attribution, "matched_samples", 0) or 0) >= min_samples
            and float(getattr(attribution, "pnl", 0.0) or 0.0) > 0
            and float(getattr(attribution, "win_rate", 0.0) or 0.0) >= float(settings.trade_attribution_block_win_rate)
            and float(getattr(attribution, "profit_factor", 0.0) or 0.0) >= float(settings.trade_attribution_block_profit_factor)
        )

    def precheck_item(
        self,
        item: RadarItem,
        *,
        recovery_mode: bool = False,
    ) -> tuple[bool, LearnedRiskReport]:
        report = self.evaluate(item, None, recovery_mode=recovery_mode)
        return report.allow_paper, report

    def reverse_opportunity(
        self,
        item: RadarItem,
        *,
        recovery_mode: bool = False,
    ) -> dict[str, Any]:
        original = self.evaluate(item, None, recovery_mode=recovery_mode)
        opposite = self._opposite_item(item)
        if opposite is None:
            return {
                "enabled": settings.trade_learning_reverse_enabled,
                "allow_reverse": False,
                "reason": "no_opposite_side",
                "original": original.asdict(),
            }

        reverse = self.evaluate(opposite, None, recovery_mode=recovery_mode)
        confirmations = _direction_confirmations(opposite, opposite.direction)
        min_confirmations = max(1, int(settings.trade_learning_reverse_min_confirmations))
        min_samples = max(1, int(settings.trade_attribution_min_samples))
        reasons: list[str] = []

        if not settings.trade_learning_reverse_enabled:
            reasons.append("reverse_learning_disabled")
        if original.severity != "BLOCK":
            reasons.append("original_side_not_blocked")
        if opposite.fund_confirm_count < min(3, opposite.fund_confirm_total):
            reasons.append("reverse_fund_confirm_not_full")
        if confirmations < min_confirmations:
            reasons.append("reverse_direction_confirmation_low")
        if opposite.fake_breakout_risk == "HIGH" or (recovery_mode and opposite.fake_breakout_risk != "LOW"):
            reasons.append("reverse_fake_breakout_not_clean")
        if not reverse.allow_paper:
            reasons.append("reverse_attribution_blocked")
        if reverse.matched_samples < min_samples:
            reasons.append("reverse_samples_low")
        if reverse.win_rate < float(settings.trade_learning_reverse_min_win_rate):
            reasons.append("reverse_win_rate_low")
        if reverse.profit_factor < float(settings.trade_learning_reverse_min_profit_factor):
            reasons.append("reverse_profit_factor_low")
        if reverse.pnl <= 0:
            reasons.append("reverse_pnl_not_positive")

        allow_reverse = not reasons
        return {
            "enabled": settings.trade_learning_reverse_enabled,
            "allow_reverse": allow_reverse,
            "reason": "ok" if allow_reverse else ",".join(reasons[:6]),
            "original": original.asdict(),
            "reverse": reverse.asdict(),
            "reverse_item": opposite.asdict(),
            "reverse_confirmations": confirmations,
            "reverse_fund_confirm": f"{opposite.fund_confirm_count}/{opposite.fund_confirm_total}",
        }

    def maybe_reverse_candidate(
        self,
        item: RadarItem,
        *,
        recovery_mode: bool = False,
    ) -> tuple[RadarItem | None, dict[str, Any]]:
        report = self.reverse_opportunity(item, recovery_mode=recovery_mode)
        if not report.get("allow_reverse"):
            return None, report
        reverse_item = self._opposite_item(item)
        return reverse_item, report

    def _hard_blocks(self, current_factors: set[str]) -> list[dict[str, Any]]:
        min_samples = max(
            int(settings.trade_attribution_min_samples),
            int(settings.trade_learning_guard_min_rule_samples),
        )
        deep = trade_attributor.deep_analysis(trade_limit=1)
        blocks: list[dict[str, Any]] = []
        for cause in deep.get("root_causes") or []:
            code = str(cause.get("code") or "")
            if code not in current_factors:
                continue
            samples = int(cause.get("samples") or 0)
            win_rate = float(cause.get("win_rate") or 0.0)
            profit_factor = float(cause.get("profit_factor") or 0.0)
            pnl = float(cause.get("pnl") or 0.0)
            if samples < min_samples:
                continue
            if pnl < 0 and win_rate < settings.trade_attribution_block_win_rate and profit_factor < settings.trade_attribution_block_profit_factor:
                blocks.append(cause)
        return blocks[:5]

    def _opposite_item(self, item: RadarItem) -> RadarItem | None:
        opposite = _opposite_side(item.direction)
        if opposite not in {"LONG", "SHORT"}:
            return None
        fund_count, fund_total = _fund_confirm_for(item, opposite)
        fake_risk = _fake_breakout_for(item, opposite)
        return replace(
            item,
            direction=opposite,
            fund_confirm_count=fund_count,
            fund_confirm_total=fund_total,
            fake_breakout_risk=fake_risk,
            ai_candidate=False,
        )


def _unique(values: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        out.append(value)
    return out


def _opposite_side(side: str) -> str:
    if side == "LONG":
        return "SHORT"
    if side == "SHORT":
        return "LONG"
    return "NEUTRAL"


def _fund_confirm_for(item: RadarItem, side: str) -> tuple[int, int]:
    count = 0
    if float(item.volume_spike or 0.0) >= 1.8:
        count += 1
    if side == "LONG":
        if float(item.change_5m or 0.0) > 0 and float(item.oi_change or 0.0) > 0:
            count += 1
        if float(item.taker_buy_ratio or 0.5) > 0.55:
            count += 1
    elif side == "SHORT":
        if float(item.change_5m or 0.0) < 0 and float(item.oi_change or 0.0) > 0:
            count += 1
        if float(item.taker_sell_ratio or 0.5) > 0.55:
            count += 1
    return count, 3


def _fake_breakout_for(item: RadarItem, side: str) -> str:
    breakout = abs(float(item.change_5m or 0.0)) > 0.7 or abs(float(item.change_15m or 0.0)) > 1.2
    score = 0
    if breakout and float(item.volume_spike or 0.0) < 1.4:
        score += 25
    if breakout and float(item.oi_change or 0.0) <= 0:
        score += 25
    if side == "LONG" and float(item.taker_buy_ratio or 0.5) < 0.50:
        score += 20
    if side == "SHORT" and float(item.taker_sell_ratio or 0.5) < 0.50:
        score += 20
    if float(item.wick_ratio or 0.0) > 0.45:
        score += 20
    if abs(float(item.funding_rate or 0.0)) > 0.0005:
        score += 10
    if score < 30:
        return "LOW"
    if score < 65:
        return "MEDIUM"
    return "HIGH"


def _direction_confirmations(item: RadarItem, side: str) -> int:
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


learned_risk_guard = LearnedRiskGuard()
