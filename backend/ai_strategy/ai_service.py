from __future__ import annotations

from dataclasses import asdict, is_dataclass
from typing import Any
from uuid import uuid4

from sqlalchemy import desc, func, select
from sqlalchemy.orm import Session, sessionmaker

from backend.ai_strategy.openai_strategy_client import openai_strategy_client
from backend.ai_strategy.strategy_contract import contract_quality
from backend.ai_strategy.strategy_validator import strategy_validator
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
        position_context = dict(position_context or {})
        candidate_source = _candidate_source(position_context)
        status = self.status(candidate_count=1, candidate_source=candidate_source)
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
                "candidate_source": candidate_source,
                "position_context": position_context,
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
            validation=_strategy_validation_payload(plan),
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
                rows = (
                    session.execute(select(AITaskRecord).order_by(desc(AITaskRecord.id)).limit(50))
                    .scalars()
                    .all()
                )
                validation_values = (
                    session.execute(
                        select(AITaskRecord.validation_json).where(AITaskRecord.state == "succeeded")
                    )
                    .scalars()
                    .all()
                )
        except Exception as exc:
            return {"ai_tasks_table": False, "error": f"{type(exc).__name__}:{exc}"}
        validation_rows = [
            row
            for row in validation_values
            if isinstance(row, dict)
        ]
        recent = [_audit_task_snapshot(row) for row in rows[:10]]
        last_tradable = next((row for row in recent if row.get("tradable_strategy")), {})
        return {
            "ai_tasks_table": True,
            "total": int(total or 0),
            "succeeded": int(succeeded or 0),
            "failed": int(failed or 0),
            "tradable_strategy_count": sum(1 for row in validation_rows if row.get("tradable_strategy") is True),
            "non_tradable_strategy_count": sum(1 for row in validation_rows if row.get("tradable_strategy") is not True),
            "invalid_strategy_count": sum(1 for row in validation_rows if row.get("valid") is False),
            "open_strategy_count": sum(1 for row in validation_rows if row.get("opens") is True),
            "wait_strategy_count": sum(1 for row in validation_rows if row.get("action") == "WAIT"),
            "last_tradable_strategy": last_tradable,
            "recent_strategy_tasks": recent,
        }


def _model_from_status(status: dict[str, Any], provider: str) -> str:
    provider_status = status.get(provider) if provider else {}
    if isinstance(provider_status, dict):
        return str(provider_status.get("model") or provider_status.get("last_model") or "")
    return str(status.get("model") or "")


def _candidate_source(position_context: dict[str, Any]) -> str:
    selection = position_context.get("candidate_selection")
    if isinstance(selection, dict) and selection.get("source"):
        return str(selection.get("source") or "")
    return str(position_context.get("source") or "")


def _strategy_validation_payload(plan: StrategyPlan) -> dict[str, Any]:
    validator_ok, validator_reason = strategy_validator.validate(plan)
    raw = plan.raw if isinstance(plan.raw, dict) else {}
    provider = str(raw.get("provider") or "").strip().lower()
    contract = raw.get("strategy_contract") if isinstance(raw.get("strategy_contract"), dict) else None
    contract_ok, contract_reasons = contract_quality(contract)
    opens = bool(validator_ok and plan.action in {"OPEN_LONG", "OPEN_SHORT"})
    provider_available = bool(provider) and not provider.endswith("_unavailable")
    return {
        "valid": bool(validator_ok),
        "validator_reason": validator_reason,
        "action": plan.action,
        "symbol": plan.symbol,
        "side": plan.side,
        "provider": provider,
        "opens": opens,
        "tradable_strategy": bool(opens and provider_available),
        "contract_quality_ok": bool(contract_ok),
        "contract_quality_reasons": contract_reasons[:8],
        "strategy_contract_quality": raw.get("strategy_contract_quality") if isinstance(raw.get("strategy_contract_quality"), dict) else {},
    }


def _audit_task_snapshot(row: AITaskRecord) -> dict[str, Any]:
    validation = row.validation_json if isinstance(row.validation_json, dict) else {}
    output = row.output_json if isinstance(row.output_json, dict) else {}
    return {
        "task_id": row.task_id,
        "state": row.state,
        "provider": validation.get("provider") or row.provider,
        "model": row.model,
        "action": validation.get("action") or output.get("action"),
        "symbol": validation.get("symbol") or output.get("symbol"),
        "side": validation.get("side") or output.get("side"),
        "valid": validation.get("valid"),
        "validator_reason": validation.get("validator_reason"),
        "opens": validation.get("opens"),
        "tradable_strategy": validation.get("tradable_strategy"),
        "contract_quality_ok": validation.get("contract_quality_ok"),
        "error": row.error,
    }


def _jsonable_dataclass(value: Any) -> dict[str, Any]:
    if is_dataclass(value):
        return asdict(value)
    if isinstance(value, dict):
        return dict(value)
    return {}


ai_service = AIService()
