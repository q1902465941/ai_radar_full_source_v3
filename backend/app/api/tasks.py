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
