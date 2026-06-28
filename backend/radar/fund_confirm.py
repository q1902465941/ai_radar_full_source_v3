from __future__ import annotations

from backend.models import Direction, MarketSnapshot


def fund_confirm(snapshot: MarketSnapshot, direction: Direction) -> tuple[int, int]:
    components = fund_confirm_components(snapshot, direction)
    return sum(1 for ok in components.values() if ok), len(components)


def fund_confirm_components(snapshot: MarketSnapshot, direction: Direction) -> dict[str, bool]:
    wick_ratio = _current_wick_ratio(snapshot)
    if direction == "LONG":
        aligned_5m = snapshot.change_5m > 0
        aligned_15m = snapshot.change_15m > 0
        one_hour_not_against = snapshot.change_1h >= -0.20
        oi_aligned = aligned_5m and snapshot.oi_change >= 0.0
        taker_aligned = snapshot.taker_buy_ratio >= 0.53
        depth_aligned = snapshot.depth_imbalance >= 0.06
    elif direction == "SHORT":
        aligned_5m = snapshot.change_5m < 0
        aligned_15m = snapshot.change_15m < 0
        one_hour_not_against = snapshot.change_1h <= 0.20
        oi_aligned = aligned_5m and snapshot.oi_change >= 0.0
        taker_aligned = snapshot.taker_sell_ratio >= 0.53
        depth_aligned = snapshot.depth_imbalance <= -0.06
    else:
        return {
            "volume_expansion": False,
            "oi_alignment": False,
            "flow_or_book_alignment": False,
            "timeframe_quality": False,
            "low_noise": False,
        }

    timeframe_quality = aligned_5m and aligned_15m and one_hour_not_against
    low_noise = wick_ratio <= 0.55 and abs(snapshot.funding_rate) <= 0.0025
    return {
        "volume_expansion": snapshot.volume_spike >= 1.45,
        "oi_alignment": oi_aligned,
        "flow_or_book_alignment": taker_aligned or depth_aligned,
        "timeframe_quality": timeframe_quality,
        "low_noise": low_noise,
    }


def _current_wick_ratio(snapshot: MarketSnapshot) -> float:
    metrics = getattr(snapshot, "structure_metrics", {}) or {}
    if isinstance(metrics, dict) and "current_wick_ratio" in metrics:
        try:
            return max(0.0, float(metrics.get("current_wick_ratio") or 0.0))
        except (TypeError, ValueError):
            pass
    return max(0.0, float(snapshot.wick_ratio or 0.0))
