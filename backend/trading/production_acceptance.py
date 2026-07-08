from __future__ import annotations

import asyncio
import copy
from dataclasses import asdict
from typing import Any

from backend.ai_strategy.ai_service import ai_service
from backend.ai_strategy.dynamic_trade_model import auto_trading_risk_model
from backend.ai_strategy.openai_strategy_client import openai_strategy_client
from backend.ai_strategy.strategy_validator import strategy_validator
from backend.config import settings
from backend.exchange.binance_futures import binance_futures
from backend.learning.ai_strategy_feedback import ai_strategy_feedback
from backend.learning.learning_data_audit import learning_data_audit
from backend.learning.strategy_geometry_sampler import strategy_geometry_sampler
from backend.learning.strategy_registry import strategy_registry
from backend.market.binance_rest import binance_rest
from backend.market.market_service import market_service
from backend.models import RadarItem, StrategyPlan, now_ms
from backend.positions.position_manager import position_manager
from backend.positions.position_registry import position_registry
from backend.radar.radar_engine import radar_engine
from backend.storage.db import db
from backend.trading.autotrader import autotrader
from backend.trading.live_executor import live_executor
from backend.trading.live_readiness import live_readiness
from backend.trading.performance_guard import performance_guard


PRODUCTION_ACCEPTANCE_CONFIRM = "ALLOW_REAL_ORDER"
PRODUCTION_ACCEPTANCE_MODES = {"preflight", "exchange_test_order", "real_order"}
PRODUCTION_ACCEPTANCE_SCAN_ATTEMPTS = 3
PRODUCTION_ACCEPTANCE_SCAN_RETRY_SECONDS = 2.0
PRODUCTION_ACCEPTANCE_SCAN_TIMEOUT_SECONDS = 90.0
STRATEGY_GEOMETRY_MIN_PROFIT_FACTOR = 1.15
PRODUCTION_ACCEPTANCE_REQUIRED_STAGES = [
    "scan",
    "learning_data_audit",
    "candidate_selection",
    "ai_strategy_plan",
    "risk_model",
    "live_readiness",
    "exchange_order_submitted",
    "learning_open_recorded",
    "position_manager_review",
    "learning_close_recorded",
]


class ProductionAcceptanceRunner:
    """Runs the real production chain and reports where acceptance stops.

    This runner deliberately avoids synthetic symbols, rule-only acceptance
    probes, and paper executor substitutions. A production pass requires a real
    exchange order response and a closed-position learning record.
    """

    def __init__(self) -> None:
        self.last_report: dict[str, Any] = db.get_kv("production_acceptance.last_report", {})
        self.in_progress = False

    def status(self) -> dict[str, Any]:
        report = copy.deepcopy(self.last_report) if isinstance(self.last_report, dict) and self.last_report else self._empty_report()
        validation = self._current_validation(report)
        report["current_validation"] = validation
        acceptance = report.setdefault("production_acceptance", {})
        acceptance["currently_valid"] = validation["currently_valid"]
        acceptance["invalidated_by"] = validation["invalidated_by"]
        if not validation["currently_valid"]:
            report["ok"] = False
            acceptance["passed"] = False
            result = report.setdefault("result", {})
            result.setdefault("blocked", validation["invalidated_by"][0] if validation["invalidated_by"] else "production_acceptance_not_currently_valid")
        return report

    def _empty_report(self) -> dict[str, Any]:
        return {
            "ok": False,
            "mode": "none",
            "production_acceptance": {"passed": False},
            "stages": [],
            "result": {"blocked": "not_run"},
        }

    def _current_validation(self, report: dict[str, Any]) -> dict[str, Any]:
        invalidated_by: list[str] = []
        acceptance = report.get("production_acceptance") if isinstance(report.get("production_acceptance"), dict) else {}
        passed = bool(report.get("ok") and acceptance.get("passed"))
        mode = str(report.get("mode") or "")
        finished_ms = int(report.get("finished_ms") or 0)
        max_age_ms = max(1, int(settings.production_acceptance_max_age_seconds or 3600)) * 1000
        age_ms = max(0, now_ms() - finished_ms) if finished_ms > 0 else 0
        if not passed:
            invalidated_by.append("production_acceptance_not_passed")
        if passed and mode != "real_order":
            invalidated_by.append("production_acceptance_mode_not_real_order")
        if passed and finished_ms <= 0:
            invalidated_by.append("production_acceptance_missing_finished_ms")
        elif passed and age_ms > max_age_ms:
            invalidated_by.append("production_acceptance_stale")
        stage_rows = report.get("stages") if isinstance(report.get("stages"), list) else []
        by_name = {stage.get("name"): stage for stage in stage_rows if isinstance(stage, dict)}
        missing_stages = [
            name
            for name in PRODUCTION_ACCEPTANCE_REQUIRED_STAGES
            if not bool(by_name.get(name, {}).get("ok"))
        ]
        if passed and missing_stages:
            invalidated_by.append("production_acceptance_evidence_incomplete")
        data_quality = learning_data_audit.cached_summary()
        if not bool(data_quality.get("production_grade")):
            invalidated_by.append("learning_data_not_production_grade")
        return {
            "currently_valid": not invalidated_by,
            "invalidated_by": invalidated_by,
            "missing_stages": missing_stages,
            "age_seconds": round(age_ms / 1000.0, 3) if finished_ms > 0 else None,
            "max_age_seconds": max_age_ms // 1000,
            "learning_data_audit": {
                "production_grade": bool(data_quality.get("production_grade")),
                "trust_level": data_quality.get("trust_level"),
                "reasons": list(data_quality.get("reasons") or []),
            },
        }

    async def run(
        self,
        *,
        mode: str = "preflight",
        confirm_real_order: str = "",
        manage_seconds: int = 0,
    ) -> dict[str, Any]:
        previous_auto_enabled = bool(autotrader.enabled)
        self.in_progress = True
        autotrader.enabled = False
        try:
            async with autotrader.global_lock:
                return await self._run(mode=mode, confirm_real_order=confirm_real_order, manage_seconds=manage_seconds)
        finally:
            autotrader.enabled = previous_auto_enabled
            self.in_progress = False

    async def _run(
        self,
        *,
        mode: str = "preflight",
        confirm_real_order: str = "",
        manage_seconds: int = 0,
    ) -> dict[str, Any]:
        mode = (mode or "preflight").strip().lower()
        manage_seconds = max(0, min(int(manage_seconds or 0), 900))
        stages: list[dict[str, Any]] = []
        started_ms = now_ms()

        if mode not in PRODUCTION_ACCEPTANCE_MODES:
            report = self._report(
                mode=mode,
                started_ms=started_ms,
                stages=[self._stage("request", False, {"reason": "invalid_mode", "allowed": sorted(PRODUCTION_ACCEPTANCE_MODES)})],
                result={"blocked": "invalid_mode"},
            )
            self._store(report)
            return report

        safety_ok, safety_reason = self._mode_safety(mode, confirm_real_order)
        stages.append(self._stage("production_safety", safety_ok, self._safety_evidence(mode, safety_reason)))
        if not safety_ok and mode != "preflight":
            report = self._report(mode=mode, started_ms=started_ms, stages=stages, result={"blocked": safety_reason})
            self._store(report)
            return report

        data_quality = learning_data_audit.summary(force=True)
        data_quality_ok = bool(data_quality.get("production_grade"))
        stages.append(self._stage("learning_data_audit", data_quality_ok, data_quality))
        if not data_quality_ok and mode != "preflight":
            report = self._report(
                mode=mode,
                started_ms=started_ms,
                stages=stages,
                result={"blocked": "learning_data_not_production_grade"},
            )
            self._store(report)
            return report

        try:
            items = await self._scan_with_timeout(force_refresh=True)
            stages.append(
                self._stage(
                    "scan",
                    bool(items),
                    {
                        "count": len(items),
                        "last_scan_id": radar_engine.last_scan_id,
                        "last_scan_time": radar_engine.last_scan_time,
                        "market_heat": radar_engine.market_heat,
                        "market_data_source": binance_rest.last_public_source,
                        "top_symbols": [item.symbol for item in items[:5]],
                        "scan_status": radar_engine.scan_status(),
                    },
                )
            )
        except asyncio.TimeoutError:
            stages.append(self._stage("scan", False, {"error": "radar_scan_timeout", "scan_status": radar_engine.scan_status()}))
            report = self._report(mode=mode, started_ms=started_ms, stages=stages, result={"blocked": "scan_failed"})
            self._store(report)
            return report
        except Exception as exc:
            stages.append(self._stage("scan", False, {"error": _err(exc), "scan_status": radar_engine.scan_status()}))
            report = self._report(mode=mode, started_ms=started_ms, stages=stages, result={"blocked": "scan_failed"})
            self._store(report)
            return report

        performance = performance_guard.summary()
        open_positions = len(position_registry.list_open())
        candidate_attempts = []
        configured_candidates = []
        configured_source = "unknown"
        candidates = []
        review_candidates = []
        geometry_candidate_reports: list[dict[str, Any]] = []
        candidate_geometry_samples: dict[str, dict[str, Any]] = {}
        geometry_rejected_symbols: list[str] = []
        generation_gate_rejections: list[dict[str, Any]] = []
        candidate_source = "strict"
        for attempt in range(1, PRODUCTION_ACCEPTANCE_SCAN_ATTEMPTS + 1):
            configured_candidates, configured_source = autotrader._candidate_batch(performance)
            candidates = radar_engine.select_ai_candidates(radar_engine.top50)
            review_candidates = radar_engine.select_ai_review_candidates(radar_engine.top50)
            attempt_evidence = {
                "attempt": attempt,
                "last_scan_id": radar_engine.last_scan_id,
                "last_scan_time": radar_engine.last_scan_time,
                "strict_count": len(candidates),
                "strict_symbols": [item.symbol for item in candidates],
                "strict_review_count": len(review_candidates),
                "strict_review_symbols": [item.symbol for item in review_candidates[:5]],
                "configured_candidate_source": configured_source,
                "configured_candidate_symbols": [item.symbol for item in configured_candidates],
            }
            candidate_attempts.append(attempt_evidence)
            if candidates:
                candidates, geometry_candidate_reports, candidate_geometry_samples = await self._geometry_rank_candidates(
                    candidates,
                    candidate_source,
                    performance,
                )
                attempt_evidence["geometry_ranked_symbols"] = [item.symbol for item in candidates]
                attempt_evidence["geometry_candidate_reports"] = geometry_candidate_reports
                geometry_rejected_symbols = self._geometry_rejected_symbols(candidates, candidate_geometry_samples)
                attempt_evidence["geometry_supported_symbols"] = [
                    item.symbol for item in self._geometry_supported_candidates(candidates, candidate_geometry_samples)
                ]
                attempt_evidence["geometry_rejected_symbols"] = geometry_rejected_symbols
                if self._has_strategy_geometry_supported_candidate(candidates, candidate_geometry_samples):
                    candidates = self._geometry_supported_candidates(candidates, candidate_geometry_samples)
                    candidates, generation_gate_rejections = self._generation_gate_supported_candidates(
                        candidates,
                        candidate_source,
                        candidate_geometry_samples,
                    )
                    attempt_evidence["generation_gate_rejected_symbols"] = [
                        row["symbol"] for row in generation_gate_rejections
                    ]
                    attempt_evidence["generation_gate_rejections"] = generation_gate_rejections
                    if not candidates:
                        attempt_evidence["generation_gate_retry_reason"] = "no_generation_gate_supported_strict_candidate"
                    else:
                        break
                candidates = []
                attempt_evidence["geometry_retry_reason"] = "no_geometry_supported_strict_candidate"
                if attempt >= PRODUCTION_ACCEPTANCE_SCAN_ATTEMPTS:
                    break
            strict_geometry_candidates = self._strict_geometry_candidates(radar_engine.top50)
            attempt_evidence["strict_geometry_count"] = len(strict_geometry_candidates)
            attempt_evidence["strict_geometry_symbols"] = [item.symbol for item in strict_geometry_candidates[:5]]
            if strict_geometry_candidates:
                strict_geometry_ordered, strict_geometry_reports, strict_geometry_samples = await self._geometry_rank_candidates(
                    strict_geometry_candidates,
                    "strict_geometry",
                    performance,
                )
                attempt_evidence["strict_geometry_ranked_symbols"] = [item.symbol for item in strict_geometry_ordered]
                attempt_evidence["strict_geometry_candidate_reports"] = strict_geometry_reports
                if self._has_strategy_geometry_supported_candidate(strict_geometry_ordered, strict_geometry_samples):
                    candidates = self._geometry_supported_candidates(strict_geometry_ordered, strict_geometry_samples)
                    candidates, generation_gate_rejections = self._generation_gate_supported_candidates(
                        candidates,
                        "strict_geometry",
                        strict_geometry_samples,
                    )
                    geometry_candidate_reports = strict_geometry_reports
                    candidate_geometry_samples = strict_geometry_samples
                    geometry_rejected_symbols = self._geometry_rejected_symbols(strict_geometry_ordered, strict_geometry_samples)
                    attempt_evidence["strict_geometry_supported_symbols"] = [item.symbol for item in candidates]
                    attempt_evidence["strict_geometry_rejected_symbols"] = geometry_rejected_symbols
                    attempt_evidence["strict_geometry_generation_gate_rejected_symbols"] = [
                        row["symbol"] for row in generation_gate_rejections
                    ]
                    attempt_evidence["strict_geometry_generation_gate_rejections"] = generation_gate_rejections
                    if not candidates:
                        attempt_evidence["strict_geometry_generation_gate_retry_reason"] = "no_generation_gate_supported_strict_geometry_candidate"
                    else:
                        candidate_source = "strict_geometry"
                        break
                attempt_evidence["strict_geometry_retry_reason"] = "no_geometry_supported_strict_geometry_candidate"
            if attempt < PRODUCTION_ACCEPTANCE_SCAN_ATTEMPTS:
                await asyncio.sleep(PRODUCTION_ACCEPTANCE_SCAN_RETRY_SECONDS)
                try:
                    await self._scan_with_timeout(force_refresh=True)
                except asyncio.TimeoutError:
                    candidate_attempts[-1]["retry_scan_error"] = "radar_scan_timeout"
                    break
                except Exception as exc:
                    candidate_attempts[-1]["retry_scan_error"] = _err(exc)
                    break
        production_candidate_ok = bool(candidates)
        stages.append(
            self._stage(
                "candidate_selection",
                production_candidate_ok,
                {
                    "candidate_source": candidate_source,
                    "candidate_symbols": [item.symbol for item in candidates],
                    "geometry_candidate_reports": geometry_candidate_reports,
                    "geometry_rejected_symbols": geometry_rejected_symbols,
                    "generation_gate_rejected_symbols": [row["symbol"] for row in generation_gate_rejections],
                    "generation_gate_rejections": generation_gate_rejections,
                    "configured_candidate_source": configured_source,
                    "configured_candidate_symbols": [item.symbol for item in configured_candidates],
                    "production_requires_source": "strict",
                    "candidate_attempts": candidate_attempts,
                    "open_positions": open_positions,
                    "performance": performance,
                    "candidate_lock": autotrader.candidate_lock_status(),
                    "diagnostics": autotrader.candidate_diagnostics(performance),
                },
            )
        )
        if not candidates:
            if review_candidates:
                review_candidates, shadow_geometry_reports, shadow_geometry_samples = await self._geometry_rank_candidates(
                    review_candidates,
                    "strict_review",
                    performance,
                )
                shadow = await self._try_generate_open_plan(
                    review_candidates,
                    performance,
                    "strict_review",
                    candidate_attempts,
                    candidate_geometry_samples=shadow_geometry_samples,
                )
                stages.append(
                    self._stage(
                        "shadow_strategy_plan",
                        bool(shadow["plan_ok"] and shadow["ai_generated"] and shadow["opens"]),
                        {
                            "candidate_source": "strict_review",
                            "candidate_symbols": [item.symbol for item in review_candidates],
                            "geometry_candidate_reports": shadow_geometry_reports,
                            "not_counted_as_production": True,
                            "reason": "strict_review candidates are useful for shadow validation but cannot satisfy production strict acceptance",
                            "provider": shadow["provider"],
                            "action": shadow["plan"].action if shadow["plan"] else "",
                            "symbol": shadow["plan"].symbol if shadow["plan"] else "",
                            "side": shadow["plan"].side if shadow["plan"] else "",
                            "confidence": shadow["plan"].confidence if shadow["plan"] else 0,
                            "validation_ok": shadow["plan_ok"],
                            "validation_reason": shadow["plan_reason"],
                            "attempted_candidates": shadow["attempted_candidates"],
                            "plan_attempts": shadow["plan_attempts"],
                        },
                    )
                )
            report = self._report(mode=mode, started_ms=started_ms, stages=stages, result={"blocked": "no_strict_production_candidates"})
            self._store(report)
            return report

        chain = await self._try_generate_and_risk_open(
            candidates,
            performance,
            candidate_source,
            candidate_attempts,
            open_positions,
            candidate_geometry_samples=candidate_geometry_samples,
        )
        if candidate_source == "strict" and not self._chain_has_open_strategy(chain):
            fallback_candidates = [
                item
                for item in self._strict_geometry_candidates(radar_engine.top50)
                if item.symbol not in {candidate.symbol for candidate in candidates}
            ]
            if fallback_candidates:
                fallback_candidates, fallback_geometry_reports, fallback_geometry_samples = await self._geometry_rank_candidates(
                    fallback_candidates,
                    "strict_geometry",
                    performance,
                )
                fallback_supported = self._has_strategy_geometry_supported_candidate(
                    fallback_candidates,
                    fallback_geometry_samples,
                )
                fallback_candidates = self._geometry_supported_candidates(fallback_candidates, fallback_geometry_samples)
                fallback_candidates, fallback_generation_rejections = self._generation_gate_supported_candidates(
                    fallback_candidates,
                    "strict_geometry",
                    fallback_geometry_samples,
                )
                fallback_chain = None
                if fallback_supported and fallback_candidates:
                    fallback_chain = await self._try_generate_and_risk_open(
                        fallback_candidates,
                        performance,
                        "strict_geometry",
                        candidate_attempts,
                        open_positions,
                        candidate_geometry_samples=fallback_geometry_samples,
                    )
                    fallback_ok = self._chain_has_open_strategy(fallback_chain)
                else:
                    fallback_ok = False
                fallback_plan = fallback_chain.get("plan") if isinstance(fallback_chain, dict) else None
                stages.append(
                    self._stage(
                        "strict_geometry_fallback",
                        fallback_ok,
                        {
                            "fallback_from": "strict",
                            "candidate_source": "strict_geometry",
                            "candidate_symbols": [item.symbol for item in fallback_candidates],
                            "geometry_candidate_reports": fallback_geometry_reports,
                            "geometry_supported": fallback_supported,
                            "generation_gate_rejected_symbols": [row["symbol"] for row in fallback_generation_rejections],
                            "generation_gate_rejections": fallback_generation_rejections,
                            "provider": fallback_chain.get("provider") if isinstance(fallback_chain, dict) else "",
                            "action": fallback_plan.action if fallback_plan else "",
                            "symbol": fallback_plan.symbol if fallback_plan else "",
                            "side": fallback_plan.side if fallback_plan else "",
                            "validation_ok": fallback_chain.get("plan_ok") if isinstance(fallback_chain, dict) else False,
                            "validation_reason": fallback_chain.get("plan_reason") if isinstance(fallback_chain, dict) else "",
                            "attempted_candidates": fallback_chain.get("attempted_candidates") if isinstance(fallback_chain, dict) else 0,
                            "plan_attempts": fallback_chain.get("plan_attempts") if isinstance(fallback_chain, dict) else [],
                            "risk_ok": fallback_chain.get("risk_ok") if isinstance(fallback_chain, dict) else False,
                            "risk_attempts": fallback_chain.get("risk_attempts") if isinstance(fallback_chain, dict) else [],
                        },
                    )
                )
                if fallback_ok and fallback_chain is not None:
                    chain = fallback_chain
                    candidates = fallback_candidates
                    candidate_source = "strict_geometry"
                    candidate_geometry_samples = fallback_geometry_samples
        item = chain["item"]
        plan = chain["plan"]
        provider = chain["provider"]
        plan_ok = chain["plan_ok"]
        plan_reason = chain["plan_reason"]
        ai_generated = chain["ai_generated"]
        opens = chain["opens"]
        plan_attempts = chain["plan_attempts"]
        max_plan_attempts = chain["attempted_candidates"]
        assert item is not None and plan is not None
        ai_status = ai_service.status(candidate_count=len(candidates), candidate_source=candidate_source)
        stages.append(
            self._stage(
                "ai_strategy_plan",
                bool(plan_ok and ai_generated and opens),
                {
                    "provider": provider,
                    "action": plan.action,
                    "symbol": plan.symbol,
                    "side": plan.side,
                    "confidence": plan.confidence,
                    "reason": plan.reason,
                    "wait_type": plan.wait_type,
                    "validation_ok": plan_ok,
                    "validation_reason": plan_reason,
                    "provider_status": _provider_status(ai_status),
                    "attempted_candidates": max_plan_attempts,
                    "plan_attempts": plan_attempts,
                    "plan": _plan_snapshot(plan),
                },
            )
        )
        if not plan_ok or not ai_generated or not opens:
            report = self._report(mode=mode, started_ms=started_ms, stages=stages, result={"blocked": "ai_strategy_not_open"})
            self._store(report)
            return report

        account_summary = chain["account_summary"]
        exec_plan = chain["exec_plan"]
        risk_ok = bool(chain["risk_ok"])
        assert exec_plan is not None
        stages.append(
            self._stage(
                "risk_model",
                risk_ok,
                {
                    "decision": exec_plan.decision,
                    "mode": exec_plan.mode,
                    "reason": exec_plan.reason,
                    "execution": asdict(exec_plan),
                    "account": _account_snapshot(account_summary),
                    "risk_attempts": chain["risk_attempts"],
                },
            )
        )
        if not risk_ok:
            report = self._report(mode=mode, started_ms=started_ms, stages=stages, result={"blocked": "risk_model_not_open"})
            self._store(report)
            return report

        readiness = live_readiness.summary()
        live_gate_ok, live_gate_reason = self._live_gate(mode, readiness)
        stages.append(
            self._stage(
                "live_readiness",
                live_gate_ok,
                {
                    "reason": live_gate_reason,
                    "current_stage": readiness.get("current_stage"),
                    "execution": (readiness.get("metrics") or {}).get("execution"),
                    "stage_readiness": readiness.get("stage_readiness"),
                },
            )
        )
        if mode == "preflight":
            report = self._report(
                mode=mode,
                started_ms=started_ms,
                stages=stages,
                result={"blocked": "preflight_does_not_submit_orders"},
            )
            self._store(report)
            return report
        if not live_gate_ok:
            report = self._report(mode=mode, started_ms=started_ms, stages=stages, result={"blocked": live_gate_reason})
            self._store(report)
            return report

        position = await live_executor.open_position(radar_engine.last_scan_id, plan.strategy_id, item.score, exec_plan)
        order = dict(position.exchange_open_order or {})
        real_order = bool(order.get("orderId")) and not bool(order.get("testOrder"))
        order_ok = real_order if mode == "real_order" else bool(order.get("testOrder"))
        stages.append(
            self._stage(
                "exchange_order_submitted",
                order_ok,
                {
                    "position_id": position.position_id,
                    "symbol": position.symbol,
                    "lock_status": position.lock_status,
                    "real_order": real_order,
                    "test_order": bool(order.get("testOrder")),
                    "order": _order_snapshot(order),
                    "stop_order": _order_snapshot(position.exchange_stop_order or {}),
                    "take_profit_order": _order_snapshot(position.exchange_tp_order or {}),
                },
            )
        )

        feedback_open = ai_strategy_feedback.record_open(
            plan=plan,
            item=item,
            exec_plan=exec_plan,
            position=position,
            candidate_source=candidate_source,
            paper_validation=False,
        )
        stages.append(self._stage("learning_open_recorded", bool(feedback_open.get("recorded")), feedback_open))

        closed = await self._wait_for_close(position.position_id, manage_seconds)
        stages.append(
            self._stage(
                "position_manager_review",
                bool(closed),
                {
                    "manage_seconds": manage_seconds,
                    "position_id": position.position_id,
                    "closed": bool(closed),
                    "open_after_wait": position_registry.open.get(position.position_id) is not None,
                    "last_decision": getattr(position_registry.open.get(position.position_id), "last_decision", {}) if position_registry.open.get(position.position_id) else {},
                },
            )
        )

        learning_closed = self._learning_close_recorded(position.strategy_id, position.position_id)
        stages.append(
            self._stage(
                "learning_close_recorded",
                learning_closed,
                {
                    "strategy_id": position.strategy_id,
                    "position_id": position.position_id,
                    "strategy": _strategy_learning_snapshot(strategy_registry.get(position.strategy_id) or {}),
                    "closed_position": closed or {},
                },
            )
        )

        report = self._report(
            mode=mode,
            started_ms=started_ms,
            stages=stages,
            result={
                "position_id": position.position_id,
                "strategy_id": position.strategy_id,
                "symbol": position.symbol,
                "real_order": real_order,
                "closed": bool(closed),
            },
        )
        self._store(report)
        return report

    async def _scan_with_timeout(self, *, force_refresh: bool = False) -> list[RadarItem]:
        return await asyncio.wait_for(
            radar_engine.scan(force_refresh=force_refresh),
            timeout=PRODUCTION_ACCEPTANCE_SCAN_TIMEOUT_SECONDS,
        )

    def _mode_safety(self, mode: str, confirm_real_order: str) -> tuple[bool, str]:
        if mode == "real_order" and confirm_real_order != PRODUCTION_ACCEPTANCE_CONFIRM:
            return False, "real_order_requires_explicit_confirm"
        if mode == "exchange_test_order" and not settings.live_use_test_order:
            return False, "exchange_test_order_requires_live_use_test_order_true"
        return True, "ok"

    def _safety_evidence(self, mode: str, reason: str) -> dict[str, Any]:
        return {
            "mode": mode,
            "reason": reason,
            "trade_mode": settings.trade_mode,
            "live_trading_enabled": settings.live_trading_enabled,
            "live_use_test_order": settings.live_use_test_order,
            "binance_configured": binance_futures.configured(),
            "market_data_source": binance_rest.last_public_source,
            "real_order_confirm_required": PRODUCTION_ACCEPTANCE_CONFIRM,
        }

    async def _fresh_item(self, item: RadarItem) -> RadarItem:
        price = await market_service.price_for(item.symbol)
        if price and price > 0:
            from dataclasses import replace

            return replace(item, price=price)
        return item

    async def _generate_plan(
        self,
        item: RadarItem,
        performance: dict[str, Any],
        candidate_source: str,
        candidate_attempts: list[dict[str, Any]],
        strategy_geometry_sample: dict[str, Any] | None = None,
    ) -> StrategyPlan:
        active_strategy = strategy_registry.active()
        position_context = {
            "open_positions": len(position_registry.list_open()),
            "performance_guard": performance,
            "candidate_selection": {
                "source": "production_acceptance",
                "candidate_source": candidate_source,
                "acceptance_mode": True,
                "production_acceptance": True,
                "strict_candidate": candidate_source == "strict",
                "strict_geometry_candidate": candidate_source == "strict_geometry",
                "attempts": candidate_attempts,
                "instruction": (
                    "This item already passed the local strict candidate selector. "
                    "Radar rank is diagnostic, not an automatic veto. "
                    "If current edge, alignment, expectancy, and risk geometry are valid, generate a constrained OPEN strategy even when historical attribution is weak. "
                    "Historical weakness belongs in confidence, invalidation, allowed_stages.live=false, and research_review; risk_model and live_readiness own final execution permission. "
                    "Return WAIT only when current edge, alignment, expectancy after costs, or risk geometry is insufficient."
                ),
            },
            "active_evolved_strategy": {
                "strategy_id": active_strategy.get("strategy_id"),
                "name": active_strategy.get("name"),
                "filters": active_strategy.get("filters"),
                "metrics": active_strategy.get("metrics"),
            }
            if active_strategy
            else None,
        }
        if isinstance(strategy_geometry_sample, dict) and strategy_geometry_sample:
            position_context["strategy_geometry_sample"] = strategy_geometry_sample
            if candidate_source == "strict_geometry":
                feedback_override = self._strict_geometry_ai_feedback_override(item, strategy_geometry_sample)
                if feedback_override:
                    position_context["ai_strategy_quality_feedback"] = feedback_override
        return await ai_service.generate_strategy(item, position_context)

    async def _try_generate_open_plan(
        self,
        candidates: list[RadarItem],
        performance: dict[str, Any],
        candidate_source: str,
        candidate_attempts: list[dict[str, Any]],
        candidate_geometry_samples: dict[str, dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        plan_attempts: list[dict[str, Any]] = []
        selected_item: RadarItem | None = None
        selected_plan: StrategyPlan | None = None
        selected_provider = ""
        selected_plan_ok = False
        selected_plan_reason = ""
        selected_ai_generated = False
        selected_opens = False
        max_plan_attempts = max(1, min(len(candidates), 5))

        for idx, candidate in enumerate(candidates[:max_plan_attempts], start=1):
            attempted_item = await self._fresh_item(candidate)
            fresh_ok, freshness = autotrader._ai_candidate_freshness_report(attempted_item, candidate_source, performance)
            if not fresh_ok:
                attempted_plan = self._stale_wait_plan(attempted_item, freshness)
                plan_attempts.append(
                    {
                        "attempt": idx,
                        "symbol": attempted_item.symbol,
                        "side": attempted_item.direction,
                        "rank": attempted_item.rank,
                        "score": attempted_item.score,
                        "fund_confirm": f"{attempted_item.fund_confirm_count}/{attempted_item.fund_confirm_total}",
                        "fake_breakout_risk": attempted_item.fake_breakout_risk,
                        "provider": "freshness_guard",
                        "action": attempted_plan.action,
                        "confidence": attempted_plan.confidence,
                        "reason": attempted_plan.reason,
                        "wait_type": attempted_plan.wait_type,
                        "validation_ok": False,
                        "validation_reason": ",".join(freshness["reasons"][:4]),
                        "opens": False,
                        "freshness": freshness,
                        "plan": _plan_snapshot(attempted_plan),
                    }
                )
                if selected_plan is None:
                    selected_item = attempted_item
                    selected_plan = attempted_plan
                    selected_provider = "freshness_guard"
                    selected_plan_reason = ",".join(freshness["reasons"][:4])
                continue
            generation_gate = self._blocked_generation_gate(attempted_item, candidate_source=candidate_source) if candidate_source != "strict_geometry" else {}
            if generation_gate:
                attempted_plan = self._generation_gate_wait_plan(attempted_item, generation_gate)
                gate_reasons = _gate_reasons(generation_gate)
                plan_attempts.append(
                    {
                        "attempt": idx,
                        "symbol": attempted_item.symbol,
                        "side": attempted_item.direction,
                        "rank": attempted_item.rank,
                        "score": attempted_item.score,
                        "fund_confirm": f"{attempted_item.fund_confirm_count}/{attempted_item.fund_confirm_total}",
                        "fake_breakout_risk": attempted_item.fake_breakout_risk,
                        "provider": "generation_gate",
                        "action": attempted_plan.action,
                        "confidence": attempted_plan.confidence,
                        "reason": attempted_plan.reason,
                        "wait_type": attempted_plan.wait_type,
                        "validation_ok": False,
                        "validation_reason": ",".join(gate_reasons[:4]),
                        "opens": False,
                        "generation_gate": generation_gate,
                        "plan": _plan_snapshot(attempted_plan),
                    }
                )
                if selected_plan is None:
                    selected_item = attempted_item
                    selected_plan = attempted_plan
                    selected_provider = "generation_gate"
                    selected_plan_reason = ",".join(gate_reasons[:4])
                continue
            geometry_sample = await self._strategy_geometry_sample(attempted_item, candidate_geometry_samples)
            geometry_reasons = self._strategy_geometry_reasons(geometry_sample)
            if geometry_reasons:
                attempted_plan = self._strategy_geometry_wait_plan(attempted_item, geometry_sample, geometry_reasons)
                plan_attempts.append(
                    self._strategy_geometry_attempt(idx, attempted_item, attempted_plan, geometry_sample, geometry_reasons)
                )
                if selected_plan is None:
                    selected_item = attempted_item
                    selected_plan = attempted_plan
                    selected_provider = "strategy_geometry_gate"
                    selected_plan_reason = geometry_reasons[0]
                continue
            generation_gate = (
                self._blocked_generation_gate(
                    attempted_item,
                    candidate_source=candidate_source,
                    strategy_geometry_sample=geometry_sample,
                )
                if candidate_source == "strict_geometry"
                else {}
            )
            if generation_gate:
                attempted_plan = self._generation_gate_wait_plan(attempted_item, generation_gate)
                gate_reasons = _gate_reasons(generation_gate)
                plan_attempts.append(
                    {
                        "attempt": idx,
                        "symbol": attempted_item.symbol,
                        "side": attempted_item.direction,
                        "rank": attempted_item.rank,
                        "score": attempted_item.score,
                        "fund_confirm": f"{attempted_item.fund_confirm_count}/{attempted_item.fund_confirm_total}",
                        "fake_breakout_risk": attempted_item.fake_breakout_risk,
                        "provider": "generation_gate",
                        "action": attempted_plan.action,
                        "confidence": attempted_plan.confidence,
                        "reason": attempted_plan.reason,
                        "wait_type": attempted_plan.wait_type,
                        "validation_ok": False,
                        "validation_reason": ",".join(gate_reasons[:4]),
                        "opens": False,
                        "generation_gate": generation_gate,
                        "strategy_geometry_sample": geometry_sample,
                        "plan": _plan_snapshot(attempted_plan),
                    }
                )
                if selected_plan is None:
                    selected_item = attempted_item
                    selected_plan = attempted_plan
                    selected_provider = "generation_gate"
                    selected_plan_reason = ",".join(gate_reasons[:4])
                continue
            attempted_plan = await self._generate_plan(
                attempted_item,
                performance,
                candidate_source,
                candidate_attempts,
                strategy_geometry_sample=geometry_sample,
            )
            attempted_provider = _plan_provider(attempted_plan)
            attempted_ok, attempted_reason = strategy_validator.validate(attempted_plan)
            attempted_ai_generated = _accepted_strategy_provider(attempted_provider)
            attempted_opens = attempted_plan.action in {"OPEN_LONG", "OPEN_SHORT"}
            opens = bool(attempted_ok and attempted_ai_generated and attempted_opens)
            plan_attempts.append(
                {
                    "attempt": idx,
                    "symbol": attempted_item.symbol,
                    "side": attempted_item.direction,
                    "rank": attempted_item.rank,
                    "score": attempted_item.score,
                    "fund_confirm": f"{attempted_item.fund_confirm_count}/{attempted_item.fund_confirm_total}",
                    "fake_breakout_risk": attempted_item.fake_breakout_risk,
                    "provider": attempted_provider,
                    "action": attempted_plan.action,
                    "confidence": attempted_plan.confidence,
                    "reason": attempted_plan.reason,
                    "wait_type": attempted_plan.wait_type,
                    "validation_ok": attempted_ok,
                    "validation_reason": attempted_reason,
                    "opens": opens,
                    "strategy_geometry_sample": geometry_sample,
                    "plan": _plan_snapshot(attempted_plan),
                }
            )
            if opens or selected_plan is None:
                selected_item = attempted_item
                selected_plan = attempted_plan
                selected_provider = attempted_provider
                selected_plan_ok = attempted_ok
                selected_plan_reason = attempted_reason
                selected_ai_generated = attempted_ai_generated
                selected_opens = attempted_opens
            if opens:
                break

        return {
            "item": selected_item,
            "plan": selected_plan,
            "provider": selected_provider,
            "plan_ok": selected_plan_ok,
            "plan_reason": selected_plan_reason,
            "ai_generated": selected_ai_generated,
            "opens": selected_opens,
            "attempted_candidates": max_plan_attempts,
            "plan_attempts": plan_attempts,
        }

    async def _try_generate_and_risk_open(
        self,
        candidates: list[RadarItem],
        performance: dict[str, Any],
        candidate_source: str,
        candidate_attempts: list[dict[str, Any]],
        open_positions: int,
        candidate_geometry_samples: dict[str, dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        market = {"market_heat": radar_engine.market_heat, "volatility_regime": autotrader._volatility_regime()}
        account_summary = None
        account = None
        plan_attempts: list[dict[str, Any]] = []
        risk_attempts: list[dict[str, Any]] = []
        selected_item: RadarItem | None = None
        selected_plan: StrategyPlan | None = None
        selected_provider = ""
        selected_plan_ok = False
        selected_plan_reason = ""
        selected_ai_generated = False
        selected_opens = False
        selected_exec_plan = None
        selected_risk_ok = False
        max_plan_attempts = max(1, min(len(candidates), 5))

        for idx, candidate in enumerate(candidates[:max_plan_attempts], start=1):
            attempted_item = await self._fresh_item(candidate)
            fresh_ok, freshness = autotrader._ai_candidate_freshness_report(attempted_item, candidate_source, performance)
            if not fresh_ok:
                attempted_plan = self._stale_wait_plan(attempted_item, freshness)
                plan_attempts.append(
                    {
                        "attempt": idx,
                        "symbol": attempted_item.symbol,
                        "side": attempted_item.direction,
                        "rank": attempted_item.rank,
                        "score": attempted_item.score,
                        "fund_confirm": f"{attempted_item.fund_confirm_count}/{attempted_item.fund_confirm_total}",
                        "fake_breakout_risk": attempted_item.fake_breakout_risk,
                        "provider": "freshness_guard",
                        "action": attempted_plan.action,
                        "confidence": attempted_plan.confidence,
                        "reason": attempted_plan.reason,
                        "wait_type": attempted_plan.wait_type,
                        "validation_ok": False,
                        "validation_reason": ",".join(freshness["reasons"][:4]),
                        "opens": False,
                        "freshness": freshness,
                        "plan": _plan_snapshot(attempted_plan),
                    }
                )
                if selected_plan is None:
                    selected_item = attempted_item
                    selected_plan = attempted_plan
                    selected_provider = "freshness_guard"
                    selected_plan_reason = ",".join(freshness["reasons"][:4])
                continue
            generation_gate = self._blocked_generation_gate(attempted_item, candidate_source=candidate_source) if candidate_source != "strict_geometry" else {}
            if generation_gate:
                attempted_plan = self._generation_gate_wait_plan(attempted_item, generation_gate)
                gate_reasons = _gate_reasons(generation_gate)
                plan_attempts.append(
                    {
                        "attempt": idx,
                        "symbol": attempted_item.symbol,
                        "side": attempted_item.direction,
                        "rank": attempted_item.rank,
                        "score": attempted_item.score,
                        "fund_confirm": f"{attempted_item.fund_confirm_count}/{attempted_item.fund_confirm_total}",
                        "fake_breakout_risk": attempted_item.fake_breakout_risk,
                        "provider": "generation_gate",
                        "action": attempted_plan.action,
                        "confidence": attempted_plan.confidence,
                        "reason": attempted_plan.reason,
                        "wait_type": attempted_plan.wait_type,
                        "validation_ok": False,
                        "validation_reason": ",".join(gate_reasons[:4]),
                        "opens": False,
                        "generation_gate": generation_gate,
                        "plan": _plan_snapshot(attempted_plan),
                    }
                )
                if selected_plan is None:
                    selected_item = attempted_item
                    selected_plan = attempted_plan
                    selected_provider = "generation_gate"
                    selected_plan_reason = ",".join(gate_reasons[:4])
                continue
            geometry_sample = await self._strategy_geometry_sample(attempted_item, candidate_geometry_samples)
            geometry_reasons = self._strategy_geometry_reasons(geometry_sample)
            if geometry_reasons:
                attempted_plan = self._strategy_geometry_wait_plan(attempted_item, geometry_sample, geometry_reasons)
                plan_attempts.append(
                    self._strategy_geometry_attempt(idx, attempted_item, attempted_plan, geometry_sample, geometry_reasons)
                )
                if selected_plan is None:
                    selected_item = attempted_item
                    selected_plan = attempted_plan
                    selected_provider = "strategy_geometry_gate"
                    selected_plan_reason = geometry_reasons[0]
                continue
            generation_gate = (
                self._blocked_generation_gate(
                    attempted_item,
                    candidate_source=candidate_source,
                    strategy_geometry_sample=geometry_sample,
                )
                if candidate_source == "strict_geometry"
                else {}
            )
            if generation_gate:
                attempted_plan = self._generation_gate_wait_plan(attempted_item, generation_gate)
                gate_reasons = _gate_reasons(generation_gate)
                plan_attempts.append(
                    {
                        "attempt": idx,
                        "symbol": attempted_item.symbol,
                        "side": attempted_item.direction,
                        "rank": attempted_item.rank,
                        "score": attempted_item.score,
                        "fund_confirm": f"{attempted_item.fund_confirm_count}/{attempted_item.fund_confirm_total}",
                        "fake_breakout_risk": attempted_item.fake_breakout_risk,
                        "provider": "generation_gate",
                        "action": attempted_plan.action,
                        "confidence": attempted_plan.confidence,
                        "reason": attempted_plan.reason,
                        "wait_type": attempted_plan.wait_type,
                        "validation_ok": False,
                        "validation_reason": ",".join(gate_reasons[:4]),
                        "opens": False,
                        "generation_gate": generation_gate,
                        "strategy_geometry_sample": geometry_sample,
                        "plan": _plan_snapshot(attempted_plan),
                    }
                )
                if selected_plan is None:
                    selected_item = attempted_item
                    selected_plan = attempted_plan
                    selected_provider = "generation_gate"
                    selected_plan_reason = ",".join(gate_reasons[:4])
                continue
            attempted_plan = await self._generate_plan(
                attempted_item,
                performance,
                candidate_source,
                candidate_attempts,
                strategy_geometry_sample=geometry_sample,
            )
            attempted_provider = _plan_provider(attempted_plan)
            attempted_ok, attempted_reason = strategy_validator.validate(attempted_plan)
            attempted_ai_generated = _accepted_strategy_provider(attempted_provider)
            attempted_opens = attempted_plan.action in {"OPEN_LONG", "OPEN_SHORT"}
            plan_can_open = bool(attempted_ok and attempted_ai_generated and attempted_opens)
            plan_attempts.append(
                {
                    "attempt": idx,
                    "symbol": attempted_item.symbol,
                    "side": attempted_item.direction,
                    "rank": attempted_item.rank,
                    "score": attempted_item.score,
                    "fund_confirm": f"{attempted_item.fund_confirm_count}/{attempted_item.fund_confirm_total}",
                    "fake_breakout_risk": attempted_item.fake_breakout_risk,
                    "provider": attempted_provider,
                    "action": attempted_plan.action,
                    "confidence": attempted_plan.confidence,
                    "reason": attempted_plan.reason,
                    "wait_type": attempted_plan.wait_type,
                    "validation_ok": attempted_ok,
                    "validation_reason": attempted_reason,
                    "opens": plan_can_open,
                    "strategy_geometry_sample": geometry_sample,
                    "plan": _plan_snapshot(attempted_plan),
                }
            )
            if selected_plan is None or plan_can_open:
                selected_item = attempted_item
                selected_plan = attempted_plan
                selected_provider = attempted_provider
                selected_plan_ok = attempted_ok
                selected_plan_reason = attempted_reason
                selected_ai_generated = attempted_ai_generated
                selected_opens = attempted_opens
            if not plan_can_open:
                continue

            if account is None or account_summary is None:
                account_summary, account = await autotrader._account_context(open_positions)
            exec_plan = auto_trading_risk_model.decide(attempted_item, attempted_plan, account, market, paper_probe=False)
            risk_ok = exec_plan.decision == "OPEN" and exec_plan.mode == "live"
            risk_attempts.append(
                {
                    "attempt": idx,
                    "symbol": attempted_item.symbol,
                    "side": attempted_item.direction,
                    "decision": exec_plan.decision,
                    "live_mode_ok": exec_plan.mode == "live",
                    "reason": exec_plan.reason,
                    "mode": exec_plan.mode,
                    "margin": exec_plan.dynamic_margin,
                    "notional": exec_plan.notional,
                    "risk_usdt": exec_plan.risk_usdt,
                    "risk_pct": exec_plan.risk_pct,
                }
            )
            selected_exec_plan = exec_plan
            selected_risk_ok = risk_ok
            if risk_ok:
                selected_item = attempted_item
                selected_plan = attempted_plan
                selected_provider = attempted_provider
                selected_plan_ok = attempted_ok
                selected_plan_reason = attempted_reason
                selected_ai_generated = attempted_ai_generated
                selected_opens = attempted_opens
                break

        return {
            "item": selected_item,
            "plan": selected_plan,
            "provider": selected_provider,
            "plan_ok": selected_plan_ok,
            "plan_reason": selected_plan_reason,
            "ai_generated": selected_ai_generated,
            "opens": selected_opens,
            "attempted_candidates": max_plan_attempts,
            "plan_attempts": plan_attempts,
            "exec_plan": selected_exec_plan,
            "risk_ok": selected_risk_ok,
            "risk_attempts": risk_attempts,
            "account_summary": account_summary or {},
        }

    def _stale_wait_plan(self, item: RadarItem, freshness: dict[str, Any]) -> StrategyPlan:
        return StrategyPlan(
            strategy_id=f"freshness_wait_{item.symbol}_{now_ms()}",
            action="WAIT",
            symbol=item.symbol,
            side="NEUTRAL",
            entry_zone_low=float(item.price or 0.0),
            entry_zone_high=float(item.price or 0.0),
            ideal_entry_price=float(item.price or 0.0),
            stop_loss=0.0,
            tp1=0.0,
            tp2=0.0,
            confidence=0.0,
            reason="candidate failed pre-Codex freshness guard",
            wait_type="CANDIDATE_STALE",
            expire_after_seconds=60,
            raw={"provider": "freshness_guard", "freshness": freshness},
        )

    def _blocked_generation_gate(
        self,
        item: RadarItem,
        *,
        candidate_source: str = "",
        strategy_geometry_sample: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        try:
            feedback = ai_strategy_feedback.evaluate_candidate(item)
        except Exception:
            return {}
        candidate_feedback = self._candidate_feedback_payload(feedback)
        gate = candidate_feedback.get("generation_gate") if isinstance(candidate_feedback, dict) else {}
        if isinstance(gate, dict) and gate.get("allow_open_plan") is False:
            if self._strict_geometry_overrides_generation_gate(
                candidate_source=candidate_source,
                gate=gate,
                strategy_geometry_sample=strategy_geometry_sample,
            ):
                return {}
            return gate
        if isinstance(gate, dict) and gate.get("review_required") and not self._has_material_review_improvement(candidate_feedback):
            reasons = list(dict.fromkeys([*_gate_reasons(gate), "review_required_without_material_improvement"]))
            delta = candidate_feedback.get("candidate_learning_delta")
            return {
                **gate,
                "allow_open_plan": False,
                "reasons": reasons,
                "blocked_by": "production_acceptance_review_material_improvement_gate",
                "candidate_learning_delta": delta if isinstance(delta, dict) else {},
            }
        if isinstance(gate, dict) and gate.get("review_required") and self._has_losing_risk_overlap(candidate_feedback):
            reasons = list(dict.fromkeys([*_gate_reasons(gate), "review_required_losing_risk_overlap"]))
            delta = candidate_feedback.get("candidate_learning_delta")
            return {
                **gate,
                "allow_open_plan": False,
                "reasons": reasons,
                "blocked_by": "production_acceptance_review_losing_risk_overlap_gate",
                "candidate_learning_delta": delta if isinstance(delta, dict) else {},
            }
        if isinstance(gate, dict) and self._has_losing_risk_overlap(candidate_feedback):
            reasons = list(dict.fromkeys([*_gate_reasons(gate), "candidate_losing_risk_overlap"]))
            delta = candidate_feedback.get("candidate_learning_delta")
            return {
                **gate,
                "allow_open_plan": False,
                "reasons": reasons,
                "blocked_by": "production_acceptance_losing_risk_overlap_gate",
                "candidate_learning_delta": delta if isinstance(delta, dict) else {},
            }
        return {}

    def _candidate_feedback_payload(self, feedback: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(feedback, dict):
            return {}
        nested = feedback.get("candidate_feedback")
        if isinstance(nested, dict) and nested:
            return nested
        return feedback

    def _has_material_review_improvement(self, candidate_feedback: dict[str, Any]) -> bool:
        if not isinstance(candidate_feedback, dict):
            return False
        delta = candidate_feedback.get("candidate_learning_delta")
        if not isinstance(delta, dict):
            return False
        improvements = delta.get("material_improvements_vs_losses")
        return bool(isinstance(improvements, list) and [item for item in improvements if str(item or "").strip()])

    def _has_losing_risk_overlap(self, candidate_feedback: dict[str, Any]) -> bool:
        if not isinstance(candidate_feedback, dict):
            return False
        delta = candidate_feedback.get("candidate_learning_delta")
        if not isinstance(delta, dict):
            return False
        overlaps = delta.get("overlaps_with_losing_risks")
        if not isinstance(overlaps, list):
            return False
        for row in overlaps:
            if not isinstance(row, dict):
                continue
            if str(row.get("name") or "").strip() and _safe_int(row.get("count")) > 0:
                return True
        return False

    def _strict_geometry_overrides_generation_gate(
        self,
        *,
        candidate_source: str,
        gate: dict[str, Any],
        strategy_geometry_sample: dict[str, Any] | None,
    ) -> bool:
        if str(candidate_source or "") != "strict_geometry":
            return False
        if not isinstance(gate, dict):
            return False
        if set(_gate_reasons(gate)) != {"cyqnt_estimated_win_rate_low"}:
            return False
        if not isinstance(strategy_geometry_sample, dict) or not strategy_geometry_sample:
            return False
        return not self._strategy_geometry_reasons(strategy_geometry_sample)

    def _strict_geometry_ai_feedback_override(self, item: RadarItem, strategy_geometry_sample: dict[str, Any]) -> dict[str, Any]:
        try:
            feedback = copy.deepcopy(ai_strategy_feedback.compact_context(item))
        except Exception:
            return {}
        candidate_feedback = feedback.get("candidate_feedback") if isinstance(feedback.get("candidate_feedback"), dict) else {}
        gate = candidate_feedback.get("generation_gate") if isinstance(candidate_feedback.get("generation_gate"), dict) else {}
        if not self._strict_geometry_overrides_generation_gate(
            candidate_source="strict_geometry",
            gate=gate,
            strategy_geometry_sample=strategy_geometry_sample,
        ):
            return {}
        candidate_feedback["generation_gate"] = {
            **gate,
            "allow_open_plan": True,
            "review_required": False,
            "reasons": [],
            "override_reason": "strict_geometry_supported",
            "overridden_generation_gate": gate,
            "instruction": (
                "Strict production selection was blocked only by cyqnt_estimated_win_rate_low, "
                "but first-touch strategy geometry passed all local production gates. Codex may generate "
                "an OPEN plan only if it uses the selected geometry and states the historical weakness in risk controls."
            ),
        }
        feedback["candidate_feedback"] = candidate_feedback
        feedback["strict_geometry_override"] = {
            "enabled": True,
            "reason": "strict_geometry_supported",
            "strategy_geometry_sample": strategy_geometry_sample,
        }
        return feedback

    def _generation_gate_wait_plan(self, item: RadarItem, gate: dict[str, Any]) -> StrategyPlan:
        reasons = _gate_reasons(gate)
        reason = "generation_gate_blocked"
        if reasons:
            reason = f"{reason}:{','.join(reasons[:6])}"
        price = float(item.price or 0.0)
        return StrategyPlan(
            strategy_id=f"generation_gate_wait_{item.symbol}_{now_ms()}",
            action="WAIT",
            symbol=item.symbol,
            side="NEUTRAL",
            entry_zone_low=price,
            entry_zone_high=price,
            ideal_entry_price=price,
            stop_loss=0.0,
            tp1=0.0,
            tp2=0.0,
            confidence=0.0,
            reason=reason,
            wait_type="GENERATION_GATE_BLOCKED",
            expire_after_seconds=300,
            raw={"provider": "generation_gate", "generation_gate": gate},
        )

    async def _geometry_rank_candidates(
        self,
        candidates: list[RadarItem],
        candidate_source: str,
        performance: dict[str, Any],
    ) -> tuple[list[RadarItem], list[dict[str, Any]], dict[str, dict[str, Any]]]:
        try:
            ordered, reports = await autotrader._geometry_supported_candidate_order(candidates, candidate_source, performance)
        except Exception as exc:
            return (
                candidates,
                [
                    {
                        "candidate_source": candidate_source,
                        "geometry_status": "unavailable",
                        "error": _err(exc),
                    }
                ],
                {},
            )
        ordered = ordered if ordered else candidates
        cache = getattr(autotrader, "_candidate_geometry_samples", {})
        samples: dict[str, dict[str, Any]] = {}
        if isinstance(cache, dict):
            for item in ordered:
                symbol = str(getattr(item, "symbol", "") or "")
                sample = cache.get(symbol)
                if isinstance(sample, dict) and sample:
                    samples[symbol] = sample
        return ordered, reports if isinstance(reports, list) else [], samples

    def _strict_geometry_candidates(self, items: list[RadarItem]) -> list[RadarItem]:
        scored: list[tuple[tuple, RadarItem]] = []
        for item in items:
            try:
                ok, feature, reasons = radar_engine._production_candidate_check(item)
            except Exception:
                continue
            if ok:
                continue
            if set(reasons or []) != {"cyqnt_win_rate_low"}:
                continue
            if float(getattr(feature, "feature_score", 0.0) or 0.0) < 68.0:
                continue
            if float(getattr(feature, "selection_score", 0.0) or 0.0) < 68.0:
                continue
            if not self._strict_geometry_current_context_ok(item, feature):
                continue
            if not self._strict_geometry_feedback_ok(item):
                continue
            scored.append((radar_engine._production_candidate_rank(item, feature), item))
        scored.sort(key=lambda row: row[0], reverse=True)
        return [item for _, item in scored]

    def _strict_geometry_current_context_ok(self, item: RadarItem, feature: Any) -> bool:
        risks = getattr(feature, "failure_risks", [])
        if not isinstance(risks, list):
            risks = []
        hard_current_risks = {
            "flow_negative",
            "funding_negative",
            "wick_above_paper_noise_budget",
            "current_wick_extreme",
            "fake_breakout_risk_high",
            "side_conflict",
        }
        if hard_current_risks.intersection({str(risk) for risk in risks}):
            return False

        side = str(item.direction or "")
        if side not in {"LONG", "SHORT"}:
            return False
        if side == "LONG":
            taker_aligned = float(item.taker_buy_ratio or 0.0) >= 0.55
            depth_aligned = float(item.depth_imbalance or 0.0) >= 0.08
        else:
            taker_aligned = float(item.taker_sell_ratio or 0.0) >= 0.55
            depth_aligned = float(item.depth_imbalance or 0.0) <= -0.08
        if not (taker_aligned or depth_aligned):
            return False

        score_features = item.score_features if isinstance(item.score_features, dict) else {}
        universal_features = score_features.get("universal_anomaly_model")
        universal = universal_features if isinstance(universal_features, dict) else {}
        if universal:
            probabilities = universal.get("probabilities") if isinstance(universal.get("probabilities"), dict) else {}
            side_probability = _safe_float(probabilities.get(side))
            confidence = _safe_float(universal.get("confidence"))
            direction = str(universal.get("direction") or "")
            if direction not in {side, ""}:
                return False
            if side_probability and side_probability < 0.55:
                return False
            if confidence and confidence < 55.0:
                return False
        return True

    def _strict_geometry_feedback_ok(self, item: RadarItem) -> bool:
        try:
            feedback = ai_strategy_feedback.evaluate_candidate(item)
        except Exception:
            return False
        if not isinstance(feedback, dict):
            return False
        candidate_feedback = self._candidate_feedback_payload(feedback)
        if candidate_feedback.get("avoid_repeating"):
            return False
        if self._has_losing_risk_overlap(candidate_feedback):
            return False
        review_lessons = (
            candidate_feedback.get("review_lessons")
            if isinstance(candidate_feedback.get("review_lessons"), list)
            else []
        )
        if not review_lessons:
            return True
        return self._has_material_review_improvement(candidate_feedback)

    async def _strategy_geometry_sample(
        self,
        item: RadarItem,
        candidate_geometry_samples: dict[str, dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        cached = {}
        if isinstance(candidate_geometry_samples, dict):
            cached = candidate_geometry_samples.get(item.symbol) or {}
        if isinstance(cached, dict) and cached and self._strategy_geometry_cache_current(item, cached):
            return cached
        try:
            sample = await strategy_geometry_sampler.evaluate(item)
        except Exception as exc:
            return {
                "enabled": True,
                "status": "unavailable",
                "reason": f"geometry_sample_error:{type(exc).__name__}",
                "symbol": item.symbol,
                "side": item.direction,
                "samples": {"sample_count": 0, "pass_gate": False},
            }
        if isinstance(sample, dict):
            return sample
        return {
            "enabled": True,
            "status": "unavailable",
            "reason": "invalid_geometry_sample",
            "symbol": item.symbol,
            "side": item.direction,
            "samples": {"sample_count": 0, "pass_gate": False},
        }

    def _strategy_geometry_cache_current(self, item: RadarItem, sample: dict[str, Any]) -> bool:
        if not isinstance(sample, dict) or not sample:
            return False
        sample_symbol = str(sample.get("symbol") or "")
        if sample_symbol and sample_symbol != item.symbol:
            return False
        selected = sample.get("selected_geometry") if isinstance(sample.get("selected_geometry"), dict) else {}
        sample_side = str(selected.get("side") or sample.get("side") or "")
        if sample_side and sample_side != str(item.direction or ""):
            return False
        current_price = _safe_float(getattr(item, "price", 0.0))
        if current_price <= 0:
            return True
        zone_low = _safe_float(selected.get("entry_zone_low"))
        zone_high = _safe_float(selected.get("entry_zone_high"))
        entry = _safe_float(selected.get("entry"))
        if (zone_low <= 0 or zone_high <= 0) and entry > 0:
            zone_low = entry * 0.999
            zone_high = entry * 1.001
        if zone_low <= 0 or zone_high <= 0:
            return True
        low, high = sorted((zone_low, zone_high))
        pad = current_price * 0.0005
        return (low - pad) <= current_price <= (high + pad)

    def _strategy_geometry_reasons(self, sample: dict[str, Any]) -> list[str]:
        samples = sample.get("samples") if isinstance(sample.get("samples"), dict) else {}
        reasons: list[str] = []
        if sample.get("status") != "ok" or not bool(samples.get("pass_gate")):
            reasons.append("strategy_geometry_sample_not_ok")
        sample_count = _safe_int(samples.get("sample_count"))
        min_sample_count = max(60, int(getattr(settings, "event_calibration_min_samples", 20) or 20))
        if sample_count < min_sample_count:
            reasons.append("strategy_geometry_sample_count_low")
        if _safe_float(samples.get("win_rate")) < float(settings.strategy_min_paper_win_rate):
            reasons.append("strategy_geometry_win_rate_low")
        if _safe_float(samples.get("expected_r")) < float(settings.strategy_min_expected_r):
            reasons.append("strategy_geometry_expected_r_low")
        if _safe_float(samples.get("profit_factor")) < STRATEGY_GEOMETRY_MIN_PROFIT_FACTOR:
            reasons.append("strategy_geometry_profit_factor_low")
        return list(dict.fromkeys(reasons))

    def _has_strategy_geometry_supported_candidate(
        self,
        candidates: list[RadarItem],
        candidate_geometry_samples: dict[str, dict[str, Any]],
    ) -> bool:
        if not candidates or not isinstance(candidate_geometry_samples, dict):
            return False
        for item in candidates:
            sample = candidate_geometry_samples.get(item.symbol)
            if isinstance(sample, dict) and sample and not self._strategy_geometry_reasons(sample):
                return True
        return False

    def _geometry_supported_candidates(
        self,
        candidates: list[RadarItem],
        candidate_geometry_samples: dict[str, dict[str, Any]],
    ) -> list[RadarItem]:
        if not candidates or not isinstance(candidate_geometry_samples, dict):
            return []
        supported: list[RadarItem] = []
        for item in candidates:
            sample = candidate_geometry_samples.get(item.symbol)
            if isinstance(sample, dict) and sample and not self._strategy_geometry_reasons(sample):
                supported.append(item)
        return supported

    def _geometry_rejected_symbols(
        self,
        candidates: list[RadarItem],
        candidate_geometry_samples: dict[str, dict[str, Any]],
    ) -> list[str]:
        if not candidates:
            return []
        rejected: list[str] = []
        samples = candidate_geometry_samples if isinstance(candidate_geometry_samples, dict) else {}
        for item in candidates:
            sample = samples.get(item.symbol)
            if not isinstance(sample, dict) or not sample or self._strategy_geometry_reasons(sample):
                rejected.append(item.symbol)
        return rejected

    def _generation_gate_supported_candidates(
        self,
        candidates: list[RadarItem],
        candidate_source: str,
        candidate_geometry_samples: dict[str, dict[str, Any]],
    ) -> tuple[list[RadarItem], list[dict[str, Any]]]:
        if not candidates:
            return [], []
        supported: list[RadarItem] = []
        rejections: list[dict[str, Any]] = []
        samples = candidate_geometry_samples if isinstance(candidate_geometry_samples, dict) else {}
        for item in candidates:
            sample = samples.get(item.symbol)
            gate = self._blocked_generation_gate(
                item,
                candidate_source=candidate_source,
                strategy_geometry_sample=sample if isinstance(sample, dict) else None,
            )
            if gate:
                rejection = {
                    "symbol": item.symbol,
                    "reasons": _gate_reasons(gate),
                    "review_required": bool(gate.get("review_required")),
                    "review_reasons": [str(reason) for reason in gate.get("review_reasons") or [] if str(reason)],
                    "blocked_by": str(gate.get("blocked_by") or "generation_gate"),
                }
                delta = gate.get("candidate_learning_delta")
                if isinstance(delta, dict) and delta:
                    rejection["candidate_learning_delta"] = delta
                rejections.append(rejection)
            else:
                supported.append(item)
        return supported, rejections

    def _chain_has_open_strategy(self, chain: dict[str, Any]) -> bool:
        return bool(
            isinstance(chain, dict)
            and chain.get("plan_ok")
            and chain.get("ai_generated")
            and chain.get("opens")
        )

    def _strategy_geometry_wait_plan(self, item: RadarItem, sample: dict[str, Any], reasons: list[str]) -> StrategyPlan:
        reason = "strategy_geometry_gate_blocked"
        if reasons:
            reason = f"{reason}:{','.join(reasons[:6])}"
        price = float(item.price or 0.0)
        return StrategyPlan(
            strategy_id=f"strategy_geometry_wait_{item.symbol}_{now_ms()}",
            action="WAIT",
            symbol=item.symbol,
            side="NEUTRAL",
            entry_zone_low=price,
            entry_zone_high=price,
            ideal_entry_price=price,
            stop_loss=0.0,
            tp1=0.0,
            tp2=0.0,
            confidence=0.0,
            reason=reason,
            wait_type="STRATEGY_GEOMETRY_GATE_BLOCKED",
            expire_after_seconds=300,
            raw={
                "provider": "strategy_geometry_gate",
                "strategy_geometry_sample": sample,
                "strategy_geometry_reasons": reasons,
            },
        )

    def _strategy_geometry_attempt(
        self,
        attempt: int,
        item: RadarItem,
        plan: StrategyPlan,
        sample: dict[str, Any],
        reasons: list[str],
    ) -> dict[str, Any]:
        return {
            "attempt": attempt,
            "symbol": item.symbol,
            "side": item.direction,
            "rank": item.rank,
            "score": item.score,
            "fund_confirm": f"{item.fund_confirm_count}/{item.fund_confirm_total}",
            "fake_breakout_risk": item.fake_breakout_risk,
            "provider": "strategy_geometry_gate",
            "action": plan.action,
            "confidence": plan.confidence,
            "reason": plan.reason,
            "wait_type": plan.wait_type,
            "validation_ok": False,
            "validation_reason": reasons[0] if reasons else "strategy_geometry_sample_not_ok",
            "opens": False,
            "strategy_geometry_reasons": reasons,
            "strategy_geometry_sample": sample,
            "plan": _plan_snapshot(plan),
        }

    def _live_gate(self, mode: str, readiness: dict[str, Any]) -> tuple[bool, str]:
        if settings.trade_mode != "live":
            return False, "trade_mode_not_live"
        if not settings.live_trading_enabled:
            return False, "live_trading_disabled"
        if not binance_futures.configured():
            return False, "binance_not_configured"
        if binance_rest.last_public_source != "mainnet":
            return False, "market_data_not_mainnet"
        if mode == "exchange_test_order":
            return (True, "ok") if settings.live_use_test_order else (False, "live_use_test_order_false")
        if settings.live_use_test_order:
            return False, "live_use_test_order_true_blocks_real_order"
        phase = _phase(readiness, "micro_live") or _phase(readiness, "scale_live")
        if phase and not phase.get("allowed"):
            # Production acceptance already requires explicit confirmation and
            # live_trading_enabled=true before reaching this branch. Do not let
            # the read-only readiness warning create an impossible real-order gate.
            blockers = [
                b
                for b in phase.get("blockers") or []
                if b.get("code") != "live_trading_already_enabled"
            ]
            if not blockers:
                return True, "ok"
            codes = [b.get("code") for b in blockers]
            return False, "live_readiness_blocked:" + ",".join([str(c) for c in codes if c])
        return True, "ok"

    async def _wait_for_close(self, position_id: str, manage_seconds: int) -> dict[str, Any]:
        deadline = now_ms() + manage_seconds * 1000
        while now_ms() <= deadline:
            if position_id not in position_registry.open:
                return _closed_snapshot(position_id)
            await position_manager.manage_all()
            if manage_seconds <= 0:
                break
            await asyncio.sleep(min(2, max(1, manage_seconds)))
        return _closed_snapshot(position_id)

    def _learning_close_recorded(self, strategy_id: str, position_id: str) -> bool:
        strategy = strategy_registry.get(strategy_id) or {}
        forward = strategy.get("forward") or {}
        return position_id in set(forward.get("closed_position_ids") or [])

    def _report(self, *, mode: str, started_ms: int, stages: list[dict[str, Any]], result: dict[str, Any]) -> dict[str, Any]:
        by_name = {stage["name"]: stage for stage in stages}
        passed = mode == "real_order" and all(by_name.get(name, {}).get("ok") for name in PRODUCTION_ACCEPTANCE_REQUIRED_STAGES)
        return {
            "ok": passed,
            "mode": mode,
            "started_ms": started_ms,
            "finished_ms": now_ms(),
            "production_acceptance": {
                "passed": passed,
                "standard": [
                    "AI scans live market data",
                    "AI generates an OPEN strategy plan",
                    "risk model approves live execution",
                    "live executor submits an exchange order",
                    "exchange returns real order evidence",
                    "position manager reviews the position lifecycle",
                    "closed trade is recorded for AI learning",
                ],
                "paper_or_test_order_counts_as_production": False,
            },
            "stages": stages,
            "result": result,
        }

    def _stage(self, name: str, ok: bool, evidence: dict[str, Any]) -> dict[str, Any]:
        return {"name": name, "ok": bool(ok), "evidence": evidence}

    def _store(self, report: dict[str, Any]) -> None:
        self.last_report = report
        db.set_kv("production_acceptance.last_report", report)


def _phase(readiness: dict[str, Any], name: str) -> dict[str, Any]:
    for phase in readiness.get("phases") or []:
        if phase.get("name") == name:
            return phase
    return {}


def _provider_status(ai_status: dict[str, Any]) -> dict[str, Any]:
    provider = ai_status.get("provider")
    if provider in {"codex_cli", "deepseek"}:
        return ai_status.get(provider) or {}
    return {"provider": provider, "not_invoked_reason": ai_status.get("not_invoked_reason", "")}


def _gate_reasons(gate: dict[str, Any]) -> list[str]:
    return [str(reason) for reason in gate.get("reasons") or [] if str(reason)]


def _safe_int(value: Any) -> int:
    try:
        return int(float(value or 0))
    except (TypeError, ValueError):
        return 0


def _safe_float(value: Any) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _plan_provider(plan: StrategyPlan) -> str:
    raw = plan.raw if isinstance(plan.raw, dict) else {}
    provider = str(raw.get("provider") or "").strip().lower()
    if provider.startswith("codex_cli"):
        return "codex_cli"
    if provider.startswith("codex"):
        return "codex_cli_unavailable" if "unavailable" in provider else "codex_cli"
    if provider.startswith("deepseek"):
        return "deepseek_unavailable" if "unavailable" in provider else "deepseek"
    return provider


def _accepted_strategy_provider(provider: str) -> bool:
    provider = str(provider or "").strip().lower()
    if provider.endswith("_unavailable"):
        return False
    if settings.require_codex_strategy_for_entry:
        return provider == "codex_cli"
    return provider in {"codex_cli", "deepseek", "openai"}


def _plan_snapshot(plan: StrategyPlan) -> dict[str, Any]:
    return {
        "strategy_id": plan.strategy_id,
        "action": plan.action,
        "symbol": plan.symbol,
        "side": plan.side,
        "entry_zone_low": plan.entry_zone_low,
        "entry_zone_high": plan.entry_zone_high,
        "ideal_entry_price": plan.ideal_entry_price,
        "stop_loss": plan.stop_loss,
        "tp1": plan.tp1,
        "tp2": plan.tp2,
        "confidence": plan.confidence,
        "provider": _plan_provider(plan),
        "contract_present": bool((plan.raw or {}).get("strategy_contract")),
    }


def _account_snapshot(summary: dict[str, Any]) -> dict[str, Any]:
    return {
        "mode": summary.get("mode"),
        "configured": summary.get("configured"),
        "canTrade": summary.get("canTrade"),
        "walletBalance": summary.get("walletBalance"),
        "availableBalance": summary.get("availableBalance"),
        "marginBalance": summary.get("marginBalance"),
        "live_trading_enabled": summary.get("live_trading_enabled"),
    }


def _order_snapshot(order: dict[str, Any]) -> dict[str, Any]:
    allowed = {
        "orderId",
        "symbol",
        "status",
        "clientOrderId",
        "side",
        "type",
        "origQty",
        "executedQty",
        "avgPrice",
        "cumQuote",
        "timeInForce",
        "workingType",
        "stopPrice",
        "testOrder",
    }
    return {key: order.get(key) for key in allowed if key in order}


def _closed_snapshot(position_id: str) -> dict[str, Any]:
    for row in position_registry.list_closed(limit=200):
        if row.get("position_id") == position_id:
            return {
                "position_id": row.get("position_id"),
                "strategy_id": row.get("strategy_id"),
                "symbol": row.get("symbol"),
                "pnl": row.get("pnl"),
                "close_reason": row.get("close_reason"),
                "close_time": row.get("close_time"),
            }
    return {}


def _strategy_learning_snapshot(strategy: dict[str, Any]) -> dict[str, Any]:
    forward = strategy.get("forward") or {}
    return {
        "strategy_id": strategy.get("strategy_id"),
        "status": strategy.get("status"),
        "metrics": strategy.get("metrics"),
        "open_position_ids": forward.get("open_position_ids"),
        "closed_position_ids": forward.get("closed_position_ids"),
        "sample_count": len(forward.get("samples") or []),
    }


def _err(exc: Exception) -> str:
    return f"{type(exc).__name__}: {exc}"


production_acceptance_runner = ProductionAcceptanceRunner()
