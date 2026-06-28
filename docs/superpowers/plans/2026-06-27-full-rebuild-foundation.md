# Full Rebuild Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the first foundation slice of the preserved-content rebuild: a parallel FastAPI v2 app, task registry, database migration foundation, and independent React frontend shell while keeping the current project runnable.

**Architecture:** The first phase adds new code beside the current system. Existing modules under `backend/radar`, `backend/trading`, `backend/positions`, `backend/learning`, `backend/market`, `backend/exchange`, and `backend/ai_strategy` remain the behavioral source of truth. New routers and services expose small v2 surfaces that can be tested without deleting current Jinja pages or changing trading behavior.

**Tech Stack:** FastAPI, Pydantic, SQLAlchemy 2.0, Alembic, SQLite-compatible development database, Vite, React, TypeScript, Vitest, pytest.

## Global Constraints

- Preserve current project content and runtime behavior.
- Do not replace radar scoring, trading rules, risk-control rules, exchange behavior, or AI provider clients.
- Do not enable live trading by default.
- Do not remove current Jinja templates or current static assets.
- Do not delete current SQLite data.
- New slow-work APIs must use task status patterns rather than blocking page interactions.
- API tokens and exchange secrets must stay server-side.
- AI decisions must remain suggestions and cannot bypass deterministic risk controls.
- Current `E:\ai_radar_full_source_v3` is not a Git repository. For commit steps, run `git -C E:\ai_radar_full_source_v3 status --short`; if it returns `fatal: not a git repository`, skip the commit and continue. Do not initialize Git unless the user explicitly asks.

---

## File Structure

Create these new backend foundation files:

- `backend/app/__init__.py`: marks the new backend application package.
- `backend/app/main.py`: v2 FastAPI app factory and app instance.
- `backend/app/api/__init__.py`: API router package marker.
- `backend/app/api/health.py`: health endpoint for the v2 app.
- `backend/app/api/tasks.py`: task status endpoint backed by the task registry.
- `backend/app/api/radar.py`: read-only radar summary endpoint that wraps existing radar state.
- `backend/app/core/__init__.py`: core package marker.
- `backend/app/core/dependencies.py`: FastAPI dependency helpers for shared app state.
- `backend/app/workers/__init__.py`: worker package marker.
- `backend/app/workers/task_registry.py`: in-process task registry abstraction.
- `backend/app/db/__init__.py`: database package marker.
- `backend/app/db/session.py`: SQLAlchemy engine and session helpers.
- `backend/app/db/models.py`: initial structured database models.
- `backend/migrations/env.py`: Alembic environment using the new SQLAlchemy metadata.
- `backend/migrations/script.py.mako`: Alembic revision template.
- `backend/migrations/versions/.gitkeep`: keeps the versions directory present.
- `alembic.ini`: Alembic config rooted at `backend/migrations`.
- `run_v2.py`: optional v2 backend launcher. The old `run.py` remains unchanged.

Create these new tests:

- `tests/test_app_foundation.py`: v2 app factory, health route, task endpoint, radar summary route.
- `tests/test_task_registry.py`: task registry state transitions.
- `tests/test_db_foundation.py`: SQLAlchemy metadata and session behavior.

Modify these existing files:

- `requirements.txt`: add SQLAlchemy, Alembic, and psycopg packages.

Create these new frontend files:

- `frontend/package.json`: Vite React TypeScript scripts and dependencies.
- `frontend/index.html`: app mount point.
- `frontend/tsconfig.json`: TypeScript compiler settings.
- `frontend/tsconfig.node.json`: Vite config compiler settings.
- `frontend/vite.config.ts`: Vite and Vitest config.
- `frontend/src/main.tsx`: React entry.
- `frontend/src/App.tsx`: first app shell.
- `frontend/src/api/client.ts`: typed API helper.
- `frontend/src/api/client.test.ts`: Vitest tests for API helper behavior.
- `frontend/src/styles/app.css`: first layout styling.

---

### Task 1: Add Parallel FastAPI v2 App And Health Route

**Files:**
- Create: `backend/app/__init__.py`
- Create: `backend/app/api/__init__.py`
- Create: `backend/app/api/health.py`
- Create: `backend/app/main.py`
- Create: `tests/test_app_foundation.py`
- Create: `run_v2.py`

**Interfaces:**
- Produces: `backend.app.main.create_app() -> fastapi.FastAPI`
- Produces: `backend.app.main.app: fastapi.FastAPI`
- Produces: `GET /api/v2/health -> {"ok": true, "service": "ai-radar-api", "version": "v2"}`
- Consumes: `backend.config.settings.app_host` and `backend.config.settings.app_port` in `run_v2.py`

- [ ] **Step 1: Write the failing health route test**

Add this content to `tests/test_app_foundation.py`:

```python
from fastapi.testclient import TestClient

from backend.app.main import create_app


def test_v2_app_health_route():
    client = TestClient(create_app())

    response = client.get("/api/v2/health")

    assert response.status_code == 200
    assert response.json() == {
        "ok": True,
        "service": "ai-radar-api",
        "version": "v2",
    }
```

- [ ] **Step 2: Run the test to verify it fails**

Run:

```powershell
python -m pytest tests/test_app_foundation.py::test_v2_app_health_route -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'backend.app'`.

- [ ] **Step 3: Create the minimal v2 app package**

Create `backend/app/__init__.py`:

```python
"""Parallel backend application package for the preserved-content rebuild."""
```

Create `backend/app/api/__init__.py`:

```python
"""API routers for the parallel v2 backend."""
```

Create `backend/app/api/health.py`:

```python
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
```

Create `backend/app/main.py`:

```python
from __future__ import annotations

from fastapi import FastAPI

from backend.app.api import health


def create_app() -> FastAPI:
    app = FastAPI(
        title="AI Radar API",
        version="2.0-foundation",
        docs_url="/api/v2/docs",
        redoc_url="/api/v2/redoc",
    )
    app.include_router(health.router)
    return app


app = create_app()
```

Create `run_v2.py`:

```python
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
```

- [ ] **Step 4: Run the health route test to verify it passes**

Run:

```powershell
python -m pytest tests/test_app_foundation.py::test_v2_app_health_route -v
```

Expected: PASS.

- [ ] **Step 5: Run a compile check for the new files**

Run:

```powershell
python -m compileall backend\app run_v2.py
```

Expected: exit code 0.

- [ ] **Step 6: Commit checkpoint**

Run:

```powershell
git -C E:\ai_radar_full_source_v3 status --short
```

Expected in the current folder: `fatal: not a git repository`. Skip the commit. If the project has been moved into a Git repository, run:

```powershell
git -C E:\ai_radar_full_source_v3 add backend/app run_v2.py tests/test_app_foundation.py
git -C E:\ai_radar_full_source_v3 commit -m "feat: add parallel v2 api foundation"
```

---

### Task 2: Add In-Process Task Registry

**Files:**
- Create: `backend/app/workers/__init__.py`
- Create: `backend/app/workers/task_registry.py`
- Create: `tests/test_task_registry.py`

**Interfaces:**
- Produces: `TaskState` enum with `PENDING`, `RUNNING`, `SUCCEEDED`, `FAILED`
- Produces: `TaskRecord` dataclass with `asdict() -> dict[str, object]`
- Produces: `TaskRegistry.create(kind: str, metadata: dict[str, object] | None = None) -> TaskRecord`
- Produces: `TaskRegistry.get(task_id: str) -> TaskRecord | None`
- Produces: `TaskRegistry.mark_running(task_id: str) -> TaskRecord`
- Produces: `TaskRegistry.mark_succeeded(task_id: str, result: dict[str, object] | None = None) -> TaskRecord`
- Produces: `TaskRegistry.mark_failed(task_id: str, error: str) -> TaskRecord`
- Consumes: no existing project state

- [ ] **Step 1: Write failing registry tests**

Create `tests/test_task_registry.py`:

```python
from backend.app.workers.task_registry import TaskRegistry, TaskState


def test_task_registry_creates_pending_task():
    registry = TaskRegistry()

    task = registry.create(kind="radar_scan", metadata={"source": "test"})

    assert task.kind == "radar_scan"
    assert task.state == TaskState.PENDING
    assert task.metadata == {"source": "test"}
    assert registry.get(task.task_id) is task
    assert task.asdict()["state"] == "pending"


def test_task_registry_tracks_successful_task():
    registry = TaskRegistry()
    task = registry.create(kind="ai_strategy")

    running = registry.mark_running(task.task_id)
    assert running.state == TaskState.RUNNING

    succeeded = registry.mark_succeeded(task.task_id, {"decision": "WAIT"})

    assert succeeded.state == TaskState.SUCCEEDED
    assert succeeded.result == {"decision": "WAIT"}
    assert succeeded.completed_at_ms >= succeeded.created_at_ms


def test_task_registry_tracks_failed_task():
    registry = TaskRegistry()
    task = registry.create(kind="ai_strategy")

    failed = registry.mark_failed(task.task_id, "provider_timeout")

    assert failed.state == TaskState.FAILED
    assert failed.error == "provider_timeout"
    assert failed.asdict()["error"] == "provider_timeout"
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```powershell
python -m pytest tests/test_task_registry.py -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'backend.app.workers'`.

- [ ] **Step 3: Implement the registry**

Create `backend/app/workers/__init__.py`:

```python
"""Background task helpers for the parallel v2 backend."""
```

Create `backend/app/workers/task_registry.py`:

```python
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from threading import Lock
from time import time
from uuid import uuid4


def _now_ms() -> int:
    return int(time() * 1000)


class TaskState(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


@dataclass
class TaskRecord:
    task_id: str
    kind: str
    state: TaskState
    created_at_ms: int
    updated_at_ms: int
    completed_at_ms: int | None = None
    metadata: dict[str, object] = field(default_factory=dict)
    result: dict[str, object] | None = None
    error: str = ""

    def asdict(self) -> dict[str, object]:
        return {
            "task_id": self.task_id,
            "kind": self.kind,
            "state": self.state.value,
            "created_at_ms": self.created_at_ms,
            "updated_at_ms": self.updated_at_ms,
            "completed_at_ms": self.completed_at_ms,
            "metadata": self.metadata,
            "result": self.result,
            "error": self.error,
        }


class TaskRegistry:
    def __init__(self) -> None:
        self._tasks: dict[str, TaskRecord] = {}
        self._lock = Lock()

    def create(self, kind: str, metadata: dict[str, object] | None = None) -> TaskRecord:
        now = _now_ms()
        task = TaskRecord(
            task_id=uuid4().hex,
            kind=kind,
            state=TaskState.PENDING,
            created_at_ms=now,
            updated_at_ms=now,
            metadata=dict(metadata or {}),
        )
        with self._lock:
            self._tasks[task.task_id] = task
        return task

    def get(self, task_id: str) -> TaskRecord | None:
        with self._lock:
            return self._tasks.get(task_id)

    def mark_running(self, task_id: str) -> TaskRecord:
        task = self._require(task_id)
        with self._lock:
            task.state = TaskState.RUNNING
            task.updated_at_ms = _now_ms()
        return task

    def mark_succeeded(self, task_id: str, result: dict[str, object] | None = None) -> TaskRecord:
        task = self._require(task_id)
        with self._lock:
            now = _now_ms()
            task.state = TaskState.SUCCEEDED
            task.result = dict(result or {})
            task.error = ""
            task.updated_at_ms = now
            task.completed_at_ms = now
        return task

    def mark_failed(self, task_id: str, error: str) -> TaskRecord:
        task = self._require(task_id)
        with self._lock:
            now = _now_ms()
            task.state = TaskState.FAILED
            task.error = error
            task.updated_at_ms = now
            task.completed_at_ms = now
        return task

    def _require(self, task_id: str) -> TaskRecord:
        task = self.get(task_id)
        if task is None:
            raise KeyError(task_id)
        return task
```

- [ ] **Step 4: Run registry tests to verify they pass**

Run:

```powershell
python -m pytest tests/test_task_registry.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit checkpoint**

Run:

```powershell
git -C E:\ai_radar_full_source_v3 status --short
```

Expected in the current folder: `fatal: not a git repository`. Skip the commit. If Git exists, run:

```powershell
git -C E:\ai_radar_full_source_v3 add backend/app/workers tests/test_task_registry.py
git -C E:\ai_radar_full_source_v3 commit -m "feat: add v2 task registry"
```

---

### Task 3: Expose Task Status Through v2 API

**Files:**
- Create: `backend/app/core/__init__.py`
- Create: `backend/app/core/dependencies.py`
- Create: `backend/app/api/tasks.py`
- Modify: `backend/app/main.py`
- Modify: `tests/test_app_foundation.py`

**Interfaces:**
- Consumes: `TaskRegistry` from Task 2
- Produces: `create_app(task_registry: TaskRegistry | None = None) -> FastAPI`
- Produces: `get_task_registry(request: Request) -> TaskRegistry`
- Produces: `GET /api/v2/tasks/{task_id}`

- [ ] **Step 1: Add failing task status API test**

Append this test to `tests/test_app_foundation.py`:

```python
from backend.app.workers.task_registry import TaskRegistry


def test_v2_task_status_route_returns_registry_task():
    registry = TaskRegistry()
    task = registry.create(kind="radar_scan", metadata={"source": "test"})
    client = TestClient(create_app(task_registry=registry))

    response = client.get(f"/api/v2/tasks/{task.task_id}")

    assert response.status_code == 200
    assert response.json()["task_id"] == task.task_id
    assert response.json()["kind"] == "radar_scan"
    assert response.json()["state"] == "pending"


def test_v2_task_status_route_returns_404_for_missing_task():
    client = TestClient(create_app(task_registry=TaskRegistry()))

    response = client.get("/api/v2/tasks/missing")

    assert response.status_code == 404
    assert response.json()["detail"] == "task_not_found"
```

- [ ] **Step 2: Run task status API tests to verify they fail**

Run:

```powershell
python -m pytest tests/test_app_foundation.py::test_v2_task_status_route_returns_registry_task tests/test_app_foundation.py::test_v2_task_status_route_returns_404_for_missing_task -v
```

Expected: FAIL with `TypeError: create_app() got an unexpected keyword argument 'task_registry'`.

- [ ] **Step 3: Add dependencies and tasks router**

Create `backend/app/core/__init__.py`:

```python
"""Core wiring helpers for the parallel v2 backend."""
```

Create `backend/app/core/dependencies.py`:

```python
from __future__ import annotations

from fastapi import Request

from backend.app.workers.task_registry import TaskRegistry


def get_task_registry(request: Request) -> TaskRegistry:
    return request.app.state.task_registry
```

Create `backend/app/api/tasks.py`:

```python
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from backend.app.core.dependencies import get_task_registry
from backend.app.workers.task_registry import TaskRegistry

router = APIRouter(prefix="/api/v2/tasks", tags=["tasks"])


@router.get("/{task_id}")
async def task_status(
    task_id: str,
    registry: TaskRegistry = Depends(get_task_registry),
) -> dict[str, object]:
    task = registry.get(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="task_not_found")
    return task.asdict()
```

Update `backend/app/main.py`:

```python
from __future__ import annotations

from fastapi import FastAPI

from backend.app.api import health, tasks
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
    return app


app = create_app()
```

- [ ] **Step 4: Run task status API tests to verify they pass**

Run:

```powershell
python -m pytest tests/test_app_foundation.py::test_v2_task_status_route_returns_registry_task tests/test_app_foundation.py::test_v2_task_status_route_returns_404_for_missing_task -v
```

Expected: PASS.

- [ ] **Step 5: Run all foundation app tests**

Run:

```powershell
python -m pytest tests/test_app_foundation.py -v
```

Expected: PASS.

- [ ] **Step 6: Commit checkpoint**

Run:

```powershell
git -C E:\ai_radar_full_source_v3 status --short
```

Expected in the current folder: `fatal: not a git repository`. Skip the commit. If Git exists, run:

```powershell
git -C E:\ai_radar_full_source_v3 add backend/app tests/test_app_foundation.py
git -C E:\ai_radar_full_source_v3 commit -m "feat: expose v2 task status api"
```

---

### Task 4: Add Read-Only Radar v2 Summary API

**Files:**
- Create: `backend/app/api/radar.py`
- Modify: `backend/app/main.py`
- Modify: `tests/test_app_foundation.py`

**Interfaces:**
- Consumes: existing `backend.radar.radar_engine.radar_engine`
- Produces: `GET /api/v2/radar/summary`
- Response shape: `{"ok": true, "top50_count": int, "top4_count": int, "last_scan_id": str, "last_scan_time": str, "market_heat": float, "alert_count": int, "scan_status": dict}`

- [ ] **Step 1: Add failing radar summary test**

Append this test to `tests/test_app_foundation.py`:

```python
import backend.app.api.radar as radar_api


def test_v2_radar_summary_uses_existing_radar_state(monkeypatch):
    class FakeRadarEngine:
        top50 = [object(), object(), object()]
        top4 = [object()]
        last_scan_id = "scan-test"
        last_scan_time = "2026-06-27 12:00:00"
        market_heat = 64
        alert_count = 5

        def scan_status(self):
            return {"in_progress": False, "active_coins": {"active_count": 3}}

    monkeypatch.setattr(radar_api, "radar_engine", FakeRadarEngine())
    client = TestClient(create_app())

    response = client.get("/api/v2/radar/summary")

    assert response.status_code == 200
    assert response.json() == {
        "ok": True,
        "top50_count": 3,
        "top4_count": 1,
        "last_scan_id": "scan-test",
        "last_scan_time": "2026-06-27 12:00:00",
        "market_heat": 64,
        "alert_count": 5,
        "scan_status": {"in_progress": False, "active_coins": {"active_count": 3}},
    }
```

- [ ] **Step 2: Run the radar summary test to verify it fails**

Run:

```powershell
python -m pytest tests/test_app_foundation.py::test_v2_radar_summary_uses_existing_radar_state -v
```

Expected: FAIL with `ImportError` for `backend.app.api.radar` or a 404 response.

- [ ] **Step 3: Add the read-only radar router**

Create `backend/app/api/radar.py`:

```python
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
```

Update `backend/app/main.py`:

```python
from __future__ import annotations

from fastapi import FastAPI

from backend.app.api import health, radar, tasks
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
    return app


app = create_app()
```

- [ ] **Step 4: Run the radar summary test to verify it passes**

Run:

```powershell
python -m pytest tests/test_app_foundation.py::test_v2_radar_summary_uses_existing_radar_state -v
```

Expected: PASS.

- [ ] **Step 5: Run all new backend foundation tests**

Run:

```powershell
python -m pytest tests/test_app_foundation.py tests/test_task_registry.py -v
```

Expected: PASS.

- [ ] **Step 6: Commit checkpoint**

Run:

```powershell
git -C E:\ai_radar_full_source_v3 status --short
```

Expected in the current folder: `fatal: not a git repository`. Skip the commit. If Git exists, run:

```powershell
git -C E:\ai_radar_full_source_v3 add backend/app tests/test_app_foundation.py
git -C E:\ai_radar_full_source_v3 commit -m "feat: add read-only v2 radar summary"
```

---

### Task 5: Add SQLAlchemy And Alembic Foundation

**Files:**
- Modify: `requirements.txt`
- Create: `backend/app/db/__init__.py`
- Create: `backend/app/db/session.py`
- Create: `backend/app/db/models.py`
- Create: `backend/migrations/env.py`
- Create: `backend/migrations/script.py.mako`
- Create: `backend/migrations/versions/.gitkeep`
- Create: `alembic.ini`
- Create: `tests/test_db_foundation.py`

**Interfaces:**
- Produces: `backend.app.db.models.Base`
- Produces: `BackgroundTaskRecord` SQLAlchemy model mapped to `background_tasks`
- Produces: `AITaskRecord` SQLAlchemy model mapped to `ai_tasks`
- Produces: `build_engine(database_url: str | None = None) -> sqlalchemy.Engine`
- Produces: `session_scope(session_factory: sessionmaker[Session]) -> Iterator[Session]`
- Consumes: `backend.config.settings.db_path`

- [ ] **Step 1: Add failing database foundation tests**

Create `tests/test_db_foundation.py`:

```python
from sqlalchemy import inspect, select
from sqlalchemy.orm import sessionmaker

from backend.app.db.models import AITaskRecord, BackgroundTaskRecord, Base
from backend.app.db.session import build_engine, session_scope


def test_database_foundation_creates_task_tables(tmp_path):
    db_path = tmp_path / "foundation.db"
    engine = build_engine(f"sqlite:///{db_path}")

    Base.metadata.create_all(engine)

    tables = set(inspect(engine).get_table_names())
    assert "background_tasks" in tables
    assert "ai_tasks" in tables


def test_session_scope_commits_records(tmp_path):
    db_path = tmp_path / "foundation.db"
    engine = build_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)

    with session_scope(SessionLocal) as session:
        session.add(
            BackgroundTaskRecord(
                task_id="task-1",
                kind="radar_scan",
                state="succeeded",
                payload_json={"source": "test"},
            )
        )

    with session_scope(SessionLocal) as session:
        row = session.execute(
            select(BackgroundTaskRecord).where(BackgroundTaskRecord.task_id == "task-1")
        ).scalar_one()

    assert row.kind == "radar_scan"
    assert row.state == "succeeded"
    assert row.payload_json == {"source": "test"}
```

- [ ] **Step 2: Run database tests to verify they fail**

Run:

```powershell
python -m pytest tests/test_db_foundation.py -v
```

Expected before dependencies are installed: FAIL with `ModuleNotFoundError: No module named 'sqlalchemy'`.

- [ ] **Step 3: Add database dependencies**

Append these lines to `requirements.txt`:

```text
sqlalchemy==2.0.36
alembic==1.14.0
psycopg[binary]==3.2.3
```

Install dependencies:

```powershell
pip install -r requirements.txt
```

Expected: exit code 0.

- [ ] **Step 4: Implement database session and models**

Create `backend/app/db/__init__.py`:

```python
"""Database foundation for the parallel v2 backend."""
```

Create `backend/app/db/session.py`:

```python
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from backend.config import settings


def _default_database_url() -> str:
    return f"sqlite:///{settings.db_path}"


def build_engine(database_url: str | None = None) -> Engine:
    url = database_url or _default_database_url()
    connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
    return create_engine(url, connect_args=connect_args, future=True)


@contextmanager
def session_scope(session_factory: sessionmaker[Session]) -> Iterator[Session]:
    session = session_factory()
    session.expire_on_commit = False
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
```

Create `backend/app/db/models.py`:

```python
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import DateTime, Integer, JSON, String
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class BackgroundTaskRecord(Base):
    __tablename__ = "background_tasks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    kind: Mapped[str] = mapped_column(String(64), index=True)
    state: Mapped[str] = mapped_column(String(32), index=True)
    error: Mapped[str] = mapped_column(String(1000), default="")
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    result_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AITaskRecord(Base):
    __tablename__ = "ai_tasks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    provider: Mapped[str] = mapped_column(String(64), default="")
    model: Mapped[str] = mapped_column(String(128), default="")
    state: Mapped[str] = mapped_column(String(32), index=True)
    prompt_summary: Mapped[str] = mapped_column(String(2000), default="")
    context_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    output_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    validation_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    error: Mapped[str] = mapped_column(String(1000), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
```

- [ ] **Step 5: Add Alembic foundation files**

Create `alembic.ini`:

```ini
[alembic]
script_location = backend/migrations
prepend_sys_path = .
sqlalchemy.url = sqlite:///data/ai_radar.db

[loggers]
keys = root,sqlalchemy,alembic

[handlers]
keys = console

[formatters]
keys = generic

[logger_root]
level = WARN
handlers = console
qualname =

[logger_sqlalchemy]
level = WARN
handlers =
qualname = sqlalchemy.engine

[logger_alembic]
level = INFO
handlers =
qualname = alembic

[handler_console]
class = StreamHandler
args = (sys.stderr,)
level = NOTSET
formatter = generic

[formatter_generic]
format = %(levelname)-5.5s [%(name)s] %(message)s
datefmt = %H:%M:%S
```

Create `backend/migrations/env.py`:

```python
from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from backend.app.db.models import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
```

Create `backend/migrations/script.py.mako`:

```python
"""${message}

Revision ID: ${up_revision}
Revises: ${down_revision | comma,n}
Create Date: ${create_date}
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

${imports if imports else ""}

revision = ${repr(up_revision)}
down_revision = ${repr(down_revision)}
branch_labels = ${repr(branch_labels)}
depends_on = ${repr(depends_on)}


def upgrade() -> None:
    ${upgrades if upgrades else "pass"}


def downgrade() -> None:
    ${downgrades if downgrades else "pass"}
```

Create an empty `backend/migrations/versions/.gitkeep` file.

- [ ] **Step 6: Run database tests to verify they pass**

Run:

```powershell
python -m pytest tests/test_db_foundation.py -v
```

Expected: PASS.

- [ ] **Step 7: Run Alembic metadata check**

Run:

```powershell
python -m alembic current
```

Expected: exit code 0. If the `data` directory does not exist, create it with:

```powershell
New-Item -ItemType Directory -Force data | Out-Null
python -m alembic current
```

Expected after creating `data`: exit code 0.

- [ ] **Step 8: Commit checkpoint**

Run:

```powershell
git -C E:\ai_radar_full_source_v3 status --short
```

Expected in the current folder: `fatal: not a git repository`. Skip the commit. If Git exists, run:

```powershell
git -C E:\ai_radar_full_source_v3 add requirements.txt backend/app/db backend/migrations alembic.ini tests/test_db_foundation.py
git -C E:\ai_radar_full_source_v3 commit -m "feat: add database migration foundation"
```

---

### Task 6: Add Independent React Frontend Shell

**Files:**
- Create: `frontend/package.json`
- Create: `frontend/index.html`
- Create: `frontend/tsconfig.json`
- Create: `frontend/tsconfig.node.json`
- Create: `frontend/vite.config.ts`
- Create: `frontend/src/main.tsx`
- Create: `frontend/src/App.tsx`
- Create: `frontend/src/api/client.ts`
- Create: `frontend/src/api/client.test.ts`
- Create: `frontend/src/styles/app.css`

**Interfaces:**
- Produces: `apiGet<T>(path: string, init?: RequestInit) -> Promise<T>`
- Produces: frontend routes for dashboard, radar, positions, strategy AI, and settings as shell navigation labels
- Consumes: `GET /api/v2/health`

- [ ] **Step 1: Create frontend API test first**

Create `frontend/package.json`:

```json
{
  "name": "ai-radar-frontend",
  "version": "0.1.0",
  "private": true,
  "type": "module",
  "scripts": {
    "dev": "vite --host 127.0.0.1 --port 5173",
    "build": "tsc -b && vite build",
    "test": "vitest"
  },
  "dependencies": {
    "react": "19.2.7",
    "react-dom": "19.2.7"
  },
  "devDependencies": {
    "@types/node": "26.0.1",
    "@types/react": "19.2.17",
    "@types/react-dom": "19.2.3",
    "@vitejs/plugin-react": "6.0.3",
    "jsdom": "29.1.1",
    "typescript": "6.0.3",
    "vite": "8.1.0",
    "vitest": "4.1.9"
  }
}
```

Create `frontend/src/api/client.test.ts`:

```typescript
import { describe, expect, it, vi } from 'vitest';
import { apiGet } from './client';

describe('apiGet', () => {
  it('prefixes v2 API paths and parses JSON responses', async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      text: async () => JSON.stringify({ ok: true, version: 'v2' }),
    });
    vi.stubGlobal('fetch', fetchMock);

    const data = await apiGet<{ ok: boolean; version: string }>('/health');

    expect(fetchMock).toHaveBeenCalledWith('/api/v2/health', { headers: new Headers() });
    expect(data).toEqual({ ok: true, version: 'v2' });
  });

  it('throws backend detail messages for failed responses', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
      ok: false,
      status: 401,
      text: async () => JSON.stringify({ detail: 'invalid_api_token' }),
    }));

    await expect(apiGet('/health')).rejects.toThrow('invalid_api_token');
  });
});
```

- [ ] **Step 2: Install frontend dependencies and verify the test fails**

Run:

```powershell
cd frontend
npm install
npm test -- --run src/api/client.test.ts
```

Expected: FAIL with `Failed to load url ./client`.

- [ ] **Step 3: Implement the API client**

Create `frontend/src/api/client.ts`:

```typescript
const API_PREFIX = '/api/v2';

type JsonObject = Record<string, unknown>;

function buildHeaders(init?: RequestInit): Headers {
  const headers = new Headers(init?.headers || {});
  const token = window.localStorage.getItem('api_token') || '';
  if (token && !headers.has('X-API-Token')) {
    headers.set('X-API-Token', token);
  }
  return headers;
}

function readError(data: JsonObject, fallback: string): string {
  const detail = data.detail;
  const error = data.error;
  if (typeof detail === 'string') return detail;
  if (typeof error === 'string') return error;
  return fallback;
}

export async function apiGet<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_PREFIX}${path}`, {
    ...init,
    headers: buildHeaders(init),
  });
  const text = await response.text();
  const data = text ? JSON.parse(text) as JsonObject : {};
  if (!response.ok) {
    throw new Error(readError(data, `HTTP ${response.status}`));
  }
  return data as T;
}
```

- [ ] **Step 4: Run frontend API tests to verify they pass**

Run:

```powershell
cd frontend
npm test -- --run src/api/client.test.ts
```

Expected: PASS.

- [ ] **Step 5: Add the React app shell**

Create `frontend/index.html`:

```html
<!doctype html>
<html lang="zh-CN">
  <head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0" />
    <title>AI Radar</title>
  </head>
  <body>
    <div id="root"></div>
    <script type="module" src="/src/main.tsx"></script>
  </body>
</html>
```

Create `frontend/tsconfig.json`:

```json
{
  "compilerOptions": {
    "target": "ES2020",
    "useDefineForClassFields": true,
    "lib": ["DOM", "DOM.Iterable", "ESNext"],
    "allowJs": false,
    "skipLibCheck": true,
    "esModuleInterop": true,
    "allowSyntheticDefaultImports": true,
    "strict": true,
    "forceConsistentCasingInFileNames": true,
    "module": "ESNext",
    "moduleResolution": "Bundler",
    "resolveJsonModule": true,
    "isolatedModules": true,
    "noEmit": true,
    "jsx": "react-jsx",
    "types": ["vite/client"]
  },
  "include": ["src"],
  "exclude": ["src/**/*.test.ts"],
  "references": [{ "path": "./tsconfig.node.json" }]
}
```

Create `frontend/tsconfig.node.json`:

```json
{
  "compilerOptions": {
    "composite": true,
    "lib": ["ESNext"],
    "module": "ESNext",
    "moduleResolution": "Bundler",
    "allowSyntheticDefaultImports": true,
    "types": ["node"]
  },
  "include": ["vite.config.ts"]
}
```

Create `frontend/vite.config.ts`:

```typescript
import react from '@vitejs/plugin-react';
import { defineConfig } from 'vite';

export default defineConfig({
  plugins: [react()],
  server: {
    host: '127.0.0.1',
    port: 5173,
    proxy: {
      '/api': 'http://127.0.0.1:8001',
    },
  },
  test: {
    environment: 'jsdom',
  },
});
```

Create `frontend/src/main.tsx`:

```typescript
import React from 'react';
import ReactDOM from 'react-dom/client';
import { App } from './App';
import './styles/app.css';

ReactDOM.createRoot(document.getElementById('root') as HTMLElement).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
);
```

Create `frontend/src/App.tsx`:

```typescript
import { useEffect, useState } from 'react';
import { apiGet } from './api/client';

type Health = {
  ok: boolean;
  service: string;
  version: string;
};

const navItems = [
  ['Overview', 'Dashboard'],
  ['Radar', 'Markets'],
  ['Positions', 'Portfolio'],
  ['Strategy AI', 'AI Insight'],
  ['Settings', 'Control'],
] as const;

export function App() {
  const [health, setHealth] = useState<Health | null>(null);
  const [error, setError] = useState('');

  useEffect(() => {
    let active = true;
    apiGet<Health>('/health')
      .then((data) => {
        if (active) setHealth(data);
      })
      .catch((err: unknown) => {
        if (active) setError(err instanceof Error ? err.message : String(err));
      });
    return () => {
      active = false;
    };
  }, []);

  return (
    <div className="app-shell">
      <aside className="sidebar">
        <div className="brand-mark">AI</div>
        <nav>
          {navItems.map(([label, caption]) => (
            <button className="nav-item" key={label} type="button">
              <span>{label}</span>
              <small>{caption}</small>
            </button>
          ))}
        </nav>
      </aside>
      <main className="workspace">
        <header className="workspace-header">
          <div>
            <p>Preserved Rebuild</p>
            <h1>AI Radar Control Center</h1>
          </div>
          <span className={health?.ok ? 'status online' : 'status'}>
            {health?.ok ? `${health.service} ${health.version}` : error || 'Connecting'}
          </span>
        </header>
        <section className="panel-grid">
          <article>
            <span>System Mode</span>
            <strong>Paper</strong>
            <p>Live trading remains guarded by the backend.</p>
          </article>
          <article>
            <span>Migration State</span>
            <strong>Foundation</strong>
            <p>Current pages and APIs remain available during the rebuild.</p>
          </article>
          <article>
            <span>Next Data Source</span>
            <strong>v2 API</strong>
            <p>Radar, AI, positions, and settings migrate behind task APIs.</p>
          </article>
        </section>
      </main>
    </div>
  );
}
```

Create `frontend/src/styles/app.css`:

```css
:root {
  color: #e8edf7;
  background: #080b12;
  font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
}

* {
  box-sizing: border-box;
}

body {
  margin: 0;
  min-width: 320px;
  min-height: 100vh;
}

button {
  font: inherit;
}

.app-shell {
  display: grid;
  grid-template-columns: 240px minmax(0, 1fr);
  min-height: 100vh;
  background:
    radial-gradient(circle at 20% 10%, rgba(90, 110, 255, 0.22), transparent 28rem),
    linear-gradient(135deg, #080b12 0%, #111722 58%, #07100d 100%);
}

.sidebar {
  border-right: 1px solid rgba(255, 255, 255, 0.08);
  padding: 24px 18px;
  background: rgba(7, 10, 17, 0.74);
}

.brand-mark {
  display: grid;
  place-items: center;
  width: 44px;
  height: 44px;
  margin-bottom: 32px;
  border-radius: 8px;
  background: #7b85ff;
  color: #fff;
  font-weight: 800;
}

nav {
  display: grid;
  gap: 8px;
}

.nav-item {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 4px;
  width: 100%;
  min-height: 56px;
  border: 1px solid rgba(255, 255, 255, 0.08);
  border-radius: 8px;
  padding: 10px 12px;
  color: #e8edf7;
  background: rgba(255, 255, 255, 0.04);
}

.nav-item small {
  color: #8e98ad;
}

.workspace {
  padding: 28px;
}

.workspace-header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 24px;
  margin-bottom: 24px;
}

.workspace-header p {
  margin: 0 0 6px;
  color: #8e98ad;
}

.workspace-header h1 {
  margin: 0;
  font-size: 32px;
  letter-spacing: 0;
}

.status {
  border: 1px solid rgba(255, 255, 255, 0.12);
  border-radius: 999px;
  padding: 8px 12px;
  color: #aeb8ca;
  background: rgba(255, 255, 255, 0.05);
}

.status.online {
  border-color: rgba(92, 214, 141, 0.35);
  color: #8df0aa;
}

.panel-grid {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 16px;
}

.panel-grid article {
  min-height: 160px;
  border: 1px solid rgba(255, 255, 255, 0.09);
  border-radius: 8px;
  padding: 18px;
  background: rgba(16, 22, 34, 0.82);
}

.panel-grid span {
  display: block;
  color: #8e98ad;
  margin-bottom: 16px;
}

.panel-grid strong {
  display: block;
  font-size: 26px;
  margin-bottom: 12px;
}

.panel-grid p {
  margin: 0;
  color: #aeb8ca;
  line-height: 1.5;
}

@media (max-width: 820px) {
  .app-shell {
    grid-template-columns: 1fr;
  }

  .sidebar {
    border-right: 0;
    border-bottom: 1px solid rgba(255, 255, 255, 0.08);
  }

  nav,
  .panel-grid {
    grid-template-columns: 1fr;
  }

  .workspace-header {
    flex-direction: column;
  }
}
```

- [ ] **Step 6: Run frontend tests and build**

Run:

```powershell
cd frontend
npm test -- --run src/api/client.test.ts
npm run build
```

Expected: both commands exit 0.

- [ ] **Step 7: Commit checkpoint**

Run:

```powershell
git -C E:\ai_radar_full_source_v3 status --short
```

Expected in the current folder: `fatal: not a git repository`. Skip the commit. If Git exists, run:

```powershell
git -C E:\ai_radar_full_source_v3 add frontend
git -C E:\ai_radar_full_source_v3 commit -m "feat: add independent frontend shell"
```

---

### Task 7: Verify Foundation Without Breaking Legacy App

**Files:**
- Modify: no source files
- Test: backend and frontend verification commands

**Interfaces:**
- Consumes: all outputs from Tasks 1 through 6
- Produces: verification evidence that old and new entry points can coexist

- [ ] **Step 1: Run targeted new backend tests**

Run:

```powershell
python -m pytest tests/test_app_foundation.py tests/test_task_registry.py tests/test_db_foundation.py -v
```

Expected: PASS.

- [ ] **Step 2: Run legacy-sensitive existing tests**

Run:

```powershell
python -m pytest tests/test_core.py::test_radar_scan_helper_uses_current_event_loop tests/test_core.py::test_sensitive_config_post_requires_api_token_when_unconfigured tests/test_core.py::test_sensitive_config_post_accepts_valid_api_token -v
```

Expected: PASS.

- [ ] **Step 3: Run compile checks**

Run:

```powershell
python -m compileall backend run.py run_v2.py
```

Expected: exit code 0.

- [ ] **Step 4: Run frontend verification**

Run:

```powershell
cd frontend
npm test -- --run src/api/client.test.ts
npm run build
```

Expected: both commands exit 0.

- [ ] **Step 5: Confirm legacy entry remains unchanged**

Run:

```powershell
Get-Content -Raw run.py
```

Expected exact content:

```python
from backend.main import run

if __name__ == "__main__":
    run()
```

- [ ] **Step 6: Commit checkpoint**

Run:

```powershell
git -C E:\ai_radar_full_source_v3 status --short
```

Expected in the current folder: `fatal: not a git repository`. Skip the commit. If Git exists, run:

```powershell
git -C E:\ai_radar_full_source_v3 add .
git -C E:\ai_radar_full_source_v3 commit -m "test: verify foundation coexists with legacy app"
```

---

## Self-Review Notes

- Spec coverage: This plan implements Phase 1 foundation from the design. It does not migrate dashboard, radar page, positions, trading controls, AI tasks, or full database data; those are separate phases with their own plans after the foundation is verified.
- Red-flag scan: The plan contains no unresolved markers, unnamed validation, or unspecified test commands.
- Type consistency: `TaskRegistry`, `TaskRecord`, `TaskState`, `create_app`, `apiGet`, `BackgroundTaskRecord`, and `AITaskRecord` are introduced before any task consumes them.
- Legacy safety: The plan does not modify `backend/main.py`, existing templates, existing static assets, or existing trading modules.
