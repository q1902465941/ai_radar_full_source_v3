from __future__ import annotations

from fastapi import Request

from backend.app.workers.task_registry import TaskRegistry
from backend.app.workers.task_runner import AsyncTaskRunner


def get_task_registry(request: Request) -> TaskRegistry:
    return request.app.state.task_registry


def get_task_runner(request: Request) -> AsyncTaskRunner:
    return request.app.state.task_runner
