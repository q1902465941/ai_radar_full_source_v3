from __future__ import annotations

from fastapi import APIRouter

from backend.app.services.dashboard import build_dashboard_overview, build_dashboard_overview_from_live_payload
from backend.app.services.legacy_monitor_source import fetch_live_radar_payload
from backend.radar.radar_engine import radar_engine

router = APIRouter(prefix="/api/v2/dashboard", tags=["dashboard"])


@router.get("/overview")
async def dashboard_overview() -> dict[str, object]:
    live_payload = await fetch_live_radar_payload()
    if live_payload is not None:
        return build_dashboard_overview_from_live_payload(live_payload)
    return build_dashboard_overview(radar_engine)
