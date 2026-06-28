from __future__ import annotations

from fastapi import Request

from backend.app.workers.task_registry import TaskRegistry


def get_task_registry(request: Request) -> TaskRegistry:
    return request.app.state.task_registry
