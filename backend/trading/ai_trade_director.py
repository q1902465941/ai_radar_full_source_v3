from __future__ import annotations

from typing import Any

from backend.ai_strategy.ai_service import ai_service
from backend.config import settings
from backend.learning.learning_data_audit import learning_data_audit
from backend.learning.strategy_registry import strategy_registry
from backend.models import new_id, now_ms
from backend.positions.position_manager import position_manager
from backend.positions.position_registry import position_registry
from backend.radar.candidate_feature_enhancer import candidate_feature_enhancer
from backend.radar.radar_engine import radar_engine
from backend.research.jesse_adapter import jesse_research_adapter
from backend.trading.autotrader import autotrader
from backend.trading.live_readiness import live_readiness
from backend.trading.performance_guard import performance_guard


class AITradeDirector:
    def __init__(self) -> None:
        self.last_cycle: dict[str, Any] = {}
        self.cycle_log: list[dict[str, Any]] = []

    async def run_codex_paper_probe(self) -> dict[str, Any]:
        mode = "codex_paper_probe"
        safety = self._paper_probe_safety()
        if safety["live_trading_enabled"]:
            return self._codex_probe_blocked(
                "live_trading_enabled_for_codex_paper_probe",
                safety=safety,
            )

        if not radar_engine.top50:
            await radar_engine.scan()

        market_ok, market_reason = autotrader._market_data_ok()
        performance = performance_guard.summary()
        candidates, candidate_source = autotrader._candidate_batch(performance)
        ai_before = ai_service.status(candidate_count=len(candidates), candidate_source=candidate_source)
        pending_codex_positions = self._codex_open_positions()
        if str(ai_before.get("not_invoked_reason") or "") == "capacity_full" and pending_codex_positions:
            return self._codex_probe_pending_close(
                safety=safety,
                ai_status=ai_before,
                candidate_source=candidate_source,
                candidates=candidates,
                performance=performance,
                open_positions=pending_codex_positions,
            )
        preflight_ok, preflight_reason = self._codex_probe_preflight(
            ai_before,
            candidates,
            market_ok=market_ok,
            market_reason=market_reason,
        )
        if not preflight_ok:
            return self._codex_probe_blocked(
                preflight_reason,
                safety=safety,
                ai_status=ai_before,
                candidate_source=candidate_source,
                candidates=candidates,
                performance=performance,
            )

        result = await self.run_once(source=mode)
        ai_after = ai_service.status(candidate_count=len(candidates), candidate_source=candidate_source)
        decision_path = self._decision_path(result)
        opened = any(row.get("opened") for row in decision_path)
        return {
            "ok": True,
            "mode": mode,
            "sampling_status": "OPENED" if opened else "NO_OPEN",
            "candidate_source": candidate_source,
            "candidate_symbols": [item.symbol for item in candidates],
            "codex_entry": self._codex_entry_status(ai_after),
            "codex_invocation": self._codex_invocation_delta(ai_before, ai_after),
            "decision_path": decision_path,
            "graduation": self._graduation_progress(),
            "market_data": autotrader._market_data_health(),
            "performance": performance,
            "safety": safety,
            "result": result,
            "next_action": (
                "wait_for_position_manager_close_to_create_codex_closed_sample"
                if opened
                else "continue_sampling_until_open_or_market_gate_changes"
            ),
        }

    async def run_once(self, *, source: str = "manual") -> dict[str, Any]:
        cycle_id = new_id("trade_director")
        if not radar_engine.top50:
            await radar_engine.scan()

        performance = performance_guard.summary()
        loop_ok, loop_reason, loop_performance = autotrader.loop_start_guard()
        candidates, candidate_source = autotrader._candidate_batch(performance)
        before = self._snapshot(
            stage="pre_execution",
            cycle_id=cycle_id,
            source=source,
            performance=performance,
            loop_ok=loop_ok,
            loop_reason=loop_reason,
            loop_performance=loop_performance,
            candidates=candidates,
            candidate_source=candidate_source,
        )

        live_order_surface = bool(settings.trade_mode == "live" and settings.live_trading_enabled)
        if not loop_ok and (source == "auto_loop" or live_order_surface):
            result = {
                "results": [
                    {
                        "decision": "DIRECTOR_BLOCKED",
                        "reason": loop_reason,
                        "candidate_source": candidate_source,
                        "candidate_symbols": [item.symbol for item in candidates],
                        "live_order_surface": live_order_surface,
                    }
                ]
            }
            autotrader.last_result = result
            cycle = {
                **before,
                "stage": "blocked",
                "autotrade_result": result,
                "manual_override": False,
            }
            self._record_cycle(cycle)
            return {**result, "trade_director": cycle}

        result = await autotrader.run_once()
        after = self._snapshot(
            stage="post_execution",
            cycle_id=cycle_id,
            source=source,
            performance=performance_guard.summary(),
            loop_ok=loop_ok,
            loop_reason=loop_reason,
            loop_performance=loop_performance,
            candidates=candidates,
            candidate_source=candidate_source,
        )
        cycle = {
            **after,
            "autotrade_result": result,
            "decision_summary": self._decision_summary(result),
            "manual_override": bool(source != "auto_loop" and not loop_ok and not live_order_surface),
        }
        self._record_cycle(cycle)
        if isinstance(result, dict):
            return {**result, "trade_director": cycle}
        return {"results": [], "trade_director": cycle}

    def status(self) -> dict[str, Any]:
        performance = performance_guard.summary()
        loop_ok, loop_reason, loop_performance = autotrader.loop_start_guard()
        candidates, candidate_source = autotrader._candidate_batch(performance) if radar_engine.top50 else ([], "no_scan")
        return self._snapshot(
            stage="status",
            cycle_id=str(self.last_cycle.get("cycle_id") or ""),
            source="status",
            performance=performance,
            loop_ok=loop_ok,
            loop_reason=loop_reason,
            loop_performance=loop_performance,
            candidates=candidates,
            candidate_source=candidate_source,
        )

    def _snapshot(
        self,
        *,
        stage: str,
        cycle_id: str,
        source: str,
        performance: dict[str, Any],
        loop_ok: bool,
        loop_reason: str,
        loop_performance: dict[str, Any],
        candidates: list,
        candidate_source: str,
    ) -> dict[str, Any]:
        ai_status = ai_service.status(candidate_count=len(candidates), candidate_source=candidate_source)
        return {
            "cycle_id": cycle_id,
            "ts_ms": now_ms(),
            "stage": stage,
            "source": source,
            "responsible": "AITradeDirector",
            "responsibility_chain": [
                {"role": "AITradeDirector", "responsibility": "own the end-to-end trade decision lifecycle"},
                {"role": "cyqnt-trd", "responsibility": "local feature, structure, and risk evidence"},
                {"role": "Codex/DeepSeek", "responsibility": "strategy hypothesis and StrategyPlan generation"},
                {"role": "Jesse", "responsibility": "research/backtest audit evidence only"},
                {"role": "risk_model", "responsibility": "sizing, leverage, stops, cost and capital limits"},
                {"role": "executor", "responsibility": "paper/live order execution only after gates pass"},
                {"role": "position_manager", "responsibility": "hold, protect, reduce, and exit open positions"},
                {"role": "learning", "responsibility": "record outcome, attribution, and strategy feedback"},
            ],
            "candidate_source": candidate_source,
            "candidate_symbols": [item.symbol for item in candidates],
            "candidate_lock": autotrader.candidate_lock_status(),
            "candidate_evidence": [self._candidate_evidence(item) for item in candidates[:3]],
            "ai_strategy": ai_status,
            "jesse_research": jesse_research_adapter.status(),
            "performance": performance,
            "loop_start_guard": {
                "ok": loop_ok,
                "reason": loop_reason,
                "performance": loop_performance,
            },
            "positions": {
                "open_count": len(position_registry.list_open()),
                "summary": position_manager.summary(),
            },
            "live_readiness": live_readiness.summary(),
            "safety": {
                "trade_mode": settings.trade_mode,
                "live_trading_enabled": bool(settings.live_trading_enabled),
                "real_order_allowed": bool(settings.trade_mode == "live" and settings.live_trading_enabled),
                "execution_owner": "paper_executor_or_live_executor",
                "live_requires_explicit_enable": True,
            },
            "last_cycle": self.last_cycle,
        }

    def _candidate_evidence(self, item) -> dict[str, Any]:
        cyqnt = candidate_feature_enhancer.evaluate(item).asdict()
        return {
            "symbol": item.symbol,
            "side": item.direction,
            "radar": {
                "rank": item.rank,
                "score": item.score,
                "fund_confirm": f"{item.fund_confirm_count}/{item.fund_confirm_total}",
                "fake_breakout_risk": item.fake_breakout_risk,
                "wick_ratio": item.wick_ratio,
            },
            "cyqnt_feature_enhancement": cyqnt,
            "jesse_audit": jesse_research_adapter.audit_context(item=item),
        }

    def _decision_summary(self, result: dict[str, Any]) -> dict[str, Any]:
        rows = list((result or {}).get("results") or [])
        first = rows[0] if rows else {}
        return {
            "decision": first.get("decision", "NO_RESULT"),
            "symbol": first.get("symbol", ""),
            "reason": first.get("reason", ""),
            "paper_validation": bool(first.get("paper_validation")),
            "opened": str(first.get("decision") or "").startswith("OPEN"),
        }

    def _paper_probe_safety(self) -> dict[str, Any]:
        real_order_allowed = bool(
            settings.trade_mode == "live"
            and settings.live_trading_enabled
            and not settings.live_use_test_order
        )
        return {
            "trade_mode": settings.trade_mode,
            "live_trading_enabled": bool(settings.live_trading_enabled),
            "live_use_test_order": bool(settings.live_use_test_order),
            "real_order_allowed": real_order_allowed,
            "paper_probe_forces_no_live_orders": True,
        }

    def _codex_probe_preflight(
        self,
        ai_status: dict[str, Any],
        candidates: list,
        *,
        market_ok: bool,
        market_reason: str,
    ) -> tuple[bool, str]:
        if not market_ok:
            return False, market_reason or "market_data_unsafe"
        provider = str(ai_status.get("provider") or "")
        codex = ai_status.get("codex_cli") if isinstance(ai_status.get("codex_cli"), dict) else {}
        if not settings.ai_enabled:
            return False, "ai_disabled"
        if provider != "codex_cli":
            return False, f"codex_required_provider_{provider or 'missing'}"
        if not settings.require_codex_strategy_for_entry:
            return False, "codex_strategy_not_required_for_entry"
        if not bool(codex.get("ready_for_generation")):
            return False, str(codex.get("availability_reason") or "codex_unavailable")
        if not candidates:
            return False, str(ai_status.get("not_invoked_reason") or "candidate_filter_empty_before_ai")
        if not bool(ai_status.get("will_invoke_for_current_candidates")):
            return False, str(ai_status.get("not_invoked_reason") or "codex_not_invoked_for_current_candidates")
        return True, "ok"

    def _codex_probe_blocked(
        self,
        reason: str,
        *,
        safety: dict[str, Any],
        ai_status: dict[str, Any] | None = None,
        candidate_source: str = "",
        candidates: list | None = None,
        performance: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        candidates = candidates or []
        return {
            "ok": False,
            "mode": "codex_paper_probe",
            "sampling_status": "BLOCKED",
            "blocked_reason": reason,
            "candidate_source": candidate_source,
            "candidate_symbols": [item.symbol for item in candidates],
            "codex_entry": self._codex_entry_status(ai_status or {}),
            "codex_invocation": self._codex_invocation_delta(ai_status or {}, ai_status or {}),
            "decision_path": [
                {
                    "decision": "CODEX_PAPER_PROBE_BLOCKED",
                    "reason": reason,
                    "opened": False,
                    "observation_recorded": False,
                }
            ],
            "graduation": self._graduation_progress(),
            "market_data": autotrader._market_data_health(),
            "performance": performance or {},
            "safety": safety,
            "result": {"results": [{"decision": "CODEX_PAPER_PROBE_BLOCKED", "reason": reason}]},
            "next_action": "fix_blocker_before_sampling",
        }

    def _codex_probe_pending_close(
        self,
        *,
        safety: dict[str, Any],
        ai_status: dict[str, Any],
        candidate_source: str,
        candidates: list,
        performance: dict[str, Any],
        open_positions: list[dict[str, Any]],
    ) -> dict[str, Any]:
        return {
            "ok": True,
            "mode": "codex_paper_probe",
            "sampling_status": "OPEN_POSITION_PENDING_CLOSE",
            "blocked_reason": "capacity_full_existing_codex_paper_position",
            "candidate_source": candidate_source,
            "candidate_symbols": [item.symbol for item in candidates],
            "codex_entry": self._codex_entry_status(ai_status),
            "codex_invocation": self._codex_invocation_delta(ai_status, ai_status),
            "decision_path": [
                {
                    "symbol": row.get("symbol", ""),
                    "candidate_source": candidate_source,
                    "decision": "OPEN_POSITION_PENDING_CLOSE",
                    "reason": "capacity_full_existing_codex_paper_position",
                    "opened": True,
                    "position_id": row.get("position_id", ""),
                    "observation_recorded": False,
                    "observation_id": "",
                }
                for row in open_positions
            ],
            "open_positions": open_positions,
            "graduation": self._graduation_progress(),
            "market_data": autotrader._market_data_health(),
            "performance": performance,
            "safety": safety,
            "result": {
                "results": [
                    {
                        "decision": "OPEN_POSITION_PENDING_CLOSE",
                        "reason": "capacity_full_existing_codex_paper_position",
                        "position_id": row.get("position_id", ""),
                        "symbol": row.get("symbol", ""),
                    }
                    for row in open_positions
                ]
            },
            "next_action": "wait_for_position_manager_close_to_create_codex_closed_sample",
        }

    def _codex_entry_status(self, ai_status: dict[str, Any]) -> dict[str, Any]:
        codex = ai_status.get("codex_cli") if isinstance(ai_status.get("codex_cli"), dict) else {}
        return {
            "ai_enabled": bool(settings.ai_enabled),
            "provider": str(ai_status.get("provider") or ""),
            "required_for_entry": bool(settings.require_codex_strategy_for_entry),
            "ready_for_generation": bool(codex.get("ready_for_generation")),
            "availability_reason": str(codex.get("availability_reason") or ""),
            "will_invoke_for_current_candidates": bool(ai_status.get("will_invoke_for_current_candidates")),
            "not_invoked_reason": str(ai_status.get("not_invoked_reason") or ""),
            "last_status": str(codex.get("last_status") or ""),
            "last_symbol": str(codex.get("last_symbol") or ""),
            "last_action": str(codex.get("last_action") or ""),
        }

    def _codex_open_positions(self) -> list[dict[str, Any]]:
        out = []
        for position in position_registry.list_open():
            strategy = strategy_registry.get(position.strategy_id) or {}
            provider = str(strategy.get("provider") or "")
            source = str(strategy.get("source") or "")
            if provider != "codex_cli" and source != "ai_generated_codex_cli":
                continue
            out.append(
                {
                    "position_id": position.position_id,
                    "strategy_id": position.strategy_id,
                    "symbol": position.symbol,
                    "side": position.side,
                    "status": position.status,
                    "provider": "codex_cli",
                    "source": source or "ai_generated_codex_cli",
                    "open_time": position.open_time,
                    "entry_price": position.entry_price,
                    "current_price": position.current_price,
                    "unrealized_pnl": position.unrealized_pnl,
                    "lifecycle_state": position.lifecycle_state,
                }
            )
        return out

    def _codex_invocation_delta(self, before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
        before_codex = before.get("codex_cli") if isinstance(before.get("codex_cli"), dict) else {}
        after_codex = after.get("codex_cli") if isinstance(after.get("codex_cli"), dict) else {}
        before_count = int(before_codex.get("invocation_count") or 0)
        after_count = int(after_codex.get("invocation_count") or 0)
        return {
            "invoked": after_count > before_count,
            "before_count": before_count,
            "after_count": after_count,
            "delta": max(0, after_count - before_count),
            "before_last_status": str(before_codex.get("last_status") or ""),
            "after_last_status": str(after_codex.get("last_status") or ""),
            "after_last_symbol": str(after_codex.get("last_symbol") or ""),
            "after_last_action": str(after_codex.get("last_action") or ""),
        }

    def _decision_path(self, result: dict[str, Any]) -> list[dict[str, Any]]:
        rows = list((result or {}).get("results") or [])
        out = []
        for row in rows:
            observation = row.get("ai_decision_observation") if isinstance(row, dict) else {}
            decision = str(row.get("decision") or "") if isinstance(row, dict) else ""
            out.append(
                {
                    "symbol": str(row.get("symbol") or "") if isinstance(row, dict) else "",
                    "candidate_source": str(row.get("candidate_source") or "") if isinstance(row, dict) else "",
                    "decision": decision,
                    "reason": str(row.get("reason") or "") if isinstance(row, dict) else "",
                    "opened": decision.startswith("OPEN"),
                    "position_id": str(row.get("position_id") or "") if isinstance(row, dict) else "",
                    "observation_recorded": bool((observation or {}).get("recorded")),
                    "observation_id": str((observation or {}).get("observation_id") or ""),
                }
            )
        return out

    def _graduation_progress(self) -> dict[str, Any]:
        report = learning_data_audit.compact()
        sources = report.get("sources") if isinstance(report.get("sources"), dict) else {}
        market_backtest = report.get("market_backtest") if isinstance(report.get("market_backtest"), dict) else {}
        return {
            "production_grade": bool(report.get("production_grade")),
            "trust_level": str(report.get("trust_level") or ""),
            "real_closed_samples_with_radar": int(sources.get("real_closed_samples_with_radar") or 0),
            "codex_real_closed_samples_with_radar": int(sources.get("codex_real_closed_samples_with_radar") or 0),
            "real_closed_samples_by_provider": sources.get("real_closed_samples_by_provider") or {},
            "market_backtest_available": bool(market_backtest.get("available")),
            "market_backtest_quality_passed": bool(market_backtest.get("quality_passed")),
        }

    def _record_cycle(self, cycle: dict[str, Any]) -> None:
        self.last_cycle = cycle
        self.cycle_log.insert(0, cycle)
        self.cycle_log = self.cycle_log[:50]


ai_trade_director = AITradeDirector()
