from __future__ import annotations

from dataclasses import asdict, dataclass, field

from backend.config import settings
from backend.learning.event_calibrator import event_calibrator
from backend.learning.strategy_filter import direction_confirmations
from backend.learning.trade_attributor import trade_attributor
from backend.models import RadarItem, StrategyPlan


@dataclass
class StrategyQualityReport:
    estimated_win_rate: float
    expected_r: float
    tp1_r: float
    tp2_r: float
    avg_reward_r: float
    cost_r: float
    paper_ok: bool
    live_ok: bool
    reasons: list[str]
    calibrated_win_rate: float = 0.0
    calibration: dict = field(default_factory=dict)
    attribution: dict = field(default_factory=dict)
    geometry_sample: dict = field(default_factory=dict)

    def asdict(self) -> dict:
        return asdict(self)


class StrategyQualityGate:
    def evaluate(self, item: RadarItem, plan: StrategyPlan) -> StrategyQualityReport:
        if plan.action in {"WAIT", "PAPER_OBSERVE"}:
            return StrategyQualityReport(0.0, 0.0, 0.0, 0.0, 0.0, 0.0, True, False, ["wait_plan"])

        tp1_r, tp2_r = self._reward_risk(plan)
        cost_r = self._cost_r(plan)
        avg_reward_r = max(0.0, tp1_r * 0.45 + tp2_r * 0.55 - cost_r)
        win_rate, reasons = self._estimate_win_rate(item, plan)
        calibration = event_calibrator.evaluate(item, plan, heuristic_win_rate=win_rate)
        if settings.event_calibration_enabled and calibration.matched_samples >= settings.event_calibration_min_samples:
            win_rate = calibration.adjusted_win_rate
        attribution = trade_attributor.evaluate(item, plan)
        geometry_sample = self._geometry_sample(plan)
        geometry_reasons = self._geometry_reasons(plan, geometry_sample)
        win_rate = self._blend_geometry_win_rate(win_rate, geometry_sample)
        expected_r = win_rate * avg_reward_r - (1.0 - win_rate)
        expected_r = self._blend_geometry_expected_r(expected_r, geometry_sample)

        min_paper_confidence = float(settings.strategy_min_paper_confidence)
        if plan.confidence < min_paper_confidence:
            reasons.append("confidence_low")
        full_fund_confirm = item.fund_confirm_count >= min(3, item.fund_confirm_total)
        if not full_fund_confirm:
            reasons.append("fund_confirm_3_required")
        if item.fake_breakout_risk == "HIGH":
            reasons.append("fake_breakout_high")
        elif item.fake_breakout_risk != "LOW":
            reasons.append("fake_breakout_not_low")
        wick_report = self._paper_wick_quality(item, plan)
        wick_ok = bool(wick_report["paper_ok"])
        live_wick_ok = bool(wick_report["live_ok"])
        if not wick_ok:
            reasons.append("wick_above_quality_budget")
            reasons.extend(wick_report["reasons"])
        if item.direction != "NEUTRAL" and plan.side != item.direction:
            reasons.append("side_not_aligned_with_radar")
        if tp1_r < 0.75:
            reasons.append("tp1_r_too_low")
        if tp2_r < settings.strategy_min_tp2_r:
            reasons.append("tp2_r_too_low")
        if cost_r > 0.35:
            reasons.append("round_trip_cost_drag_high")
        if expected_r < settings.strategy_min_expected_r:
            reasons.append("expected_r_low")
        reasons.extend(calibration.reasons)
        reasons.extend(attribution.reasons)
        reasons.extend(geometry_reasons)
        geometry_blocked = any(reason.startswith("strategy_geometry_") for reason in geometry_reasons)

        paper_ok = (
            win_rate >= settings.strategy_min_paper_win_rate
            and expected_r >= settings.strategy_min_expected_r
            and tp2_r >= settings.strategy_min_tp2_r
            and cost_r <= 0.45
            and plan.confidence >= min_paper_confidence
            and item.fake_breakout_risk == "LOW"
            and wick_ok
            and full_fund_confirm
            and (item.direction == "NEUTRAL" or plan.side == item.direction)
            and calibration.paper_ok
            and attribution.paper_ok
            and not geometry_blocked
        )
        live_ok = (
            paper_ok
            and win_rate >= settings.strategy_min_live_win_rate
            and expected_r >= settings.strategy_min_live_expected_r
            and full_fund_confirm
            and item.fake_breakout_risk == "LOW"
            and live_wick_ok
            and item.score >= 62
            and plan.confidence >= 68
            and cost_r <= 0.30
            and (calibration.live_ok or not settings.event_calibration_require_for_live)
            and (attribution.live_ok or not settings.trade_attribution_require_for_live)
        )
        return StrategyQualityReport(
            estimated_win_rate=round(win_rate, 4),
            expected_r=round(expected_r, 4),
            tp1_r=round(tp1_r, 4),
            tp2_r=round(tp2_r, 4),
            avg_reward_r=round(avg_reward_r, 4),
            cost_r=round(cost_r, 4),
            paper_ok=paper_ok,
            live_ok=live_ok,
            reasons=reasons,
            calibrated_win_rate=round(calibration.adjusted_win_rate, 4),
            calibration=calibration.asdict(),
            attribution=attribution.asdict(),
            geometry_sample=geometry_sample,
        )

    def _paper_wick_quality(self, item: RadarItem, plan: StrategyPlan) -> dict:
        wick_budget = min(0.55, max(0.0, self._float_value(settings.paper_probe_max_wick_ratio, 0.55)))
        metrics = self._structure_metrics(item)
        current_wick = self._float_value(metrics.get("current_wick_ratio"), self._float_value(item.wick_ratio, 0.0))
        recent_max_wick = self._float_value(metrics.get("max_wick_ratio_14"), self._float_value(item.wick_ratio, 0.0))
        avg_wick = self._float_value(metrics.get("avg_wick_ratio_14"), recent_max_wick)
        bars_since_max = self._int_value(metrics.get("bars_since_max_wick"), 0)
        balanced_current_limit = min(0.75, max(0.65, wick_budget + 0.10))
        avg_limit = max(0.65, wick_budget + 0.10)
        live_ok = current_wick <= wick_budget and recent_max_wick <= wick_budget
        if current_wick >= 0.88:
            return {"paper_ok": False, "live_ok": False, "reasons": ["current_wick_extreme"]}
        if current_wick > balanced_current_limit:
            return {"paper_ok": False, "live_ok": False, "reasons": ["current_wick_above_balance_limit"]}
        if recent_max_wick <= wick_budget:
            return {"paper_ok": True, "live_ok": live_ok, "reasons": []}

        reasons: list[str] = []
        if item.fake_breakout_risk != "LOW":
            reasons.append("balanced_noise_requires_low_fake_risk")
        if item.fund_confirm_count < min(3, item.fund_confirm_total):
            reasons.append("balanced_noise_requires_full_fund_confirm")
        if direction_confirmations(item.asdict(), plan.side) < 5:
            reasons.append("balanced_noise_requires_direction_confirmations_5")
        if float(item.score or 0.0) < 85.0:
            reasons.append("balanced_noise_requires_score_85")
        if recent_max_wick > 0.85 and bars_since_max < 3:
            reasons.append("recent_wick_spike_unresolved")
        if avg_wick > avg_limit:
            reasons.append("average_wick_noise_high")
        return {"paper_ok": not reasons, "live_ok": False, "reasons": reasons}

    def _structure_metrics(self, item: RadarItem) -> dict:
        features = item.score_features if isinstance(item.score_features, dict) else {}
        metrics = features.get("structure_metrics") if isinstance(features, dict) else {}
        return metrics if isinstance(metrics, dict) else {}

    def _float_value(self, value, default: float = 0.0) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    def _int_value(self, value, default: int = 0) -> int:
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return default

    def _reward_risk(self, plan: StrategyPlan) -> tuple[float, float]:
        entry = plan.ideal_entry_price
        if entry <= 0:
            return 0.0, 0.0
        if plan.side == "LONG":
            risk = entry - plan.stop_loss
            return _ratio(plan.tp1 - entry, risk), _ratio(plan.tp2 - entry, risk)
        if plan.side == "SHORT":
            risk = plan.stop_loss - entry
            return _ratio(entry - plan.tp1, risk), _ratio(entry - plan.tp2, risk)
        return 0.0, 0.0

    def _cost_r(self, plan: StrategyPlan) -> float:
        entry = float(plan.ideal_entry_price or 0.0)
        if entry <= 0:
            return 99.0
        risk_pct = abs(entry - float(plan.stop_loss or 0.0)) / entry
        if risk_pct <= 0:
            return 99.0
        round_trip_cost_pct = 2.0 * max(0.0, float(settings.paper_taker_fee_rate)) + 2.0 * max(0.0, float(settings.paper_slippage_pct))
        return round_trip_cost_pct / risk_pct

    def _estimate_win_rate(self, item: RadarItem, plan: StrategyPlan) -> tuple[float, list[str]]:
        p = 0.38
        reasons: list[str] = []

        if item.score >= 70:
            p += 0.10; reasons.append("score_strong")
        elif item.score >= 62:
            p += 0.07; reasons.append("score_good")
        elif item.score >= 55:
            p += 0.04; reasons.append("score_ok")
        else:
            p -= 0.05; reasons.append("score_low")

        p += {3: 0.09, 2: 0.04, 1: -0.04, 0: -0.10}.get(min(3, int(item.fund_confirm_count or 0)), -0.08)
        p += {"LOW": 0.05, "MEDIUM": -0.10, "HIGH": -0.20}.get(item.fake_breakout_risk, -0.10)

        p += self._direction_alignment_delta(item, plan)
        if item.slope_score >= 80:
            p += 0.05; reasons.append("heat_accelerating")
        elif item.slope_score < 20:
            p -= 0.03; reasons.append("heat_flat")

        if item.volume_spike >= 1.8:
            p += 0.04; reasons.append("volume_confirmed")
        elif item.volume_spike < 0.7:
            p -= 0.04; reasons.append("volume_weak")

        dealer_signal = _dealer_signal(item.dealer_radar)
        if dealer_signal == "extension":
            p += 0.04; reasons.append("dealer_extension")
        if dealer_signal == "trap":
            p -= 0.08; reasons.append("dealer_trap")

        if item.wick_ratio > 0.55:
            p -= 0.05; reasons.append("wick_risk")
        if abs(item.funding_rate) > 0.003:
            p -= 0.03; reasons.append("funding_extreme")

        if plan.confidence >= 72:
            p += 0.04; reasons.append("ai_confidence_good")
        elif plan.confidence < 58:
            p -= 0.06; reasons.append("ai_confidence_low")

        return min(0.78, max(0.05, p)), reasons

    def _geometry_sample(self, plan: StrategyPlan) -> dict:
        sample = plan.raw.get("strategy_geometry_sample") if isinstance(plan.raw, dict) else {}
        return sample if isinstance(sample, dict) else {}

    def _geometry_reasons(self, plan: StrategyPlan, sample: dict) -> list[str]:
        if not bool(plan.raw.get("strategy_geometry_sample_required")):
            return []
        if plan.action not in {"OPEN_LONG", "OPEN_SHORT"}:
            return []
        reasons: list[str] = []
        status = str(sample.get("status") or "")
        samples = sample.get("samples") if isinstance(sample.get("samples"), dict) else {}
        selected = sample.get("selected_geometry") if isinstance(sample.get("selected_geometry"), dict) else {}
        if status != "ok":
            reasons.append("strategy_geometry_sample_not_ok")
        if int(samples.get("sample_count") or 0) < 60:
            reasons.append("strategy_geometry_sample_count_low")
        if float(samples.get("win_rate") or 0.0) < float(settings.strategy_min_paper_win_rate):
            reasons.append("strategy_geometry_win_rate_low")
        if float(samples.get("expected_r") or 0.0) < float(settings.strategy_min_expected_r):
            reasons.append("strategy_geometry_expected_r_low")
        if float(samples.get("profit_factor") or 0.0) < 1.15:
            reasons.append("strategy_geometry_profit_factor_low")
        selected_side = str(selected.get("side") or "")
        if selected_side in {"LONG", "SHORT"} and plan.side != selected_side:
            reasons.append("strategy_geometry_side_mismatch")
        return list(dict.fromkeys(reasons))

    def _blend_geometry_win_rate(self, win_rate: float, sample: dict) -> float:
        samples = sample.get("samples") if isinstance(sample.get("samples"), dict) else {}
        if sample.get("status") != "ok":
            return win_rate
        sample_count = int(samples.get("sample_count") or 0)
        geo_win = float(samples.get("win_rate") or 0.0)
        if sample_count < 60 or geo_win <= 0:
            return win_rate
        confidence = min(0.65, sample_count / 240.0)
        return win_rate * (1.0 - confidence) + geo_win * confidence

    def _blend_geometry_expected_r(self, expected_r: float, sample: dict) -> float:
        samples = sample.get("samples") if isinstance(sample.get("samples"), dict) else {}
        if sample.get("status") != "ok":
            return expected_r
        sample_count = int(samples.get("sample_count") or 0)
        geo_ev = float(samples.get("expected_r") or 0.0)
        if sample_count < 60:
            return expected_r
        confidence = min(0.65, sample_count / 240.0)
        return expected_r * (1.0 - confidence) + geo_ev * confidence

    def _direction_alignment_delta(self, item: RadarItem, plan: StrategyPlan) -> float:
        if plan.side == "LONG":
            confirmations = [
                item.change_5m > 0,
                item.change_15m > 0,
                item.change_1h >= 0,
                item.taker_buy_ratio > 0.55,
                item.depth_imbalance > 0.10,
                item.sm_delta >= 0,
            ]
        elif plan.side == "SHORT":
            confirmations = [
                item.change_5m < 0,
                item.change_15m < 0,
                item.change_1h <= 0,
                item.taker_sell_ratio > 0.55,
                item.depth_imbalance < -0.10,
                item.sm_delta <= 0,
            ]
        else:
            return -0.12
        positives = sum(1 for ok in confirmations if ok)
        return (positives - 3) * 0.025


def _ratio(reward: float, risk: float) -> float:
    if risk <= 0:
        return 0.0
    return max(0.0, reward / risk)


def _dealer_signal(label: str) -> str:
    text = str(label or "").lower()
    extension_tokens = ("多延", "空延", "long_extend", "short_extend", "extend", "澶氬欢", "绌哄欢")
    trap_tokens = ("多诱", "空诱", "trap", "诱", "澶氳", "绌鸿")
    if any(token.lower() in text for token in trap_tokens):
        return "trap"
    if any(token.lower() in text for token in extension_tokens):
        return "extension"
    return ""


strategy_quality_gate = StrategyQualityGate()
