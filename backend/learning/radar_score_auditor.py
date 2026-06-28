from __future__ import annotations

from typing import Any

from backend.config import settings
from backend.learning.replay_memory import replay_memory
from backend.learning.strategy_filter import direction_confirmations
from backend.learning.trade_memory import trade_memory


SCORE_BANDS = [
    (0.0, 40.0, "score_lt_40"),
    (40.0, 50.0, "score_40_50"),
    (50.0, 60.0, "score_50_60"),
    (60.0, 70.0, "score_60_70"),
    (70.0, 101.0, "score_70_plus"),
]


class RadarScoreAuditor:
    def report(self, *, current_items: list[Any] | None = None, limit: int | None = None) -> dict[str, Any]:
        limit = max(100, min(20000, int(limit or settings.event_calibration_sample_limit or 5000)))
        samples = self._samples(limit)
        rows = [self._row(sample) for sample in samples]
        rows = [row for row in rows if row.get("side") in {"LONG", "SHORT"} and _f(row.get("score")) > 0]

        by_score_band = [
            {
                "band": name,
                "range": {"min": low, "max": high},
                **self._metrics([row for row in rows if low <= _f(row.get("score")) < high]),
            }
            for low, high, name in SCORE_BANDS
        ]
        factor_buckets = self._factor_buckets(rows)
        current = self._current_scan(current_items or [])
        return {
            "sample_count": len(rows),
            "sources": {
                "replay_enabled": bool(settings.replay_enabled),
                "replay_limit": limit,
                "trade_memory_join_required": True,
            },
            "validation": self._validation_summary(by_score_band, factor_buckets),
            "by_score_band": by_score_band,
            "factor_buckets": factor_buckets,
            "current_scan": current,
            "interpretation": {
                "score_role": "radar_score is an anomaly score; it must be validated against realized/replayed outcomes before being trusted as an edge score",
                "production_rule": "use score only with direction, fund confirmation, fake-breakout risk, cyqnt enhancement, attribution, and risk geometry",
                "sample_warning": "small buckets are diagnostic only and should not drive live execution",
            },
        }

    def _samples(self, limit: int) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        if settings.replay_enabled:
            out.extend(replay_memory.samples(limit=limit))
        out.extend(trade_memory.samples(limit=limit, require_radar=True))
        return _dedupe_recent(out, limit)

    def _row(self, sample: dict[str, Any]) -> dict[str, Any]:
        radar = sample.get("radar") if isinstance(sample.get("radar"), dict) else {}
        row = {**radar, **{key: value for key, value in sample.items() if key != "radar"}}
        side = row.get("side") or row.get("direction")
        row["side"] = side
        row["direction"] = side
        return row

    def _factor_buckets(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        buckets: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            for factor in self._factors(row):
                buckets.setdefault(factor, []).append(row)

        records = []
        for factor, bucket in buckets.items():
            metrics = self._metrics(bucket)
            records.append({"factor": factor, **metrics})
        records.sort(key=lambda row: (row["sample_count"], row["profit_factor"], row["win_rate"], row["pnl"]), reverse=True)
        return records[:40]

    def _factors(self, row: dict[str, Any]) -> list[str]:
        side = str(row.get("side") or row.get("direction") or "")
        confirms = direction_confirmations(row, side)
        wick = _f(row.get("wick_ratio"))
        volume = _f(row.get("volume_spike"))
        score = _f(row.get("score"))
        factors = [
            f"side_{side.lower()}",
            self._score_band(score),
            "fund_confirm_3" if _f(row.get("fund_confirm_count")) >= 3 else ("fund_confirm_2" if _f(row.get("fund_confirm_count")) >= 2 else "fund_confirm_lt2"),
            f"fake_{str(row.get('fake_breakout_risk') or 'NA').lower()}",
            "dirconf_6_plus" if confirms >= 6 else ("dirconf_4_5" if confirms >= 4 else "dirconf_lt4"),
            "wick_low" if wick <= 0.45 else ("wick_mid" if wick <= 0.55 else ("wick_high" if wick <= 0.78 else "wick_extreme")),
            "volume_hot" if volume >= 1.8 else ("volume_ok" if volume >= 1.2 else "volume_weak"),
        ]
        if side == "LONG":
            factors.append("timeframe_aligned" if _f(row.get("change_5m")) > 0 and _f(row.get("change_15m")) > 0 and _f(row.get("change_1h")) >= 0 else "timeframe_mixed")
            factors.append("taker_aligned" if _f(row.get("taker_buy_ratio"), 0.5) >= 0.58 else "taker_not_aligned")
            factors.append("depth_aligned" if _f(row.get("depth_imbalance")) >= 0.12 else "depth_not_aligned")
        elif side == "SHORT":
            factors.append("timeframe_aligned" if _f(row.get("change_5m")) < 0 and _f(row.get("change_15m")) < 0 and _f(row.get("change_1h")) <= 0 else "timeframe_mixed")
            factors.append("taker_aligned" if _f(row.get("taker_sell_ratio"), 0.5) >= 0.58 else "taker_not_aligned")
            factors.append("depth_aligned" if _f(row.get("depth_imbalance")) <= -0.12 else "depth_not_aligned")
        return factors

    def _score_band(self, score: float) -> str:
        for low, high, name in SCORE_BANDS:
            if low <= score < high:
                return name
        return "score_unknown"

    def _metrics(self, rows: list[dict[str, Any]]) -> dict[str, Any]:
        pnls = [_f(row.get("pnl")) for row in rows]
        wins = [pnl for pnl in pnls if pnl > 0]
        losses = [pnl for pnl in pnls if pnl < 0]
        gross_profit = sum(wins)
        gross_loss = abs(sum(losses))
        return {
            "sample_count": len(pnls),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": round(len(wins) / max(1, len(wins) + len(losses)), 4),
            "profit_factor": round(gross_profit / gross_loss, 4) if gross_loss > 0 else (999.0 if gross_profit > 0 else 0.0),
            "pnl": round(sum(pnls), 4),
            "avg_pnl": round(sum(pnls) / len(pnls), 6) if pnls else 0.0,
        }

    def _validation_summary(self, by_score_band: list[dict[str, Any]], factor_buckets: list[dict[str, Any]]) -> dict[str, Any]:
        enough_samples = sum(row["sample_count"] for row in by_score_band) >= 100
        high = next((row for row in by_score_band if row["band"] == "score_60_70"), {})
        higher = next((row for row in by_score_band if row["band"] == "score_70_plus"), {})
        mid = next((row for row in by_score_band if row["band"] == "score_40_50"), {})
        high_edge = (
            (high.get("sample_count", 0) >= 20 and high.get("profit_factor", 0.0) > 1.05)
            or (higher.get("sample_count", 0) >= 10 and higher.get("profit_factor", 0.0) > 1.05)
        )
        weak_score_warning = (
            mid.get("sample_count", 0) >= 20
            and high.get("sample_count", 0) >= 20
            and high.get("win_rate", 0.0) <= mid.get("win_rate", 0.0)
        )
        best_factors = [row for row in factor_buckets if row["sample_count"] >= 20 and row["profit_factor"] > 1.05][:8]
        bad_factors = [
            row
            for row in factor_buckets
            if row["sample_count"] >= 20 and row["pnl"] < 0 and row["profit_factor"] < 0.95
        ][:8]
        return {
            "enough_samples": enough_samples,
            "high_score_edge_observed": bool(high_edge),
            "score_monotonic_warning": bool(weak_score_warning),
            "best_validated_factors": best_factors,
            "negative_factors": bad_factors,
            "verdict": (
                "insufficient_samples"
                if not enough_samples
                else ("score_needs_recalibration" if weak_score_warning or not high_edge else "score_has_some_validated_edge")
            ),
        }

    def _current_scan(self, items: list[Any]) -> dict[str, Any]:
        rows = [item.asdict() if hasattr(item, "asdict") else dict(item) for item in items]
        band_counts: dict[str, int] = {}
        for row in rows:
            band = self._score_band(_f(row.get("score")))
            band_counts[band] = band_counts.get(band, 0) + 1
        top = []
        for row in rows[:10]:
            explain = row.get("score_explain") if isinstance(row.get("score_explain"), dict) else {}
            top.append(
                {
                    "symbol": row.get("symbol"),
                    "side": row.get("direction"),
                    "rank": row.get("rank"),
                    "score": row.get("score"),
                    "fund_confirm": f"{row.get('fund_confirm_count')}/{row.get('fund_confirm_total')}",
                    "fake_breakout_risk": row.get("fake_breakout_risk"),
                    "score_top_positive": explain.get("top_positive", [])[:3],
                    "score_top_penalty": explain.get("top_penalty", [])[:3],
                }
            )
        return {"count": len(rows), "score_band_counts": band_counts, "top": top}


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


radar_score_auditor = RadarScoreAuditor()
