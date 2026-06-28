from __future__ import annotations

from fastapi import APIRouter

from backend.radar.radar_engine import radar_engine

router = APIRouter(prefix="/api/v2/radar", tags=["radar"])


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
