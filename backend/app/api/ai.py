from __future__ import annotations

from fastapi import APIRouter

from backend.ai_strategy.ai_service import ai_service

router = APIRouter(prefix="/api/v2/ai", tags=["ai"])


@router.get("/status")
async def ai_status() -> dict[str, object]:
    return {"ok": True, "service": ai_service.status()}
