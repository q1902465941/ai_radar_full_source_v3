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
