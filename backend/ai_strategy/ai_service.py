from __future__ import annotations

from dataclasses import asdict, is_dataclass
from typing import Any
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from backend.ai_strategy.openai_strategy_client import openai_strategy_client
from backend.app.db.models import AITaskRecord, utc_now
from backend.app.db.session import SessionLocal, session_scope
from backend.models import StrategyPlan


class AIService:
    def __init__(
        self,
        *,
        strategy_client: Any | None = None,
        session_factory: sessionmaker[Session] | None = None,
    ) -> None:
        self._strategy_client = strategy_client or openai_strategy_client
        self._session_factory = session_factory or SessionLocal

    async def generate_strategy(self, item: Any, position_context: dict | None = None) -> StrategyPlan:
        status = self.status(candidate_count=1, candidate_source=str((position_context or {}).get("source") or ""))
        provider = str(status.get("provider") or "")
        model = _model_from_status(status, provider)
        row = self._create_task(
            provider=provider,
            model=model,
            prompt_summary=f"strategy_plan:{getattr(item, 'symbol', '')}",
            context={
                "symbol": getattr(item, "symbol", ""),
                "direction": getattr(item, "direction", ""),
                "price": getattr(item, "price", 0),
                "score": getattr(item, "score", 0),
                "position_context": dict(position_context or {}),
            },
        )
        try:
            plan = await self._strategy_client.generate(item, position_context)
        except Exception as exc:
            self._mark_failed(row.task_id, f"{type(exc).__name__}:{exc}")
            raise

        plan_json = _jsonable_dataclass(plan)
        self._mark_succeeded(
            row.task_id,
            output=plan_json,
            validation={
                "valid": True,
                "action": plan.action,
                "symbol": plan.symbol,
                "side": plan.side,
            },
        )
        return plan

    def status(self, **kwargs: Any) -> dict[str, Any]:
        base = dict(self._strategy_client.status(**kwargs))
        base["audit"] = self._audit_summary()
        return base

    def _create_task(
        self,
        *,
        provider: str,
        model: str,
        prompt_summary: str,
        context: dict[str, Any],
    ) -> AITaskRecord:
        with session_scope(self._session_factory) as session:
            row = AITaskRecord(
                task_id=uuid4().hex,
                provider=provider,
                model=model,
                state="running",
                prompt_summary=prompt_summary,
                context_json=context,
            )
            session.add(row)
            session.flush()
            session.expunge(row)
            return row

    def _mark_succeeded(
        self,
        task_id: str,
        *,
        output: dict[str, Any],
        validation: dict[str, Any],
    ) -> None:
        with session_scope(self._session_factory) as session:
            row = session.execute(select(AITaskRecord).where(AITaskRecord.task_id == task_id)).scalar_one()
            row.state = "succeeded"
            row.output_json = output
            row.validation_json = validation
            row.error = ""
            row.completed_at = utc_now()

    def _mark_failed(self, task_id: str, error: str) -> None:
        with session_scope(self._session_factory) as session:
            row = session.execute(select(AITaskRecord).where(AITaskRecord.task_id == task_id)).scalar_one()
            row.state = "failed"
            row.error = error[:1000]
            row.validation_json = {"valid": False}
            row.completed_at = utc_now()

    def _audit_summary(self) -> dict[str, Any]:
        try:
            with session_scope(self._session_factory) as session:
                total = session.execute(select(func.count(AITaskRecord.id))).scalar_one()
                succeeded = session.execute(
                    select(func.count(AITaskRecord.id)).where(AITaskRecord.state == "succeeded")
                ).scalar_one()
                failed = session.execute(
                    select(func.count(AITaskRecord.id)).where(AITaskRecord.state == "failed")
                ).scalar_one()
        except Exception as exc:
            return {"ai_tasks_table": False, "error": f"{type(exc).__name__}:{exc}"}
        return {
            "ai_tasks_table": True,
            "total": int(total or 0),
            "succeeded": int(succeeded or 0),
            "failed": int(failed or 0),
        }


def _model_from_status(status: dict[str, Any], provider: str) -> str:
    provider_status = status.get(provider) if provider else {}
    if isinstance(provider_status, dict):
        return str(provider_status.get("model") or provider_status.get("last_model") or "")
    return str(status.get("model") or "")


def _jsonable_dataclass(value: Any) -> dict[str, Any]:
    if is_dataclass(value):
        return asdict(value)
    if isinstance(value, dict):
        return dict(value)
    return {}


ai_service = AIService()
