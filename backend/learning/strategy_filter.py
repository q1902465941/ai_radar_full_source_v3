from __future__ import annotations

from typing import Any

from backend.models import RadarItem


def strategy_matches(strategy: dict[str, Any], item_or_sample: RadarItem | dict[str, Any]) -> bool:
    filters = strategy.get("filters", strategy)
    row = _as_row(item_or_sample)
    side = row.get("side") or row.get("direction")
    if side not in {"LONG", "SHORT"}:
        return False

    allowed_sides = set(filters.get("allowed_sides") or ["LONG", "SHORT"])
    if side not in allowed_sides:
        return False

    blocked_symbols = set(filters.get("blocked_symbols") or [])
    if row.get("symbol") in blocked_symbols:
        return False

    if _f(row.get("score")) < _f(filters.get("min_score"), 0):
        return False
    if _f(row.get("fund_confirm_count")) < _f(filters.get("min_fund_confirm"), 0):
        return False
    if row.get("fake_breakout_risk") not in set(filters.get("allowed_fake_risks") or ["LOW"]):
        return False
    if direction_confirmations(row, side) < int(filters.get("min_direction_confirmations", 0)):
        return False
    if _f(row.get("volume_spike")) < _f(filters.get("min_volume_spike"), 0):
        return False
    if _wick_ratio(row) > _f(filters.get("max_wick_ratio"), 999):
        return False

    if filters.get("require_oi_positive") and _f(row.get("oi_change")) < 0:
        return False
    if filters.get("require_timeframe_alignment") and not _timeframes_aligned(row, side):
        return False
    if filters.get("require_taker_alignment") and not _taker_aligned(row, side):
        return False
    if filters.get("require_depth_alignment") and not _depth_aligned(row, side):
        return False
    if filters.get("require_sm_delta_alignment") and not _sm_aligned(row, side):
        return False

    return True


def direction_confirmations(row: dict[str, Any], side: str) -> int:
    if side == "LONG":
        checks = [
            _f(row.get("change_5m")) > 0,
            _f(row.get("change_15m")) > 0,
            _f(row.get("change_1h")) >= 0,
            _f(row.get("taker_buy_ratio"), 0.5) >= 0.58,
            _f(row.get("depth_imbalance")) >= 0.12,
            _f(row.get("sm_delta")) >= 0,
            _f(row.get("volume_spike")) >= 1.5,
            _f(row.get("oi_change")) >= 0,
            _wick_ratio(row) <= 0.55,
        ]
    elif side == "SHORT":
        checks = [
            _f(row.get("change_5m")) < 0,
            _f(row.get("change_15m")) < 0,
            _f(row.get("change_1h")) <= 0,
            _f(row.get("taker_sell_ratio"), 0.5) >= 0.58,
            _f(row.get("depth_imbalance")) <= -0.12,
            _f(row.get("sm_delta")) <= 0,
            _f(row.get("volume_spike")) >= 1.5,
            _f(row.get("oi_change")) >= 0,
            _wick_ratio(row) <= 0.55,
        ]
    else:
        return 0
    return sum(1 for ok in checks if ok)


def _as_row(item_or_sample: RadarItem | dict[str, Any]) -> dict[str, Any]:
    if isinstance(item_or_sample, RadarItem):
        return item_or_sample.asdict()
    radar = item_or_sample.get("radar")
    if isinstance(radar, dict):
        row = {**radar, **{k: v for k, v in item_or_sample.items() if k != "radar"}}
        row.setdefault("direction", row.get("side") or radar.get("direction"))
        return row
    return dict(item_or_sample)


def _timeframes_aligned(row: dict[str, Any], side: str) -> bool:
    if side == "LONG":
        return _f(row.get("change_5m")) > 0 and _f(row.get("change_15m")) > 0 and _f(row.get("change_1h")) >= 0
    return _f(row.get("change_5m")) < 0 and _f(row.get("change_15m")) < 0 and _f(row.get("change_1h")) <= 0


def _taker_aligned(row: dict[str, Any], side: str) -> bool:
    return _f(row.get("taker_buy_ratio"), 0.5) >= 0.58 if side == "LONG" else _f(row.get("taker_sell_ratio"), 0.5) >= 0.58


def _depth_aligned(row: dict[str, Any], side: str) -> bool:
    return _f(row.get("depth_imbalance")) >= 0.12 if side == "LONG" else _f(row.get("depth_imbalance")) <= -0.12


def _sm_aligned(row: dict[str, Any], side: str) -> bool:
    return _f(row.get("sm_delta")) >= 0 if side == "LONG" else _f(row.get("sm_delta")) <= 0


def _f(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


def _wick_ratio(row: dict[str, Any]) -> float:
    features = row.get("score_features")
    metrics = features.get("structure_metrics") if isinstance(features, dict) else {}
    if isinstance(metrics, dict) and "current_wick_ratio" in metrics:
        return _f(metrics.get("current_wick_ratio"))
    return _f(row.get("wick_ratio"))
