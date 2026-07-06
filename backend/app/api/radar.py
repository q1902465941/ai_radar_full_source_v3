from __future__ import annotations

import time
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Request, status
from pydantic import BaseModel
from sqlalchemy import select

from backend.app.db.models import RadarCandidateRecord, RadarScanRecord, utc_now
from backend.app.db.session import session_scope
from backend.app.services.radar_persistence import (
    radar_candidate_record_asdict,
    radar_scan_record_asdict,
    save_radar_scan_result,
)
from backend.app.services.legacy_monitor_source import fetch_live_radar_payload, legacy_payload_scan_response
from backend.radar.radar_engine import radar_engine

router = APIRouter(prefix="/api/v2/radar", tags=["radar"])


class RadarScanRequest(BaseModel):
    force_refresh: bool = False


@router.get("/summary")
async def radar_summary() -> dict[str, object]:
    top50 = list(getattr(radar_engine, "top50", []) or [])
    top4 = list(getattr(radar_engine, "top4", []) or [])
    return {
        "ok": True,
        "top50_count": len(top50),
        "top4_count": len(top4),
        "last_scan_id": getattr(radar_engine, "last_scan_id", ""),
        "last_scan_time": getattr(radar_engine, "last_scan_time", ""),
        "market_heat": getattr(radar_engine, "market_heat", 0),
        "alert_count": getattr(radar_engine, "alert_count", 0),
        "scan_status": radar_engine.scan_status(),
    }


@router.post("/scans", status_code=status.HTTP_202_ACCEPTED)
async def start_radar_scan(payload: RadarScanRequest, request: Request) -> dict[str, object]:
    force_refresh = bool(payload.force_refresh)
    session_factory = request.app.state.session_factory

    async def run_scan() -> dict[str, Any]:
        started_at = utc_now()
        started = time.monotonic()
        try:
            items = await radar_engine.scan(force_refresh=force_refresh)
        except Exception as exc:
            completed_at = utc_now()
            duration_ms = int((time.monotonic() - started) * 1000)
            scan_id = getattr(radar_engine, "last_scan_id", "") or uuid4().hex[:12]
            save_radar_scan_result(
                session_factory,
                scan_id=scan_id,
                items=[],
                source="v2_task",
                state="failed",
                market_heat=int(getattr(radar_engine, "market_heat", 0) or 0),
                alert_count=int(getattr(radar_engine, "alert_count", 0) or 0),
                duration_ms=duration_ms,
                started_at=started_at,
                completed_at=completed_at,
                metadata={"force_refresh": force_refresh},
                error=f"{type(exc).__name__}:{exc}",
            )
            raise

        completed_at = utc_now()
        duration_ms = int((time.monotonic() - started) * 1000)
        scan_id = getattr(radar_engine, "last_scan_id", "") or uuid4().hex[:12]
        persisted_scan_id = save_radar_scan_result(
            session_factory,
            scan_id=scan_id,
            items=items,
            source="v2_task",
            state="succeeded",
            market_heat=int(getattr(radar_engine, "market_heat", 0) or 0),
            alert_count=int(getattr(radar_engine, "alert_count", 0) or 0),
            duration_ms=duration_ms,
            started_at=started_at,
            completed_at=completed_at,
            metadata={"force_refresh": force_refresh},
        )
        return {
            "scan_id": persisted_scan_id,
            "top50_count": len(items),
            "top4_count": len(getattr(radar_engine, "top4", []) or []),
            "market_heat": int(getattr(radar_engine, "market_heat", 0) or 0),
            "alert_count": int(getattr(radar_engine, "alert_count", 0) or 0),
            "duration_ms": duration_ms,
        }

    task = request.app.state.task_runner.submit(
        "radar_scan",
        {"force_refresh": force_refresh},
        run_scan,
    )
    return {"ok": True, "task": task.asdict()}


@router.get("/scans/latest")
async def latest_radar_scan(request: Request, include_details: bool = False) -> dict[str, object]:
    live_payload = await fetch_live_radar_payload()
    if live_payload is not None:
        return legacy_payload_scan_response(live_payload, include_details=include_details)

    session_factory = request.app.state.session_factory
    with session_scope(session_factory) as session:
        scan = session.execute(
            select(RadarScanRecord).order_by(RadarScanRecord.created_at.desc())
        ).scalars().first()
        if scan is None:
            return {"ok": True, "scan": None, "candidates": []}

        candidates = session.execute(
            select(RadarCandidateRecord)
            .where(RadarCandidateRecord.scan_id == scan.scan_id)
            .order_by(RadarCandidateRecord.rank.asc())
        ).scalars().all()

        return {
            "ok": True,
            "scan": radar_scan_record_asdict(scan),
            "candidates": [
                radar_candidate_record_asdict(candidate, include_details=include_details)
                for candidate in candidates
            ],
        }
