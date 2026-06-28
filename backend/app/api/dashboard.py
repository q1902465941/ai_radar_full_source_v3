from __future__ import annotations

from fastapi import APIRouter

from backend.app.services.dashboard import build_dashboard_overview
from backend.radar.radar_engine import radar_engine

router = APIRouter(prefix="/api/v2/dashboard", tags=["dashboard"])


@router.get("/overview")
async def dashboard_overview() -> dict[str, object]:
    return build_dashboard_overview(radar_engine)
