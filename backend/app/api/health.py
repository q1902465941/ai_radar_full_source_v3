from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(prefix="/api/v2", tags=["health"])


@router.get("/health")
async def health() -> dict[str, object]:
    return {
        "ok": True,
        "service": "ai-radar-api",
        "version": "v2",
    }
