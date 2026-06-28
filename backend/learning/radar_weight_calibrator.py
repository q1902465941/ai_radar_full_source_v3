from __future__ import annotations

import time
from typing import Any

from backend.config import settings
from backend.learning.replay_memory import replay_memory
from backend.learning.trade_memory import trade_memory
from backend.radar.score_engine import SCORE_WEIGHTS, clamp, norm_abs


FAKE_RISK_TO_SCORE = {
    "LOW": 15.0,
    "MEDIUM": 45.0,
    "HIGH": 85.0,
}


class RadarWeightCalibrator:
    def __init__(self) -> None:
        self._cache_until = 0.0
        self._cache_limit = 0
        self._cache_report: dict[str, Any] | None = None

    def clear_cache(self) -> None:
        self._cache_until = 0.0
        self._cache_limit = 0
        self._cache_report = None

    def weights(self, *, force: bool = False, limit: int | None = None) -> dict[str, float]:
        return dict(self.report(force=force, limit=limit).get("effective_weights") or SCORE_WEIGHTS)

    def compact_context(self, report: dict[str, Any] | None = None) -> dict[str, Any]:
        report = report or self.report()
        return {
            "enabled": bool(report.get("enabled")),
            "active": bool(report.get("active")),
            "reason": report.get("reason"),
            "sample_count": int(report.get("sample_count") or 0),
            "adjusted_features": [row.get("feature") for row in report.get("adjustments", [])[:8]],
            "effective_weights": report.get("effective_weights") or SCORE_WEIGHTS,
        }

    def summary(self) -> dict[str, Any]:
        return self.report()

    def report(self, *, force: bool = False, limit: int | None = None) -> dict[str, Any]:
        limit = max(100, min(20000, int(limit or settings.radar_weight_sample_limit or 5000)))
        now = time.time()
        if (
            not force
            and self._cache_report is not None
            and now < self._cache_until
            and self._cache_limit >= limit
        ):
            return self._cache_report

        report = self._build_report(limit)
        self._cache_report = report
        self._cache_limit = limit
        self._cache_until = now + max(1, int(settings.radar_weight_ttl_seconds or 60))
        return report

    def _build_report(self, limit: int) -> dict[str, Any]:
        default_weights = self._rounded_weights(SCORE_WEIGHTS)
        if not settings.radar_weight_calibration_enabled:
            return self._inactive(
                reason="disabled",
                sample_count=0,
                default_weights=default_weights,
                baseline=self._metrics([]),
            )

        samples = self._samples(limit)
        rows = [self._row(sample) for sample in samples]
        rows = [
            row
            for row in rows
            if row.get("side") in {"LONG", "SHORT"}
            and row.get("features")
            and row.get("pnl") is not None
        ]
        baseline = self._metrics(rows)
        min_samples = max(1, int(settings.radar_weight_min_samples or 200))
        if len(rows) < min_samples:
            return self._inactive(
                reason="sample_count_below_minimum",
                sample_count=len(rows),
                default_weights=default_weights,
                baseline=baseline,
            )

        bucket_min = max(1, int(settings.radar_weight_bucket_min_samples or 30))
        proposed = {key: float(value) for key, value in SCORE_WEIGHTS.items()}
        adjustments: list[dict[str, Any]] = []
        for feature, default_weight in SCORE_WEIGHTS.items():
            feature_rows = [row for row in rows if feature in row["features"]]
            if len(feature_rows) < bucket_min:
                continue
            if default_weight < 0:
                target, adjustment = self._penalty_adjustment(feature, default_weight, feature_rows, baseline)
            else:
                target, adjustment = self._positive_adjustment(feature, default_weight, feature_rows, baseline)
            if adjustment:
                proposed[feature] = target
                adjustments.append(adjustment)

        if not adjustments:
            return self._inactive(
                reason="no_stable_weight_adjustments",
                sample_count=len(rows),
                default_weights=default_weights,
                baseline=baseline,
            )

        calibrated = self._normalize_positive_weights(proposed)
        calibrated = {key: round(float(value), 6) for key, value in calibrated.items()}
        for adjustment in adjustments:
            feature = adjustment["feature"]
            adjustment["default_weight"] = round(float(SCORE_WEIGHTS[feature]), 6)
            adjustment["calibrated_weight"] = calibrated[feature]
            adjustment["multiplier"] = round(
                calibrated[feature] / float(SCORE_WEIGHTS[feature]),
                6,
            )

        return {
            "enabled": True,
            "active": True,
            "reason": "validated_factor_edges",
            "sample_count": len(rows),
            "baseline": baseline,
            "default_weights": default_weights,
            "calibrated_weights": calibrated,
            "effective_weights": calibrated,
            "adjustments": adjustments,
            "method": {
                "min_samples": min_samples,
                "bucket_min_samples": bucket_min,
                "positive_profit_factor": float(settings.radar_weight_positive_profit_factor or 1.05),
                "negative_profit_factor": float(settings.radar_weight_negative_profit_factor or 0.95),
                "max_adjustment_pct": float(settings.radar_weight_max_adjustment_pct or 0.35),
                "positive_weight_sum_preserved": True,
                "live_order_effect": "none; scan scoring only",
            },
        }

    def _inactive(
        self,
        *,
        reason: str,
        sample_count: int,
        default_weights: dict[str, float],
        baseline: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "enabled": bool(settings.radar_weight_calibration_enabled),
            "active": False,
            "reason": reason,
            "sample_count": sample_count,
            "baseline": baseline,
            "default_weights": default_weights,
            "calibrated_weights": default_weights,
            "effective_weights": default_weights,
            "adjustments": [],
            "method": {
                "min_samples": int(settings.radar_weight_min_samples or 200),
                "bucket_min_samples": int(settings.radar_weight_bucket_min_samples or 30),
                "max_adjustment_pct": float(settings.radar_weight_max_adjustment_pct or 0.35),
                "live_order_effect": "none; scan scoring only",
            },
        }

    def _samples(self, limit: int) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        if settings.radar_weight_use_replay and settings.replay_enabled:
            out.extend(replay_memory.samples(limit=limit))
        if settings.radar_weight_use_closed_trades:
            out.extend(trade_memory.samples(limit=limit, require_radar=True))
        return _dedupe_recent(out, limit)

    def _row(self, sample: dict[str, Any]) -> dict[str, Any]:
        radar = sample.get("radar") if isinstance(sample.get("radar"), dict) else {}
        flat = {key: value for key, value in sample.items() if key != "radar"}
        row = {**radar, **flat}
        side = row.get("side") or row.get("direction")
        row["side"] = side
        row["direction"] = side
        row["features"] = self._features(row)
        return row

    def _features(self, row: dict[str, Any]) -> dict[str, float]:
        existing = row.get("score_features")
        if isinstance(existing, dict):
            features = self._clean_features(existing)
            if features:
                return features

        explain = row.get("score_explain")
        if isinstance(explain, dict):
            components = explain.get("components")
            if isinstance(components, dict):
                features = self._clean_features(
                    {
                        key: value.get("raw")
                        for key, value in components.items()
                        if isinstance(value, dict)
                    }
                )
                if features:
                    return features

        return self._fallback_features(row)

    def _clean_features(self, raw: dict[str, Any]) -> dict[str, float]:
        features: dict[str, float] = {}
        for key in SCORE_WEIGHTS:
            if key in raw:
                features[key] = round(clamp(_f(raw.get(key))), 6)
        return features

    def _fallback_features(self, row: dict[str, Any]) -> dict[str, float]:
        change_5m = _f(row.get("change_5m"))
        change_15m = _f(row.get("change_15m"))
        change_1h = _f(row.get("change_1h"))
        history = row.get("score_history") if isinstance(row.get("score_history"), list) else []
        if len(history) >= 2:
            heat_score = clamp((_f(history[-1]) - _f(history[0])) * 2.0)
        else:
            heat_score = clamp(_f(row.get("slope_score"), abs(_f(row.get("heat_slope"))) * 10.0))

        fake_risk = str(row.get("fake_breakout_risk") or "").upper()
        fake_penalty = FAKE_RISK_TO_SCORE.get(fake_risk)
        if fake_penalty is None:
            fake_penalty = clamp(max(0.0, _f(row.get("wick_ratio")) - 0.55) * 220.0)

        return {
            "trend_score": round(clamp(abs(change_5m) * 20.0 + abs(change_15m) * 12.0 + abs(change_1h) * 6.0), 6),
            "volume_score": round(clamp((_f(row.get("volume_spike")) - 0.5) / 3.0 * 100.0), 6),
            "volatility_score": round(clamp(_f(row.get("atr_pct")) / 1.6 * 100.0), 6),
            "oi_score": round(norm_abs(_f(row.get("oi_change")), 2.0), 6),
            "taker_score": round(norm_abs(_f(row.get("taker_buy_ratio"), 0.5) - 0.5, 0.18), 6),
            "timeframe_score": 80.0 if (change_5m * change_15m > 0 and change_15m * change_1h >= 0) else 45.0,
            "sm_score": round(clamp(_f(row.get("sm_position"))), 6),
            "heat_score": round(clamp(heat_score), 6),
            "fake_penalty": round(clamp(fake_penalty), 6),
        }

    def _positive_adjustment(
        self,
        feature: str,
        default_weight: float,
        rows: list[dict[str, Any]],
        baseline: dict[str, Any],
    ) -> tuple[float, dict[str, Any] | None]:
        high_cut, low_cut = self._cuts(feature)
        high_rows = [row for row in rows if _f(row["features"].get(feature)) >= high_cut]
        low_rows = [row for row in rows if _f(row["features"].get(feature)) < low_cut]
        bucket_min = max(1, int(settings.radar_weight_bucket_min_samples or 30))
        if len(high_rows) < bucket_min:
            return default_weight, None

        high = self._metrics(high_rows)
        low = self._metrics(low_rows)
        pos_pf = float(settings.radar_weight_positive_profit_factor or 1.05)
        neg_pf = float(settings.radar_weight_negative_profit_factor or 0.95)
        delta = 0.0
        reasons: list[str] = []

        if high["pnl"] > 0 and high["profit_factor"] >= pos_pf:
            delta += 0.10
            delta += min(0.16, max(0.0, high["profit_factor"] - pos_pf) * 0.08)
            delta += min(0.08, max(0.0, high["win_rate"] - baseline["win_rate"]) * 0.6)
            reasons.append("high_feature_bucket_profitable")
        if high["pnl"] < 0 and high["profit_factor"] <= neg_pf:
            delta -= 0.14
            delta -= min(0.16, max(0.0, neg_pf - high["profit_factor"]) * 0.10)
            delta -= min(0.08, max(0.0, baseline["win_rate"] - high["win_rate"]) * 0.6)
            reasons.append("high_feature_bucket_losing")
        if len(low_rows) >= bucket_min:
            if low["pnl"] < 0 and low["profit_factor"] <= neg_pf and high["avg_pnl"] > low["avg_pnl"]:
                delta += 0.06
                reasons.append("low_feature_bucket_losing")
            if low["pnl"] > 0 and low["profit_factor"] >= pos_pf and low["avg_pnl"] > high["avg_pnl"]:
                delta -= 0.08
                reasons.append("low_feature_bucket_outperformed")

        delta = self._cap_delta(delta)
        if abs(delta) < 0.03:
            return default_weight, None

        target = self._cap_weight(default_weight, default_weight * (1.0 + delta))
        return target, {
            "feature": feature,
            "role": "positive",
            "delta_pct": round(delta, 6),
            "reason": ",".join(reasons),
            "evidence": {
                "high_cut": high_cut,
                "low_cut": low_cut,
                "high_bucket": high,
                "low_bucket": low,
                "baseline": baseline,
            },
        }

    def _penalty_adjustment(
        self,
        feature: str,
        default_weight: float,
        rows: list[dict[str, Any]],
        baseline: dict[str, Any],
    ) -> tuple[float, dict[str, Any] | None]:
        high_rows = [row for row in rows if _f(row["features"].get(feature)) >= 50.0]
        low_rows = [row for row in rows if _f(row["features"].get(feature)) < 35.0]
        bucket_min = max(1, int(settings.radar_weight_bucket_min_samples or 30))
        if len(high_rows) < bucket_min:
            return default_weight, None

        high = self._metrics(high_rows)
        low = self._metrics(low_rows)
        pos_pf = float(settings.radar_weight_positive_profit_factor or 1.05)
        neg_pf = float(settings.radar_weight_negative_profit_factor or 0.95)
        delta = 0.0
        reasons: list[str] = []

        if high["pnl"] < 0 and high["profit_factor"] <= neg_pf:
            delta += 0.14
            delta += min(0.16, max(0.0, neg_pf - high["profit_factor"]) * 0.10)
            delta += min(0.08, max(0.0, baseline["win_rate"] - high["win_rate"]) * 0.6)
            reasons.append("high_penalty_bucket_losing")
        if high["pnl"] > 0 and high["profit_factor"] >= pos_pf:
            delta -= 0.14
            delta -= min(0.16, max(0.0, high["profit_factor"] - pos_pf) * 0.08)
            delta -= min(0.08, max(0.0, high["win_rate"] - baseline["win_rate"]) * 0.6)
            reasons.append("high_penalty_bucket_profitable")
        if len(low_rows) >= bucket_min:
            if low["pnl"] > 0 and low["profit_factor"] >= pos_pf and high["avg_pnl"] < low["avg_pnl"]:
                delta += 0.06
                reasons.append("low_penalty_bucket_profitable")
            if low["pnl"] < 0 and low["profit_factor"] <= neg_pf and low["avg_pnl"] < high["avg_pnl"]:
                delta -= 0.06
                reasons.append("low_penalty_bucket_losing")

        delta = self._cap_delta(delta)
        if abs(delta) < 0.03:
            return default_weight, None

        target = self._cap_weight(default_weight, default_weight * (1.0 + delta))
        return target, {
            "feature": feature,
            "role": "penalty",
            "delta_pct": round(delta, 6),
            "reason": ",".join(reasons),
            "evidence": {
                "high_cut": 50.0,
                "low_cut": 35.0,
                "high_bucket": high,
                "low_bucket": low,
                "baseline": baseline,
            },
        }

    def _normalize_positive_weights(self, weights: dict[str, float]) -> dict[str, float]:
        out = dict(weights)
        target_sum = sum(value for value in SCORE_WEIGHTS.values() if value > 0)
        current_sum = sum(value for key, value in out.items() if SCORE_WEIGHTS[key] > 0)
        if current_sum <= 0:
            return out
        scale = target_sum / current_sum
        for key, default_weight in SCORE_WEIGHTS.items():
            if default_weight > 0:
                out[key] = self._cap_weight(default_weight, out[key] * scale)
        return out

    def _cap_delta(self, delta: float) -> float:
        max_adj = max(0.0, min(0.75, float(settings.radar_weight_max_adjustment_pct or 0.35)))
        return max(-max_adj, min(max_adj, delta))

    def _cap_weight(self, default_weight: float, value: float) -> float:
        max_adj = max(0.0, min(0.75, float(settings.radar_weight_max_adjustment_pct or 0.35)))
        if default_weight < 0:
            lower = default_weight * (1.0 + max_adj)
            upper = default_weight * (1.0 - max_adj)
            return max(lower, min(upper, value))
        lower = default_weight * (1.0 - max_adj)
        upper = default_weight * (1.0 + max_adj)
        return max(lower, min(upper, value))

    def _cuts(self, feature: str) -> tuple[float, float]:
        if feature == "timeframe_score":
            return 75.0, 50.0
        if feature in {"heat_score", "sm_score"}:
            return 55.0, 40.0
        return 60.0, 40.0

    def _metrics(self, rows: list[dict[str, Any]]) -> dict[str, Any]:
        pnls = [_f(row.get("pnl")) for row in rows]
        wins = [pnl for pnl in pnls if pnl > 0]
        losses = [pnl for pnl in pnls if pnl < 0]
        gross_profit = sum(wins)
        gross_loss = abs(sum(losses))
        trade_count = len(wins) + len(losses)
        return {
            "sample_count": len(pnls),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": round(len(wins) / max(1, trade_count), 4),
            "profit_factor": round(gross_profit / gross_loss, 4) if gross_loss > 0 else (999.0 if gross_profit > 0 else 0.0),
            "pnl": round(sum(pnls), 4),
            "avg_pnl": round(sum(pnls) / len(pnls), 6) if pnls else 0.0,
        }

    def _rounded_weights(self, weights: dict[str, float]) -> dict[str, float]:
        return {key: round(float(value), 6) for key, value in weights.items()}


def _f(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


def _sample_time(sample: dict[str, Any]) -> int:
    return int(_f(sample.get("close_time") or sample.get("ts_ms") or 0))


def _sample_key(sample: dict[str, Any]) -> str:
    raw = sample.get("sample_id") or sample.get("position_id")
    if raw:
        return str(raw)
    return "|".join(
        str(sample.get(key) or "")
        for key in ("symbol", "side", "direction", "open_time", "close_time", "pnl")
    )


def _dedupe_recent(samples: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for sample in sorted(samples, key=_sample_time, reverse=True):
        key = _sample_key(sample)
        if key in seen:
            continue
        seen.add(key)
        out.append(sample)
        if len(out) >= limit:
            break
    return out


radar_weight_calibrator = RadarWeightCalibrator()
