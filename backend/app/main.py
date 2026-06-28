from __future__ import annotations

from typing import Any

from fastapi import FastAPI

from backend.app.api import ai, dashboard, health, radar, tasks
from backend.app.db.session import SessionLocal, init_db
from backend.app.workers.task_registry import TaskRegistry
from backend.app.workers.task_runner import AsyncTaskRunner


def create_app(
    task_registry: TaskRegistry | None = None,
    task_runner: Any | None = None,
    session_factory: Any | None = None,
    *,
    initialize_database: bool = True,
) -> FastAPI:
    active_session_factory = session_factory or SessionLocal
    if initialize_database:
        bind = getattr(active_session_factory, "kw", {}).get("bind")
        init_db(bind)
    registry = task_registry or TaskRegistry(session_factory=active_session_factory)
    app = FastAPI(
        title="AI Radar API",
        version="2.0-foundation",
        docs_url="/api/v2/docs",
        redoc_url="/api/v2/redoc",
    )
    app.state.session_factory = active_session_factory
    app.state.task_registry = registry
    app.state.task_runner = task_runner or AsyncTaskRunner(registry)
    app.include_router(health.router)
    app.include_router(tasks.router)
    app.include_router(radar.router)
    app.include_router(dashboard.router)
    app.include_router(ai.router)
    return app


app = create_app()
