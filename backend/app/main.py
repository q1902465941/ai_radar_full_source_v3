from __future__ import annotations

from fastapi import FastAPI

from backend.app.api import dashboard, health, radar, tasks
from backend.app.workers.task_registry import TaskRegistry


def create_app(task_registry: TaskRegistry | None = None) -> FastAPI:
    app = FastAPI(
        title="AI Radar API",
        version="2.0-foundation",
        docs_url="/api/v2/docs",
        redoc_url="/api/v2/redoc",
    )
    app.state.task_registry = task_registry or TaskRegistry()
    app.include_router(health.router)
    app.include_router(tasks.router)
    app.include_router(radar.router)
    app.include_router(dashboard.router)
    return app


app = create_app()
