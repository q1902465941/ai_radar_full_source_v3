from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from backend.config import settings
from backend.learning.event_calibrator import event_calibrator
from backend.learning.learning_data_audit import learning_data_audit
from backend.learning.strategy_filter import direction_confirmations
from backend.learning.trade_attributor import trade_attributor
from backend.models import RadarItem

try:
    import pandas as pd
    from cyqnt_trd.blocks.scoring import weighted_composite
    from cyqnt_trd.blocks.verdicts import normalize_score

    CYQNT_AVAILABLE = True
except Exception:
    pd = None
    weighted_composite = None
    normalize_score = None
    CYQNT_AVAILABLE = False


@dataclass
class CandidateFeatureReport:
    symbol: str
    side: str
    cyqnt_available: bool
    feature_score: float
    estimated_win_rate: float
    selection_score: float
    attribution_samples: int
    attribution_win_rate: float
    attribution_profit_factor: float
    event_samples: int
    event_win_rate: float
    event_profit_factor: float
    reasons: list[str]
    contributions: dict[str, float]
    positive_factors: list[str]
    failure_risks: list[str]

    def asdict(self) -> dict[str, Any]:
        return asdict(self)


class CandidateFeatureEnhancer:
    def evaluate(self, item: RadarItem) -> CandidateFeatureReport:
        contributions = self._contributions(item)
        feature_score = self._feature_score(contributions)
        feature_win_rate = self._feature_win_rate(item, feature_score)
        attr = trade_attributor.evaluate(item, None)
        event = self._event_context(item)
        learning_quality = learning_data_audit.summary()
        historical_trusted = bool(learning_quality.get("can_hard_block_from_learning"))

        estimated = feature_win_rate
        reasons = self._feature_reasons(item, contributions, feature_score)

        attr_samples = int(getattr(attr, "matched_samples", 0) or 0)
        attr_win = float(getattr(attr, "win_rate", 0.0) or 0.0)
        attr_pf = float(getattr(attr, "profit_factor", 0.0) or 0.0)
        attr_min = max(1, int(settings.trade_attribution_min_samples or 1))
        if historical_trusted and attr_samples >= attr_min and attr_win > 0:
            confidence = min(0.65, attr_samples / max(attr_min * 3.0, 1.0))
            estimated = estimated * (1.0 - confidence) + attr_win * confidence
            reasons.append("attribution_win_rate_blended")
        elif attr_samples >= attr_min and attr_win > 0:
            reasons.append("attribution_low_trust_not_blended")
        else:
            reasons.append("attribution_samples_low")

        event_samples = int(event.get("samples") or 0)
        event_win = float(event.get("win_rate") or 0.0)
        event_pf = float(event.get("profit_factor") or 0.0)
        event_min = max(1, int(settings.event_calibration_min_samples or 1))
        if historical_trusted and event_samples >= event_min and event_win > 0:
            confidence = min(0.45, event_samples / max(event_min * 4.0, 1.0))
            estimated = estimated * (1.0 - confidence) + event_win * confidence
            reasons.append("event_win_rate_blended")
        elif event_samples >= event_min and event_win > 0:
            reasons.append("event_low_trust_not_blended")
        else:
            reasons.append("event_samples_low")

        historical_negative = self._historical_negative(attr_samples, attr_win, attr_pf, event_samples, event_win, event_pf)
        if historical_negative:
            cap = self._historical_negative_win_rate_cap(attr_samples, attr_win, event_samples, event_win)
            if cap > 0 and estimated > cap:
                estimated = cap
                reasons.append("historical_negative_estimate_capped")

        current_floor = self._current_signal_floor(item, feature_score, contributions)
        if current_floor > 0 and estimated < current_floor:
            if self._historical_hard_block(attr_samples, attr_win, attr_pf, event_samples, event_win, event_pf):
                reasons.append("historical_hard_block_kept")
            elif historical_negative:
                reasons.append("historical_negative_floor_blocked")
            else:
                estimated = current_floor
                reasons.append("current_feature_floor_applied")

        estimated = _clamp(estimated, 0.05, 0.82)
        selection_score = self._selection_score(
            item,
            feature_score,
            estimated,
            attr_samples,
            attr_pf,
            event_samples,
            event_pf,
            historical_trusted=historical_trusted,
        )
        positive_factors = self._positive_factors(item, contributions, feature_score, estimated)
        failure_risks = self._failure_risks(
            item,
            contributions,
            feature_score,
            estimated,
            attr_samples,
            attr_win,
            attr_pf,
            event_samples,
            event_win,
            event_pf,
        )
        return CandidateFeatureReport(
            symbol=item.symbol,
            side=item.direction,
            cyqnt_available=CYQNT_AVAILABLE,
            feature_score=round(feature_score, 4),
            estimated_win_rate=round(estimated, 4),
            selection_score=round(selection_score, 4),
            attribution_samples=attr_samples,
            attribution_win_rate=round(attr_win, 4),
            attribution_profit_factor=round(attr_pf, 4),
            event_samples=event_samples,
            event_win_rate=round(event_win, 4),
            event_profit_factor=round(event_pf, 4),
            reasons=reasons[:14],
            contributions={key: round(value, 4) for key, value in contributions.items()},
            positive_factors=positive_factors[:10],
            failure_risks=failure_risks[:10],
        )

    def rank_key(self, item: RadarItem) -> tuple:
        report = self.evaluate(item)
        return (
            report.estimated_win_rate,
            report.selection_score,
            report.feature_score,
            float(getattr(item, "score", 0.0) or 0.0),
            -int(getattr(item, "rank", 999) or 999),
        )

    def _feature_score(self, contributions: dict[str, float]) -> float:
        if CYQNT_AVAILABLE and pd is not None and weighted_composite is not None and normalize_score is not None:
            signals = {key: pd.Series([value]) for key, value in contributions.items()}
            weights = {
                "trend": 1.25,
                "flow": 1.35,
                "structure": 1.10,
                "liquidity": 0.95,
                "noise": 1.20,
                "funding": 0.75,
            }
            raw = float(weighted_composite(signals, weights).iloc[-1])
            return float(normalize_score(raw, min_val=-24.0, max_val=24.0))
        raw = sum(contributions.values())
        return _normalize(raw, -24.0, 24.0)

    def _contributions(self, item: RadarItem) -> dict[str, float]:
        side = item.direction
        trend = (
            _aligned_pct(item.change_5m, side) * 1.8
            + _aligned_pct(item.change_15m, side) * 1.2
            + _aligned_pct(item.change_1h, side) * 0.8
        )
        taker_edge = (
            float(item.taker_buy_ratio or 0.5) - float(item.taker_sell_ratio or 0.5)
            if side == "LONG"
            else float(item.taker_sell_ratio or 0.5) - float(item.taker_buy_ratio or 0.5)
        )
        depth_edge = float(item.depth_imbalance or 0.0) if side == "LONG" else -float(item.depth_imbalance or 0.0)
        sm_edge = float(item.sm_delta or 0.0) if side == "LONG" else -float(item.sm_delta or 0.0)
        flow = taker_edge * 10.0 + depth_edge * 12.0 + sm_edge * 4.0 + max(0.0, float(item.oi_change or 0.0)) * 1.2
        confirms = direction_confirmations(item.asdict(), side)
        structure = (confirms - 4) * 2.0 + min(4.0, max(0.0, float(item.slope_score or 0.0) - 50.0) / 10.0)
        liquidity = min(5.0, max(0.0, float(item.volume_spike or 0.0) - 1.0) * 2.2)
        if int(item.fund_confirm_count or 0) >= min(3, int(item.fund_confirm_total or 3)):
            liquidity += 3.0
        elif int(item.fund_confirm_count or 0) >= 2:
            liquidity += 1.4
        noise = {"LOW": 4.0, "MEDIUM": -2.5, "HIGH": -8.0}.get(str(item.fake_breakout_risk or ""), -3.0)
        noise -= max(0.0, _current_wick_ratio(item) - 0.55) * 10.0
        if "trap" in str(item.dealer_radar or "").lower():
            noise -= 4.0
        funding = -min(4.0, abs(float(item.funding_rate or 0.0)) * 900.0)
        return {
            "trend": _clamp(trend, -6.0, 6.0),
            "flow": _clamp(flow, -7.0, 7.0),
            "structure": _clamp(structure, -6.0, 6.0),
            "liquidity": _clamp(liquidity, -2.0, 8.0),
            "noise": _clamp(noise, -8.0, 5.0),
            "funding": _clamp(funding, -4.0, 1.0),
        }

    def _feature_win_rate(self, item: RadarItem, feature_score: float) -> float:
        p = 0.34 + (feature_score / 100.0) * 0.32
        if int(item.fund_confirm_count or 0) >= min(3, int(item.fund_confirm_total or 3)):
            p += 0.04
        if item.fake_breakout_risk == "LOW":
            p += 0.025
        elif item.fake_breakout_risk == "MEDIUM":
            p -= 0.025
        if _current_wick_ratio(item) > 0.75:
            p -= 0.035
        return _clamp(p, 0.30, 0.74)

    def _current_signal_floor(self, item: RadarItem, feature_score: float, contributions: dict[str, float]) -> float:
        if item.direction not in {"LONG", "SHORT"}:
            return 0.0
        confirms = direction_confirmations(item.asdict(), item.direction)
        fund = int(item.fund_confirm_count or 0)
        if (
            fund >= min(3, int(item.fund_confirm_total or 3))
            and item.fake_breakout_risk == "LOW"
            and confirms >= 5
            and feature_score >= 85.0
            and _current_wick_ratio(item) <= 0.55
        ):
            positive_blocks = sum(1 for key in ("trend", "flow", "structure", "liquidity") if contributions.get(key, 0.0) > 2.0)
            if positive_blocks >= 3:
                return max(0.54, min(0.58, float(settings.strategy_min_paper_win_rate or 0.56)))
        if (
            fund >= min(2, int(item.fund_confirm_total or 3))
            and item.fake_breakout_risk == "LOW"
            and confirms >= 5
            and feature_score >= 90.0
            and _current_wick_ratio(item) <= 0.55
        ):
            return 0.54
        return 0.0

    def _historical_hard_block(
        self,
        attr_samples: int,
        attr_win: float,
        attr_pf: float,
        event_samples: int,
        event_win: float,
        event_pf: float,
    ) -> bool:
        if not bool(learning_data_audit.summary().get("can_hard_block_from_learning")):
            return False
        return self._historical_negative(attr_samples, attr_win, attr_pf, event_samples, event_win, event_pf)

    def _historical_negative(
        self,
        attr_samples: int,
        attr_win: float,
        attr_pf: float,
        event_samples: int,
        event_win: float,
        event_pf: float,
    ) -> bool:
        attr_min = max(1, int(settings.trade_attribution_min_samples or 1))
        event_min = max(1, int(settings.event_calibration_min_samples or 1))
        block_win = float(settings.trade_attribution_block_win_rate or 0.42)
        block_pf = float(settings.trade_attribution_block_profit_factor or 0.85)
        if attr_samples >= attr_min and attr_win < block_win and attr_pf < block_pf:
            return True
        if event_samples >= event_min and event_win < block_win and event_pf < block_pf:
            return True
        return False

    def _historical_negative_win_rate_cap(
        self,
        attr_samples: int,
        attr_win: float,
        event_samples: int,
        event_win: float,
    ) -> float:
        observed: list[float] = []
        attr_min = max(1, int(settings.trade_attribution_min_samples or 1))
        event_min = max(1, int(settings.event_calibration_min_samples or 1))
        if attr_samples >= attr_min and attr_win > 0:
            observed.append(attr_win)
        if event_samples >= event_min and event_win > 0:
            observed.append(event_win)
        if not observed:
            return 0.0
        return _clamp(min(observed) + 0.08, 0.05, float(settings.strategy_min_paper_win_rate or 0.56) - 0.01)

    def _selection_score(
        self,
        item: RadarItem,
        feature_score: float,
        estimated_win_rate: float,
        attr_samples: int,
        attr_pf: float,
        event_samples: int,
        event_pf: float,
        *,
        historical_trusted: bool = False,
    ) -> float:
        sample_bonus = min(8.0, (attr_samples + event_samples) * 0.25) if historical_trusted else 0.0
        pf_bonus = (
            min(8.0, max(0.0, attr_pf - 1.0) * 3.0 + max(0.0, event_pf - 1.0) * 2.0)
            if historical_trusted
            else 0.0
        )
        raw_score = min(100.0, max(0.0, float(item.score or 0.0)))
        return feature_score * 0.45 + estimated_win_rate * 100.0 * 0.40 + raw_score * 0.15 + sample_bonus + pf_bonus

    def _event_context(self, item: RadarItem) -> dict[str, Any]:
        try:
            return (event_calibrator.compact_context(item) or {}).get("similar_current_event") or {}
        except Exception:
            return {}

    def _feature_reasons(self, item: RadarItem, contributions: dict[str, float], feature_score: float) -> list[str]:
        reasons: list[str] = []
        if CYQNT_AVAILABLE:
            reasons.append("cyqnt_weighted_composite")
        else:
            reasons.append("cyqnt_unavailable_fallback")
        if feature_score >= 68:
            reasons.append("feature_score_strong")
        elif feature_score < 48:
            reasons.append("feature_score_weak")
        for key, value in contributions.items():
            if value >= 3.0:
                reasons.append(f"{key}_positive")
            elif value <= -3.0:
                reasons.append(f"{key}_negative")
        return reasons

    def _positive_factors(
        self,
        item: RadarItem,
        contributions: dict[str, float],
        feature_score: float,
        estimated_win_rate: float,
    ) -> list[str]:
        factors: list[str] = []
        if feature_score >= 68:
            factors.append("feature_score_strong")
        if estimated_win_rate >= float(settings.strategy_min_paper_win_rate or 0.56):
            factors.append("estimated_win_rate_above_paper_gate")
        if int(item.fund_confirm_count or 0) >= min(3, int(item.fund_confirm_total or 3)):
            factors.append("fund_confirm_full")
        elif int(item.fund_confirm_count or 0) >= min(2, int(item.fund_confirm_total or 3)):
            factors.append("fund_confirm_partial")
        if item.fake_breakout_risk == "LOW":
            factors.append("fake_breakout_risk_low")
        for key in ("trend", "flow", "structure", "liquidity", "noise"):
            if contributions.get(key, 0.0) >= 3.0:
                factors.append(f"{key}_positive")
        if abs(float(item.funding_rate or 0.0)) <= 0.0015:
            factors.append("funding_not_extreme")
        return factors

    def _failure_risks(
        self,
        item: RadarItem,
        contributions: dict[str, float],
        feature_score: float,
        estimated_win_rate: float,
        attr_samples: int,
        attr_win: float,
        attr_pf: float,
        event_samples: int,
        event_win: float,
        event_pf: float,
    ) -> list[str]:
        risks: list[str] = []
        if feature_score < 48:
            risks.append("feature_score_weak")
        if estimated_win_rate < float(settings.strategy_min_paper_win_rate or 0.56):
            risks.append("estimated_win_rate_below_paper_gate")
        if int(item.fund_confirm_count or 0) < min(2, int(item.fund_confirm_total or 3)):
            risks.append("fund_confirm_too_low_for_training")
        if item.fake_breakout_risk == "HIGH":
            risks.append("fake_breakout_risk_high")
        elif item.fake_breakout_risk == "MEDIUM":
            risks.append("fake_breakout_risk_medium")
        if _current_wick_ratio(item) > float(settings.paper_probe_max_wick_ratio or 0.55):
            risks.append("wick_above_paper_noise_budget")
        for key in ("trend", "flow", "structure", "liquidity", "noise", "funding"):
            if contributions.get(key, 0.0) <= -3.0:
                risks.append(f"{key}_negative")
        attr_min = max(1, int(settings.trade_attribution_min_samples or 1))
        if attr_samples >= attr_min and attr_win < float(settings.trade_attribution_block_win_rate or 0.42):
            risks.append("attribution_win_rate_low")
        if attr_samples >= attr_min and attr_pf < float(settings.trade_attribution_block_profit_factor or 0.85):
            risks.append("attribution_profit_factor_low")
        event_min = max(1, int(settings.event_calibration_min_samples or 1))
        if event_samples >= event_min and event_win < float(settings.event_calibration_min_win_rate or 0.60):
            risks.append("event_win_rate_low")
        if event_samples >= event_min and event_pf < float(settings.event_calibration_min_profit_factor or 1.20):
            risks.append("event_profit_factor_low")
        return risks


def _aligned_pct(value: float, side: str) -> float:
    raw = float(value or 0.0)
    return raw if side == "LONG" else -raw


def _normalize(value: float, min_val: float, max_val: float) -> float:
    if max_val <= min_val:
        return 0.0
    return _clamp((value - min_val) / (max_val - min_val) * 100.0, 0.0, 100.0)


def _clamp(value: float, low: float, high: float) -> float:
    return min(high, max(low, float(value)))


def _current_wick_ratio(item: RadarItem) -> float:
    features = item.score_features if isinstance(item.score_features, dict) else {}
    metrics = features.get("structure_metrics") if isinstance(features, dict) else {}
    if isinstance(metrics, dict) and "current_wick_ratio" in metrics:
        try:
            return max(0.0, float(metrics.get("current_wick_ratio") or 0.0))
        except (TypeError, ValueError):
            pass
    return max(0.0, float(item.wick_ratio or 0.0))


candidate_feature_enhancer = CandidateFeatureEnhancer()
