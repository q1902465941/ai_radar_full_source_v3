from __future__ import annotations

import json
import time
from typing import Any

import httpx

from backend.config import settings
from backend.storage.db import db


async def fetch_live_radar_payload() -> dict[str, Any] | None:
    payload = await _fetch_legacy_http_payload()
    if payload is not None:
        return payload
    if bool(settings.monitor_legacy_db_fallback_enabled):
        return _fetch_legacy_db_payload()
    return None


def legacy_payload_scan_response(payload: dict[str, Any], *, include_details: bool = False) -> dict[str, Any]:
    rows = _rows(payload.get("top50"))
    confirmed = _rows(payload.get("top4") or payload.get("top5_confirmed") or payload.get("trade_top5"))
    scan_status = payload.get("scan_status") if isinstance(payload.get("scan_status"), dict) else {}
    market_heat = _int(payload.get("market_heat"), _avg_score(rows[:20]))
    alert_count = _int(payload.get("alert_count"), len(confirmed))
    scan_id = str(payload.get("last_scan_id") or scan_status.get("last_scan_id") or "legacy-live")
    last_scan_time = payload.get("last_scan_time") or scan_status.get("last_scan_time")
    return {
        "ok": True,
        "scan": {
            "scan_id": scan_id,
            "state": "succeeded",
            "source": "legacy_live",
            "top50_count": len(rows),
            "top4_count": len(confirmed),
            "market_heat": market_heat,
            "alert_count": alert_count,
            "duration_ms": 0,
            "error": str(payload.get("error") or ""),
            "metadata": {"source_endpoint": "/api/radar", "last_scan_time": last_scan_time},
            "started_at": None,
            "completed_at": None,
            "created_at": None,
            "updated_at": None,
        },
        "candidates": [_candidate_from_live_row(row, scan_id=scan_id, include_details=include_details) for row in rows],
    }


async def _fetch_legacy_http_payload() -> dict[str, Any] | None:
    base_url = str(settings.monitor_legacy_backend_url or "").strip().rstrip("/")
    if not base_url:
        return None
    timeout = max(0.2, float(settings.monitor_legacy_backend_timeout_seconds or 3.0))
    try:
        async with httpx.AsyncClient(base_url=base_url, timeout=timeout) as client:
            radar_response = await client.get("/api/radar")
            radar_response.raise_for_status()
            payload = radar_response.json()
            if not _usable_payload(payload):
                return None
            try:
                state_response = await client.get("/api/state")
                if state_response.status_code == 200:
                    _merge_state(payload, state_response.json())
            except Exception:
                pass
            return payload
    except Exception:
        return None


def _fetch_legacy_db_payload() -> dict[str, Any] | None:
    try:
        with db.conn() as conn:
            latest = conn.execute(
                """
                SELECT scan_id, MAX(ts_ms) AS last_ts
                FROM radar_snapshots
                GROUP BY scan_id
                ORDER BY last_ts DESC
                LIMIT 1
                """
            ).fetchone()
            if not latest:
                return None
            last_ts = int(latest["last_ts"] or 0)
            max_age_ms = max(1, int(settings.monitor_legacy_snapshot_max_age_seconds or 240)) * 1000
            if last_ts <= 0 or int(time.time() * 1000) - last_ts > max_age_ms:
                return None
            rows = conn.execute(
                "SELECT payload FROM radar_snapshots WHERE scan_id=?",
                (latest["scan_id"],),
            ).fetchall()
    except Exception:
        return None

    items: list[dict[str, Any]] = []
    for row in rows:
        try:
            payload = json.loads(row["payload"])
        except Exception:
            continue
        if isinstance(payload, dict):
            items.append(payload)
    if not items:
        return None
    items.sort(key=lambda item: _int(item.get("rank"), 999999))
    confirmed = [item for item in items if bool(item.get("ai_candidate"))][:5]
    last_scan_time = time.strftime("%H:%M:%S", time.localtime(last_ts / 1000))
    symbols = [str(item.get("symbol") or "") for item in items if item.get("symbol")]
    return {
        "ok": True,
        "last_scan_id": str(latest["scan_id"] or ""),
        "last_scan_time": last_scan_time,
        "market_heat": _avg_score(items[:20]),
        "alert_count": len(confirmed),
        "top50": items,
        "top4": confirmed,
        "scan_status": {
            "last_scan_id": str(latest["scan_id"] or ""),
            "last_scan_time": last_scan_time,
            "top50_count": len(items),
            "market_refresh": {"source": "legacy_db", "degraded": False, "error": ""},
            "active_coins": {"active_count": len(symbols), "active_symbols": symbols[:200]},
            "dynamic_stream": {"active_count": len(symbols), "active_symbols": symbols[:200]},
        },
    }


def _candidate_from_live_row(row: dict[str, Any], *, scan_id: str, include_details: bool) -> dict[str, Any]:
    data = {
        "scan_id": scan_id,
        "symbol": str(row.get("symbol") or ""),
        "base_asset": str(row.get("base_asset") or row.get("symbol") or ""),
        "rank": _int(row.get("rank"), 0),
        "score": _float(row.get("score")),
        "direction": str(row.get("direction") or ""),
        "stage": str(row.get("stage") or ""),
        "trigger_mode": str(row.get("trigger_mode") or ""),
        "price": _float(row.get("price")),
        "change_5m": _float(row.get("change_5m")),
        "change_15m": _float(row.get("change_15m")),
        "change_1h": _float(row.get("change_1h")),
        "oi_change": _float(row.get("oi_change")),
        "fund_confirm_count": _int(row.get("fund_confirm_count"), 0),
        "fund_confirm_total": _int(row.get("fund_confirm_total"), 0),
        "fake_breakout_risk": str(row.get("fake_breakout_risk") or ""),
        "ai_candidate": bool(row.get("ai_candidate")),
        "market_structure": row.get("market_structure") if isinstance(row.get("market_structure"), dict) else {},
    }
    if include_details:
        data.update(
            {
                "score_features": row.get("score_features") if isinstance(row.get("score_features"), dict) else {},
                "score_explain": row.get("score_explain") if isinstance(row.get("score_explain"), dict) else {},
                "raw": dict(row),
            }
        )
    return data


def _usable_payload(payload: Any) -> bool:
    if not isinstance(payload, dict):
        return False
    rows = payload.get("top50")
    return isinstance(rows, list) and len(rows) > 0


def _merge_state(payload: dict[str, Any], state: Any) -> None:
    if not isinstance(state, dict):
        return
    for key in ("last_scan_time", "market_heat", "alert_count", "market_data_source"):
        if key in state:
            payload[key] = state[key]


def _rows(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [dict(row) for row in value if isinstance(row, dict)]


def _avg_score(rows: list[dict[str, Any]]) -> int:
    if not rows:
        return 0
    return round(sum(_float(row.get("score")) for row in rows) / len(rows))


def _int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return int(default)


def _float(value: Any) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0
