import asyncio

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from backend.app.db.models import BackgroundTaskRecord, Base
from backend.app.db.session import build_engine, session_scope
from backend.app.workers.task_registry import TaskRegistry, TaskState
from backend.app.workers.task_runner import AsyncTaskRunner


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


def test_task_registry_persists_state_changes(tmp_path):
    db_path = tmp_path / "tasks.db"
    engine = build_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)
    registry = TaskRegistry(session_factory=SessionLocal)

    task = registry.create(kind="radar_scan", metadata={"force_refresh": True})
    registry.mark_running(task.task_id)
    registry.mark_succeeded(task.task_id, {"scan_id": "scan-1", "top50_count": 50})

    with session_scope(SessionLocal) as session:
        row = session.execute(
            select(BackgroundTaskRecord).where(BackgroundTaskRecord.task_id == task.task_id)
        ).scalar_one()

    assert row.kind == "radar_scan"
    assert row.state == "succeeded"
    assert row.payload_json == {"force_refresh": True}
    assert row.result_json == {"scan_id": "scan-1", "top50_count": 50}
    assert row.completed_at is not None


def test_async_task_runner_marks_task_succeeded():
    async def scenario():
        registry = TaskRegistry()
        runner = AsyncTaskRunner(registry)

        async def handler():
            return {"decision": "WAIT"}

        task = runner.submit("ai_strategy", {}, handler)
        for _ in range(10):
            await asyncio.sleep(0)
            if registry.get(task.task_id).state == TaskState.SUCCEEDED:
                break

        finished = registry.get(task.task_id)
        assert finished.state == TaskState.SUCCEEDED
        assert finished.result == {"decision": "WAIT"}

    asyncio.run(scenario())


def test_async_task_runner_marks_task_failed():
    async def scenario():
        registry = TaskRegistry()
        runner = AsyncTaskRunner(registry)

        async def handler():
            raise TimeoutError("provider_timeout")

        task = runner.submit("ai_strategy", {}, handler)
        for _ in range(10):
            await asyncio.sleep(0)
            if registry.get(task.task_id).state == TaskState.FAILED:
                break

        finished = registry.get(task.task_id)
        assert finished.state == TaskState.FAILED
        assert "provider_timeout" in finished.error

    asyncio.run(scenario())
