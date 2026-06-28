from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import delete, select
from sqlalchemy.orm import Session, sessionmaker

from backend.app.db.models import RadarCandidateRecord, RadarScanRecord
from backend.app.db.session import session_scope


def item_asdict(item: object) -> dict[str, Any]:
    if isinstance(item, dict):
        return dict(item)
    asdict_fn = getattr(item, "asdict", None)
    if callable(asdict_fn):
        return dict(asdict_fn())
    return {}


def save_radar_scan_result(
    session_factory: sessionmaker[Session],
    *,
    scan_id: str,
    items: Iterable[object],
    source: str,
    state: str,
    market_heat: int,
    alert_count: int,
    duration_ms: int,
    started_at: datetime | None,
    completed_at: datetime | None,
    metadata: dict[str, Any] | None = None,
    error: str = "",
) -> str:
    item_rows = [item_asdict(item) for item in items]
    persisted_scan_id = scan_id or uuid4().hex[:12]

    with session_scope(session_factory) as session:
        scan = session.execute(
            select(RadarScanRecord).where(RadarScanRecord.scan_id == persisted_scan_id)
        ).scalar_one_or_none()
        if scan is None:
            scan = RadarScanRecord(scan_id=persisted_scan_id)
            session.add(scan)

        scan.state = state
        scan.source = source
        scan.top50_count = len(item_rows)
        scan.top4_count = sum(1 for row in item_rows if bool(row.get("ai_candidate")))
        scan.market_heat = int(market_heat or 0)
        scan.alert_count = int(alert_count or 0)
        scan.duration_ms = max(0, int(duration_ms or 0))
        scan.error = str(error or "")[:1000]
        scan.metadata_json = dict(metadata or {})
        scan.started_at = started_at
        scan.completed_at = completed_at

        session.execute(delete(RadarCandidateRecord).where(RadarCandidateRecord.scan_id == persisted_scan_id))
        for row in item_rows:
            session.add(_candidate_from_row(persisted_scan_id, row))

    return persisted_scan_id


def radar_scan_record_asdict(scan: RadarScanRecord) -> dict[str, Any]:
    return {
        "scan_id": scan.scan_id,
        "state": scan.state,
        "source": scan.source,
        "top50_count": scan.top50_count,
        "top4_count": scan.top4_count,
        "market_heat": scan.market_heat,
        "alert_count": scan.alert_count,
        "duration_ms": scan.duration_ms,
        "error": scan.error,
        "metadata": scan.metadata_json,
        "started_at": scan.started_at.isoformat() if scan.started_at else None,
        "completed_at": scan.completed_at.isoformat() if scan.completed_at else None,
        "created_at": scan.created_at.isoformat() if scan.created_at else None,
        "updated_at": scan.updated_at.isoformat() if scan.updated_at else None,
    }


def radar_candidate_record_asdict(
    candidate: RadarCandidateRecord,
    *,
    include_details: bool = False,
) -> dict[str, Any]:
    data = {
        "scan_id": candidate.scan_id,
        "symbol": candidate.symbol,
        "base_asset": candidate.base_asset,
        "rank": candidate.rank,
        "score": candidate.score,
        "direction": candidate.direction,
        "stage": candidate.stage,
        "trigger_mode": candidate.trigger_mode,
        "price": candidate.price,
        "change_5m": candidate.change_5m,
        "change_15m": candidate.change_15m,
        "change_1h": candidate.change_1h,
        "oi_change": candidate.oi_change,
        "fund_confirm_count": candidate.fund_confirm_count,
        "fund_confirm_total": candidate.fund_confirm_total,
        "fake_breakout_risk": candidate.fake_breakout_risk,
        "ai_candidate": candidate.ai_candidate,
        "market_structure": candidate.market_structure_json,
    }
    if include_details:
        data.update(
            {
                "score_features": candidate.score_features_json,
                "score_explain": candidate.score_explain_json,
                "raw": candidate.raw_json,
            }
        )
    return data


def _candidate_from_row(scan_id: str, row: dict[str, Any]) -> RadarCandidateRecord:
    return RadarCandidateRecord(
        scan_id=scan_id,
        symbol=_text(row.get("symbol")),
        base_asset=_text(row.get("base_asset")),
        rank=_int(row.get("rank")),
        score=_float(row.get("score")),
        direction=_text(row.get("direction")),
        stage=_text(row.get("stage")),
        trigger_mode=_text(row.get("trigger_mode")),
        price=_float(row.get("price")),
        change_5m=_float(row.get("change_5m")),
        change_15m=_float(row.get("change_15m")),
        change_1h=_float(row.get("change_1h")),
        oi_change=_float(row.get("oi_change")),
        fund_confirm_count=_int(row.get("fund_confirm_count")),
        fund_confirm_total=_int(row.get("fund_confirm_total")),
        fake_breakout_risk=_text(row.get("fake_breakout_risk")),
        ai_candidate=bool(row.get("ai_candidate")),
        market_structure_json=_json_dict(row.get("market_structure")),
        score_features_json=_json_dict(row.get("score_features")),
        score_explain_json=_json_dict(row.get("score_explain")),
        raw_json=row,
    )


def _text(value: Any) -> str:
    return "" if value is None else str(value)


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _float(value: Any) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _json_dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}
