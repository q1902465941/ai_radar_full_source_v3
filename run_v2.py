from __future__ import annotations

import uvicorn

from backend.config import settings


if __name__ == "__main__":
    uvicorn.run(
        "backend.app.main:app",
        host=settings.app_host,
        port=settings.app_port + 1,
        reload=False,
    )
