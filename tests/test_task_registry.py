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
