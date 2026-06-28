from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from backend.app.workers.task_registry import TaskRecord, TaskRegistry

TaskHandler = Callable[[], Awaitable[dict[str, Any] | None]]


class AsyncTaskRunner:
    def __init__(self, registry: TaskRegistry) -> None:
        self._registry = registry

    def submit(
        self,
        kind: str,
        metadata: dict[str, object] | None,
        handler: TaskHandler,
    ) -> TaskRecord:
        task = self._registry.create(kind=kind, metadata=metadata)
        asyncio.create_task(self._run(task.task_id, handler), name=f"{kind}-{task.task_id}")
        return task

    async def _run(self, task_id: str, handler: TaskHandler) -> None:
        self._registry.mark_running(task_id)
        try:
            result = await handler()
        except Exception as exc:
            self._registry.mark_failed(task_id, f"{type(exc).__name__}:{exc}")
            return
        self._registry.mark_succeeded(task_id, result or {})
