from __future__ import annotations

from dataclasses import asdict, is_dataclass
from typing import Any


def _item_dict(item: Any) -> dict[str, Any]:
    if isinstance(item, dict):
        return item
    if hasattr(item, "asdict"):
        data = item.asdict()
        return data if isinstance(data, dict) else {}
    if is_dataclass(item):
        return asdict(item)
    return {}


def _structure(item: dict[str, Any]) -> dict[str, Any]:
    value = item.get("market_structure")
    return value if isinstance(value, dict) else {}


def _pct(part: int, total: int) -> int:
    if total <= 0:
        return 0
    return round(part / total * 100)


def _candidate_view(item: dict[str, Any]) -> dict[str, object]:
    structure = _structure(item)
    return {
        "symbol": item.get("symbol") or "",
        "base_asset": item.get("base_asset") or item.get("symbol") or "",
        "direction": item.get("direction") or "NEUTRAL",
        "score": round(float(item.get("score") or 0)),
        "action": structure.get("action") or "WAIT",
        "regime": structure.get("regime") or "",
        "phase": structure.get("phase") or "",
    }


def build_dashboard_overview(radar_engine: object) -> dict[str, object]:
    rows = [_item_dict(item) for item in (getattr(radar_engine, "top50", []) or [])]
    confirmed = [_item_dict(item) for item in (getattr(radar_engine, "top4", []) or [])]
    scan_status = radar_engine.scan_status()
    active = scan_status.get("active_coins", {}) if isinstance(scan_status, dict) else {}
    stream = scan_status.get("dynamic_stream", {}) if isinstance(scan_status, dict) else {}

    long_count = sum(1 for item in rows if item.get("direction") == "LONG")
    short_count = sum(1 for item in rows if item.get("direction") == "SHORT")
    neutral_count = max(0, len(rows) - long_count - short_count)
    ai_count = sum(1 for item in rows if bool(item.get("ai_candidate")))
    actionable_count = sum(
        1
        for item in rows
        if _structure(item).get("action") in {"OPEN_LONG", "OPEN_SHORT"}
    )
    fund_ready_count = sum(1 for item in rows if int(item.get("fund_confirm_count") or 0) >= 3)
    fake_high_count = sum(1 for item in rows if item.get("fake_breakout_risk") == "HIGH")
    average_score = round(sum(float(item.get("score") or 0) for item in rows) / len(rows)) if rows else 0

    state_code = "WATCH" if actionable_count else "FILTERING" if ai_count else "NEUTRAL"
    state_text = {
        "WATCH": "Executable structures exist, but risk and cost checks remain mandatory.",
        "FILTERING": "AI candidates exist; the system is filtering noise, funding, and structure quality.",
        "NEUTRAL": "No clear structure is active. Keep observing until cleaner candidates appear.",
    }[state_code]

    total = max(1, len(rows))
    long_pct = _pct(long_count, total)
    short_pct = _pct(short_count, total)
    neutral_pct = max(0, 100 - long_pct - short_pct)
    candidates_source = confirmed if confirmed else rows[:4]

    return {
        "ok": True,
        "state": {"code": state_code, "text": state_text},
        "metrics": {
            "top50_count": len(rows),
            "ai_candidate_count": ai_count,
            "actionable_count": actionable_count,
            "average_score": average_score,
            "dynamic_stream_count": int(stream.get("active_count") or 0) if isinstance(stream, dict) else 0,
            "active_coin_count": int(active.get("active_count") or 0) if isinstance(active, dict) else 0,
            "fund_ready_count": fund_ready_count,
            "fake_high_count": fake_high_count,
        },
        "direction": {
            "long": long_count,
            "short": short_count,
            "neutral": neutral_count,
            "long_pct": long_pct,
            "short_pct": short_pct,
            "neutral_pct": neutral_pct,
        },
        "candidates": [_candidate_view(item) for item in candidates_source[:4]],
        "scan": {
            "last_scan_id": getattr(radar_engine, "last_scan_id", ""),
            "last_scan_time": getattr(radar_engine, "last_scan_time", ""),
            "market_heat": getattr(radar_engine, "market_heat", 0),
            "alert_count": getattr(radar_engine, "alert_count", 0),
            "scan_status": scan_status if isinstance(scan_status, dict) else {},
        },
    }
