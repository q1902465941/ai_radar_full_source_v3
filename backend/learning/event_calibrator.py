from __future__ import annotations

import math
import time
from dataclasses import asdict, dataclass
from typing import Any

from backend.config import settings
from backend.learning.replay_memory import replay_memory
from backend.learning.strategy_filter import direction_confirmations
from backend.learning.trade_memory import trade_memory
from backend.models import RadarItem, StrategyPlan


EXCLUDED_REASONS = {"RESTORED_STALE_RECONCILE", "PRICE_SOURCE_STALE_RECONCILE", "ACCEPTANCE_TP2"}


@dataclass
class EventCalibrationReport:
    matched_samples: int
    match_level: str
    win_rate: float
    profit_factor: float
    pnl: float
    avg_pnl: float
    confidence: float
    adjusted_win_rate: float
    paper_ok: bool
    live_ok: bool
    reasons: list[str]
    best_patterns: list[dict[str, Any]]
    worst_patterns: list[dict[str, Any]]

    def asdict(self) -> dict[str, Any]:
        return asdict(self)


class EventCalibrator:
    def __init__(self) -> None:
        self._cache_until = 0.0
        self._sample_cache: list[dict[str, Any]] = []

    def evaluate(
        self,
        item: RadarItem,
        plan: StrategyPlan,
        *,
        heuristic_win_rate: float,
    ) -> EventCalibrationReport:
        if not settings.event_calibration_enabled:
            return self._empty_report(heuristic_win_rate, "disabled")

        samples, level = self._matched_samples(item, plan)
        metrics = self._metrics(samples)
        min_samples = max(1, int(settings.event_calibration_min_samples))
        enough = metrics["count"] >= min_samples
        confidence = self._confidence(metrics["count"], min_samples)
        adjusted = heuristic_win_rate
        if enough:
            adjusted = heuristic_win_rate * (1.0 - confidence) + metrics["win_rate"] * confidence

        reasons: list[str] = []
        if not enough:
            reasons.append("event_calibration_samples_low")
        low_reasons: list[str] = []
        if enough and metrics["win_rate"] < settings.event_calibration_min_win_rate:
            low_reasons.append("event_calibrated_win_rate_low")
        if enough and metrics["profit_factor"] < settings.event_calibration_min_profit_factor:
            low_reasons.append("event_calibrated_profit_factor_low")
        if enough and metrics["pnl"] <= settings.event_calibration_min_pnl:
            low_reasons.append("event_calibrated_pnl_low")
        if low_reasons and self._recent_matched_recovered(samples, min_samples):
            reasons.append("recent_similar_event_recovered")
        else:
            reasons.extend(low_reasons)

        paper_ok = not any(reason.endswith("_low") for reason in reasons if reason != "event_calibration_samples_low")
        live_ok = (
            enough
            and paper_ok
            and metrics["win_rate"] >= settings.event_calibration_live_min_win_rate
            and metrics["profit_factor"] >= settings.event_calibration_live_min_profit_factor
            and metrics["pnl"] > 0
        )

        return EventCalibrationReport(
            matched_samples=metrics["count"],
            match_level=level,
            win_rate=round(metrics["win_rate"], 4),
            profit_factor=round(metrics["profit_factor"], 4),
            pnl=round(metrics["pnl"], 4),
            avg_pnl=round(metrics["avg_pnl"], 6),
            confidence=round(confidence, 4),
            adjusted_win_rate=round(adjusted, 4),
            paper_ok=paper_ok,
            live_ok=live_ok,
            reasons=reasons,
            best_patterns=self._pattern_summary(samples, best=True),
            worst_patterns=self._pattern_summary(samples, best=False),
        )

    def compact_context(self, item: RadarItem | None = None) -> dict[str, Any]:
        samples = self._samples()
        global_metrics = self._metrics(samples)
        context: dict[str, Any] = {
            "enabled": settings.event_calibration_enabled,
            "sample_count": global_metrics["count"],
            "global_win_rate": round(global_metrics["win_rate"], 4),
            "global_profit_factor": round(global_metrics["profit_factor"], 4),
            "global_pnl": round(global_metrics["pnl"], 4),
            "minimums": {
                "samples": settings.event_calibration_min_samples,
                "paper_win_rate": settings.event_calibration_min_win_rate,
                "paper_profit_factor": settings.event_calibration_min_profit_factor,
                "live_win_rate": settings.event_calibration_live_min_win_rate,
                "live_profit_factor": settings.event_calibration_live_min_profit_factor,
            },
            "best_patterns": self._pattern_summary(samples, best=True),
            "worst_patterns": self._pattern_summary(samples, best=False),
            "instruction": (
                "Use this compressed event evidence only as historical calibration. "
                "Do not infer edge from raw market narrative. Prefer WAIT when similar-event evidence is weak."
            ),
        }
        if item is not None:
            matched, level = self._matched_samples(item, None)
            metrics = self._metrics(matched)
            context["similar_current_event"] = {
                "match_level": level,
                "samples": metrics["count"],
                "win_rate": round(metrics["win_rate"], 4),
                "profit_factor": round(metrics["profit_factor"], 4),
                "pnl": round(metrics["pnl"], 4),
            }
        return context

    def summary(self) -> dict[str, Any]:
        return self.compact_context()

    def _empty_report(self, heuristic_win_rate: float, reason: str) -> EventCalibrationReport:
        return EventCalibrationReport(
            matched_samples=0,
            match_level=reason,
            win_rate=0.0,
            profit_factor=0.0,
            pnl=0.0,
            avg_pnl=0.0,
            confidence=0.0,
            adjusted_win_rate=round(heuristic_win_rate, 4),
            paper_ok=True,
            live_ok=False,
            reasons=[f"event_calibration_{reason}"],
            best_patterns=[],
            worst_patterns=[],
        )

    def _samples(self) -> list[dict[str, Any]]:
        now = time.time()
        if now < self._cache_until:
            return self._sample_cache

        limit = max(100, int(settings.event_calibration_sample_limit))
        samples: list[dict[str, Any]] = []
        if settings.replay_enabled and settings.event_calibration_use_replay:
            samples.extend(replay_memory.samples(limit=limit))
        if settings.event_calibration_use_closed_trades:
            samples.extend(trade_memory.samples(limit=limit, require_radar=True))
        samples = [sample for sample in samples if self._usable_sample(sample)]
        samples = self._dedupe_recent(samples, limit)
        self._sample_cache = samples[:limit]
        self._cache_until = now + max(1, int(settings.event_calibration_ttl_seconds))
        return self._sample_cache

    def _usable_sample(self, sample: dict[str, Any]) -> bool:
        side = sample.get("side") or sample.get("direction")
        if side not in {"LONG", "SHORT"}:
            return False
        if str(sample.get("close_reason") or "") in EXCLUDED_REASONS:
            return False
        return _f(sample.get("pnl")) != 0.0

    def _matched_samples(self, item: RadarItem, plan: StrategyPlan | None) -> tuple[list[dict[str, Any]], str]:
        samples = self._samples()
        side = plan.side if plan and plan.side in {"LONG", "SHORT"} else item.direction
        if side not in {"LONG", "SHORT"}:
            return [], "neutral"
        current = item.asdict()
        current["side"] = side
        current_confirms = direction_confirmations(current, side)
        current_score = _f(current.get("score"))
        current_fund = _f(current.get("fund_confirm_count"))
        current_fake = str(current.get("fake_breakout_risk") or "")
        current_checks = self._alignment_checks(current, side)

        def base_filter(sample: dict[str, Any]) -> bool:
            return (sample.get("side") or sample.get("direction")) == side

        def strict_filter(sample: dict[str, Any]) -> bool:
            if not base_filter(sample):
                return False
            if str(sample.get("fake_breakout_risk") or "") != current_fake:
                return False
            if _f(sample.get("score")) < max(0.0, current_score - 10.0):
                return False
            if _f(sample.get("fund_confirm_count")) < max(2.0, min(current_fund, 3.0)):
                return False
            if direction_confirmations(sample, side) < max(3, current_confirms - 1):
                return False
            sample_checks = self._alignment_checks(sample, side)
            for key in ("timeframe", "taker", "depth"):
                if current_checks[key] and not sample_checks[key]:
                    return False
            return True

        def relaxed_filter(sample: dict[str, Any]) -> bool:
            if not base_filter(sample):
                return False
            if str(sample.get("fake_breakout_risk") or "") == "HIGH":
                return False
            if _f(sample.get("score")) < max(0.0, current_score - 20.0):
                return False
            if _f(sample.get("fund_confirm_count")) < 2:
                return False
            if direction_confirmations(sample, side) < 3:
                return False
            return True

        strict = [sample for sample in samples if strict_filter(sample)]
        if len(strict) >= settings.event_calibration_min_samples:
            return strict, "strict_similar_event"
        relaxed = [sample for sample in samples if relaxed_filter(sample)]
        if len(relaxed) >= settings.event_calibration_min_samples:
            return relaxed, "relaxed_similar_event"
        baseline = [
            sample
            for sample in samples
            if base_filter(sample)
            and str(sample.get("fake_breakout_risk") or "") != "HIGH"
            and _f(sample.get("score")) >= 50
        ]
        return baseline or relaxed or strict, "side_baseline"

    def _alignment_checks(self, row: dict[str, Any], side: str) -> dict[str, bool]:
        if side == "LONG":
            return {
                "timeframe": _f(row.get("change_5m")) > 0 and _f(row.get("change_15m")) > 0 and _f(row.get("change_1h")) >= 0,
                "taker": _f(row.get("taker_buy_ratio"), 0.5) >= 0.58,
                "depth": _f(row.get("depth_imbalance")) >= 0.12,
            }
        return {
            "timeframe": _f(row.get("change_5m")) < 0 and _f(row.get("change_15m")) < 0 and _f(row.get("change_1h")) <= 0,
            "taker": _f(row.get("taker_sell_ratio"), 0.5) >= 0.58,
            "depth": _f(row.get("depth_imbalance")) <= -0.12,
        }

    def _metrics(self, samples: list[dict[str, Any]]) -> dict[str, float]:
        pnls = [_f(sample.get("pnl")) for sample in samples]
        wins = [pnl for pnl in pnls if pnl > 0]
        losses = [pnl for pnl in pnls if pnl < 0]
        gross_profit = sum(wins)
        gross_loss = abs(sum(losses))
        return {
            "count": len(pnls),
            "win_rate": len(wins) / max(1, len(wins) + len(losses)),
            "profit_factor": gross_profit / gross_loss if gross_loss > 0 else (999.0 if gross_profit > 0 else 0.0),
            "pnl": sum(pnls),
            "avg_pnl": sum(pnls) / len(pnls) if pnls else 0.0,
        }

    def _recent_matched_recovered(self, samples: list[dict[str, Any]], min_samples: int) -> bool:
        recent = self._dedupe_recent(samples, min_samples)
        if len(recent) < min_samples:
            return False
        metrics = self._metrics(recent)
        return (
            metrics["win_rate"] >= settings.event_calibration_min_win_rate
            and metrics["profit_factor"] >= settings.event_calibration_min_profit_factor
            and metrics["pnl"] > settings.event_calibration_min_pnl
        )

    def _dedupe_recent(self, samples: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
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

    def _confidence(self, count: int, min_samples: int) -> float:
        if count < min_samples:
            return 0.0
        return min(0.80, math.sqrt(count / max(1, min_samples)) / 4.0)

    def _pattern_summary(self, samples: list[dict[str, Any]], *, best: bool) -> list[dict[str, Any]]:
        buckets: dict[str, list[dict[str, Any]]] = {}
        for sample in samples:
            key = self._bucket_key(sample)
            buckets.setdefault(key, []).append(sample)
        records = []
        min_bucket = max(3, min(int(settings.event_calibration_min_samples), 20))
        for key, bucket in buckets.items():
            if len(bucket) < min_bucket:
                continue
            metrics = self._metrics(bucket)
            records.append(
                {
                    "pattern": key,
                    "samples": metrics["count"],
                    "win_rate": round(metrics["win_rate"], 4),
                    "profit_factor": round(metrics["profit_factor"], 4),
                    "pnl": round(metrics["pnl"], 4),
                }
            )
        if best:
            records.sort(key=lambda x: (x["profit_factor"], x["win_rate"], x["pnl"]), reverse=True)
        else:
            records.sort(key=lambda x: (x["pnl"], x["profit_factor"], x["win_rate"]))
        return records[:5]

    def _bucket_key(self, row: dict[str, Any]) -> str:
        side = str(row.get("side") or row.get("direction") or "NA")
        score = int(_f(row.get("score")) // 10 * 10)
        fake = str(row.get("fake_breakout_risk") or "NA")
        confirms = direction_confirmations(row, side) if side in {"LONG", "SHORT"} else 0
        fund = int(_f(row.get("fund_confirm_count")))
        wick = _f(row.get("wick_ratio"))
        volume = _f(row.get("volume_spike"))
        wick_band = "wick_low" if wick <= 0.45 else ("wick_mid" if wick <= 0.55 else "wick_high")
        volume_band = "vol_hot" if volume >= 1.8 else ("vol_ok" if volume >= 1.2 else "vol_weak")
        return f"{side}|score{score}+|fake={fake}|fund={fund}|conf={confirms}|{wick_band}|{volume_band}"


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


event_calibrator = EventCalibrator()
