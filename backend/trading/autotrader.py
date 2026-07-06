from __future__ import annotations

import asyncio
from asyncio import Lock
from dataclasses import replace
from typing import Any

from backend.account.account_service import account_service
from backend.ai_strategy.ai_service import ai_service
from backend.ai_strategy.dynamic_trade_model import auto_trading_risk_model
from backend.ai_strategy.openai_strategy_client import openai_strategy_client
from backend.ai_strategy.strategy_validator import strategy_validator
from backend.ai_strategy.wait_manager import wait_manager
from backend.config import settings
from backend.learning.ai_strategy_feedback import ai_strategy_feedback
from backend.learning.learned_risk_guard import learned_risk_guard
from backend.learning.strategy_geometry_sampler import strategy_geometry_sampler
from backend.learning.strategy_filter import direction_confirmations as strategy_direction_confirmations
from backend.learning.strategy_filter import strategy_matches
from backend.learning.strategy_registry import strategy_registry
from backend.market.market_service import market_service
from backend.market.binance_factor_source import binance_factor_source
from backend.models import now_ms
from backend.positions.position_registry import position_registry
from backend.radar.candidate_feature_enhancer import candidate_feature_enhancer
from backend.radar.radar_engine import radar_engine
from backend.storage.db import db
from backend.trading.live_executor import live_executor
from backend.trading.paper_executor import paper_executor
from backend.trading.performance_guard import performance_guard
from backend.trading.prg.readiness_engine import readiness_engine


class AutoTrader:
    def __init__(self):
        self.enabled = bool(settings.auto_trading_enabled)
        self.global_lock = Lock()
        self.symbol_locks: dict[str, Lock] = {}
        self.executed_strategy_ids = set()
        self.last_result = {}
        self.ai_candidate_lock: dict[str, Any] = {}
        self.ai_candidate_wait_cooldowns: dict[str, int] = {}
        self._candidate_geometry_samples: dict[str, dict[str, Any]] = {}

    def _symbol_lock(self, symbol):
        self.symbol_locks.setdefault(symbol, Lock())
        return self.symbol_locks[symbol]

    def loop_start_guard(self) -> tuple[bool, str, dict]:
        performance_context = performance_guard.summary()
        paper_closed_loop = not (settings.trade_mode == "live" and settings.live_trading_enabled)
        mode = str(settings.auto_trading_candidate_mode).lower()
        if paper_closed_loop:
            if not settings.auto_trading_use_performance_guard:
                return False, "performance_guard_required_for_auto_loop", performance_context
            if mode not in {"strict", "paper_top"}:
                return False, "invalid_candidate_mode_for_paper_loop", performance_context
            if performance_context.get("recovery_mode") and not (
                settings.paper_probe_enabled and settings.paper_loop_allow_recovery
            ):
                return False, "recovery_mode_blocks_auto_loop_without_paper_probe", performance_context
            if int(settings.max_open_positions or 0) > 3:
                return False, "max_open_positions_too_high_for_auto_loop", performance_context
            reason = "paper_recovery_sampling" if performance_context.get("recovery_mode") else "paper_closed_loop_sampling"
            return True, reason, performance_context
        if performance_context.get("recovery_mode"):
            return False, "recovery_mode_blocks_live_auto_loop", performance_context
        if not settings.auto_trading_use_performance_guard:
            return False, "performance_guard_required_for_auto_loop", performance_context
        if mode != "strict":
            return False, "strict_candidate_mode_required_for_auto_loop", performance_context
        if int(settings.max_open_positions or 0) > 3:
            return False, "max_open_positions_too_high_for_auto_loop", performance_context
        return True, "ok", performance_context

    async def run_once(self):
        async with self.global_lock:
            return await self._run_once_locked()

    async def _run_once_locked(self):
        if not radar_engine.top50:
            await radar_engine.scan()
        market_ok, market_reason = self._market_data_ok()
        if not market_ok:
            self.last_result = {
                "results": [
                    {
                        "decision": "MARKET_DATA_UNSAFE",
                        "reason": market_reason,
                        "market_data": self._market_data_health(),
                    }
                ]
            }
            return self.last_result

        open_positions = len(position_registry.list_open())
        if open_positions >= settings.max_open_positions:
            self.last_result = {
                "results": [
                    {
                        "decision": "CAPACITY_FULL",
                        "reason": f"open_positions={open_positions}, max={settings.max_open_positions}",
                    }
                ]
            }
            return self.last_result

        performance_context = performance_guard.summary()
        candidates, candidate_source = self._candidate_batch(performance_context)
        if not candidates:
            if candidate_source == "paper_top":
                diagnostics = self.candidate_diagnostics(performance_context)
                observation = self._record_gate_observation(
                    decision="NO_CANDIDATES",
                    reason="top5_full_confirm_filter_empty",
                    candidate_source=candidate_source,
                    diagnostics=diagnostics,
                )
                self.last_result = {
                    "results": [
                        {
                            "decision": "NO_CANDIDATES",
                            "reason": "top5_full_confirm_filter_empty",
                            "candidate_source": candidate_source,
                            "performance": performance_guard.summary(),
                            "diagnostics": diagnostics,
                            "ai_decision_observation": observation,
                        }
                    ]
                }
                return self.last_result
            probe_candidates, probe_source = self._paper_probe_batch(performance_context, "paper_probe_no_candidates")
            if probe_candidates:
                candidates, candidate_source = probe_candidates, probe_source
            else:
                diagnostics = self.candidate_diagnostics(performance_context)
                observation = self._record_gate_observation(
                    decision="NO_CANDIDATES",
                    reason="candidate_filter_empty",
                    candidate_source=candidate_source,
                    diagnostics=diagnostics,
                )
                self.last_result = {
                    "results": [
                        {
                            "decision": "NO_CANDIDATES",
                            "reason": "candidate_filter_empty",
                            "candidate_source": candidate_source,
                            "performance": performance_guard.summary(),
                            "diagnostics": diagnostics,
                            "ai_decision_observation": observation,
                        }
                    ]
                }
                return self.last_result
        results = []
        candidates, _geometry_selection = await self._geometry_supported_candidate_order(
            candidates,
            candidate_source,
            performance_context,
        )
        account_summary, account = await self._account_context(open_positions)
        market = {"market_heat": radar_engine.market_heat, "volatility_regime": self._volatility_regime()}
        active_strategy = strategy_registry.active()
        if (
            not self._is_ai_review_source(candidate_source)
            and performance_context.get("recovery_mode")
            and settings.evolved_strategy_required_in_recovery
            and not active_strategy
        ):
            probe_candidates, probe_source = self._paper_probe_batch(performance_context, "paper_probe_learning_required")
            if probe_candidates:
                candidates, candidate_source = probe_candidates, probe_source
                selected_strategies = {}
            else:
                self.last_result = {
                    "results": [
                        {
                            "decision": "LEARNING_REQUIRED",
                            "reason": "recovery_mode_requires_active_evolved_strategy",
                            "performance": performance_context,
                            "diagnostics": self.candidate_diagnostics(performance_context),
                        }
                    ]
                }
                return self.last_result

        use_active_filter = bool(settings.auto_trading_use_active_strategy_filter)
        if (
            active_strategy
            and use_active_filter
            and not self._is_ai_review_source(candidate_source)
            and not self._is_paper_probe_source(candidate_source)
        ):
            pre_filter_candidates = candidates
            selected_strategies = {}
            selected_candidates = []
            for item in pre_filter_candidates:
                matched_strategy = self._best_matching_strategy(item, active_strategy)
                if matched_strategy:
                    selected_candidates.append(item)
                    selected_strategies[item.symbol] = matched_strategy
            candidates = selected_candidates
            if not candidates:
                if candidate_source == "paper_top":
                    self.last_result = {
                        "results": [
                            {
                                "decision": "NO_STRATEGY_MATCH",
                                "reason": "active_evolved_strategy_filtered_all_top5_candidates",
                                "candidate_source": candidate_source,
                                "active_strategy": {
                                    "strategy_id": active_strategy.get("strategy_id"),
                                    "name": active_strategy.get("name"),
                                    "filters": active_strategy.get("filters"),
                                },
                                "diagnostics": {
                                    "candidate_filter": self.candidate_diagnostics(performance_context),
                                    "strategy_filter": self.strategy_selection_diagnostics(active_strategy, pre_filter_candidates),
                                },
                                "performance": performance_context,
                            }
                        ]
                    }
                    return self.last_result
                probe_candidates, probe_source = self._paper_probe_batch(
                    performance_context,
                    "paper_probe_strategy_miss",
                    preferred=pre_filter_candidates,
                )
                if probe_candidates:
                    candidates, candidate_source = probe_candidates, probe_source
                    selected_strategies = {}
                else:
                    self.last_result = {
                        "results": [
                            {
                                "decision": "NO_STRATEGY_MATCH",
                                "reason": "active_evolved_strategy_filtered_all_candidates",
                                "candidate_source": candidate_source,
                                "active_strategy": {
                                    "strategy_id": active_strategy.get("strategy_id"),
                                    "name": active_strategy.get("name"),
                                    "filters": active_strategy.get("filters"),
                                },
                                "diagnostics": {
                                    "candidate_filter": self.candidate_diagnostics(performance_context),
                                    "strategy_filter": self.strategy_selection_diagnostics(active_strategy, pre_filter_candidates),
                                },
                                "performance": performance_context,
                            }
                        ]
                    }
                    return self.last_result
        elif "selected_strategies" not in locals():
            selected_strategies = {}

        attempted_candidate_symbols: set[str] = set()
        for item in candidates:
            attempted_candidate_symbols.add(str(getattr(item, "symbol", "") or ""))
            paper_probe = self._is_paper_probe_source(candidate_source)
            ai_review = self._is_ai_review_source(candidate_source)
            paper_validation = ai_review and self._paper_validation_allowed(item, performance_context)
            selected_strategy = selected_strategies.get(item.symbol, active_strategy)
            if position_registry.has_symbol(item.symbol):
                results.append({"symbol": item.symbol, "decision": "SKIP", "reason": "same_symbol_open"})
                continue
            if not paper_probe and not ai_review and item.fund_confirm_count < min(3, item.fund_confirm_total):
                results.append(
                    {
                        "symbol": item.symbol,
                        "decision": "SKIP",
                        "reason": "fund_confirm_3_required",
                        "fund_confirm": f"{item.fund_confirm_count}/{item.fund_confirm_total}",
                    }
                )
                continue
            if paper_probe and item.fund_confirm_count < max(0, int(settings.paper_probe_min_fund_confirm)):
                results.append(
                    {
                        "symbol": item.symbol,
                        "decision": "SKIP",
                        "reason": "paper_probe_fund_confirm_low",
                        "fund_confirm": f"{item.fund_confirm_count}/{item.fund_confirm_total}",
                    }
                )
                continue

            if paper_probe or ai_review:
                learned_ok, learned_report = True, None
            else:
                learned_ok, learned_report = learned_risk_guard.precheck_item(
                    item,
                    recovery_mode=bool(performance_context.get("recovery_mode")),
                )
            if not learned_ok:
                results.append(
                    {
                        "symbol": item.symbol,
                        "decision": "LEARNED_SKIP",
                        "reason": ",".join(learned_report.reasons[:4]),
                        "learned_guard": learned_report.asdict(),
                    }
                )
                continue

            pre_ok, pre_reason = True, ""
            if not paper_probe and not ai_review and settings.auto_trading_use_performance_guard:
                pre_ok, pre_reason = performance_guard.precheck_candidate(item)
            if not pre_ok:
                results.append(
                    {
                        "symbol": item.symbol,
                        "decision": "SKIP",
                        "reason": pre_reason,
                        "performance": performance_context,
                    }
                )
                continue

            item, pre_ai_refresh = await self._prepare_latest_item_for_ai(item, force_scan=True)
            price_ok, price_reason = self._pre_trade_price_ok(pre_ai_refresh)
            if not price_ok:
                results.append(
                    {
                        "symbol": item.symbol,
                        "candidate_source": candidate_source,
                        "decision": "SKIP_MARKET_PRICE_UNSAFE",
                        "reason": price_reason,
                        "pre_ai_market_refresh": pre_ai_refresh,
                    }
                )
                continue
            fresh_ok, fresh_report = self._ai_candidate_freshness_report(item, candidate_source, performance_context)
            if not fresh_ok:
                lock_released = False
                cooldown_until = 0
                if ai_review:
                    cooldown_until = self._cooldown_stale_candidate(item)
                    lock_released = self._release_ai_candidate_lock(item.symbol)
                retry_candidates = await self._retry_candidates_after_pre_ai_stale(
                    candidate_source,
                    performance_context,
                    attempted_candidate_symbols,
                )
                retry_symbols = []
                if retry_candidates:
                    existing_symbols = {str(getattr(row, "symbol", "") or "") for row in candidates}
                    for retry_item in retry_candidates:
                        retry_symbol = str(getattr(retry_item, "symbol", "") or "")
                        if retry_symbol and retry_symbol not in existing_symbols:
                            candidates.append(retry_item)
                            existing_symbols.add(retry_symbol)
                            retry_symbols.append(retry_symbol)
                observation = self._record_decision_observation(
                    item=item,
                    decision="SKIP_STALE_CANDIDATE",
                    reason=",".join(fresh_report["reasons"][:4]),
                    candidate_source=candidate_source,
                    stage="pre_ai_freshness",
                    paper_validation=paper_validation,
                    context={
                        "freshness": fresh_report,
                        "pre_ai_market_refresh": pre_ai_refresh,
                    },
                )
                results.append(
                    {
                        "symbol": item.symbol,
                        "candidate_source": candidate_source,
                        "decision": "SKIP_STALE_CANDIDATE",
                        "reason": ",".join(fresh_report["reasons"][:4]),
                        "freshness": fresh_report,
                        "pre_ai_market_refresh": pre_ai_refresh,
                        "retry_candidates_added": retry_symbols,
                        "candidate_lock_released": lock_released,
                        "candidate_wait_cooldown_until_ms": cooldown_until,
                        "ai_decision_observation": observation,
                    }
                )
                continue

            plan = await self._generate_strategy_plan(
                item,
                account,
                performance_context,
                candidate_source,
                paper_probe,
                paper_validation,
                selected_strategy,
                pre_ai_refresh=pre_ai_refresh,
                strategy_geometry_sample=self._candidate_geometry_samples.get(item.symbol),
            )
            ok, reason = strategy_validator.validate(plan)
            if not ok:
                observation = self._record_decision_observation(
                    item=item,
                    plan=plan,
                    decision="INVALID_PLAN",
                    reason=reason,
                    candidate_source=candidate_source,
                    stage="strategy_validation",
                    paper_validation=paper_validation,
                )
                results.append(
                    {
                        "symbol": item.symbol,
                        "decision": "INVALID_PLAN",
                        "reason": reason,
                        "ai_decision_observation": observation,
                    }
                )
                continue

            if plan.action == "WAIT":
                wd = wait_manager.evaluate(item, plan)
                lock_released = False
                cooldown_until = 0
                if ai_review:
                    cooldown_until = self._cooldown_ai_wait_candidate(item, plan, wd)
                if ai_review and (cooldown_until or wd.get("decision") in {"EXPIRED", "WAIT_EXPIRED"}):
                    lock_released = self._release_ai_candidate_lock(item.symbol)
                retry_symbols, retry_cooldown_until, retry_lock_released = await self._append_paper_top_retry_candidates(
                    candidates,
                    candidate_source,
                    performance_context,
                    attempted_candidate_symbols,
                    item,
                    cooldown_current=not bool(cooldown_until),
                )
                cooldown_until = cooldown_until or retry_cooldown_until
                lock_released = lock_released or retry_lock_released
                observation = self._record_decision_observation(
                    item=item,
                    plan=plan,
                    decision=wd["decision"],
                    reason=wd["reason"],
                    candidate_source=candidate_source,
                    stage="ai_wait",
                    paper_validation=paper_validation,
                    context={"wait_decision": wd},
                )
                results.append(
                    {
                        "symbol": item.symbol,
                        "candidate_source": candidate_source,
                        "decision": wd["decision"],
                        "reason": wd["reason"],
                        "paper_validation_allowed": paper_validation,
                        "candidate_lock_released": lock_released,
                        "candidate_wait_cooldown_until_ms": cooldown_until,
                        "retry_candidates_added": retry_symbols,
                        "ai_decision_observation": observation,
                    }
                )
                continue

            item, post_ai_refresh = await self._prepare_latest_item_for_ai(item, force_scan=False)
            price_ok, price_reason = self._pre_trade_price_ok(post_ai_refresh)
            if not price_ok:
                results.append(
                    {
                        "symbol": item.symbol,
                        "candidate_source": candidate_source,
                        "decision": "STALE_AFTER_AI",
                        "reason": price_reason,
                        "post_ai_market_refresh": post_ai_refresh,
                    }
                )
                continue
            fresh_ok, fresh_report = self._ai_candidate_freshness_report(item, candidate_source, performance_context)
            if not fresh_ok:
                lock_released = False
                cooldown_until = 0
                if ai_review:
                    cooldown_until = self._cooldown_stale_candidate(item)
                    lock_released = self._release_ai_candidate_lock(item.symbol)
                observation = self._record_decision_observation(
                    item=item,
                    plan=plan,
                    decision="STALE_AFTER_AI",
                    reason=",".join(fresh_report["reasons"][:4]),
                    candidate_source=candidate_source,
                    stage="post_ai_freshness",
                    paper_validation=paper_validation,
                    context={
                        "freshness": fresh_report,
                        "post_ai_market_refresh": post_ai_refresh,
                    },
                )
                results.append(
                    {
                        "symbol": item.symbol,
                        "candidate_source": candidate_source,
                        "decision": "STALE_AFTER_AI",
                        "reason": ",".join(fresh_report["reasons"][:4]),
                        "freshness": fresh_report,
                        "post_ai_market_refresh": post_ai_refresh,
                        "candidate_lock_released": lock_released,
                        "candidate_wait_cooldown_until_ms": cooldown_until,
                        "ai_decision_observation": observation,
                    }
                )
                continue
            if plan.side in {"LONG", "SHORT"} and item.direction in {"LONG", "SHORT"} and plan.side != item.direction:
                drift = await self._regenerate_after_ai_drift(
                    item=item,
                    stale_plan=plan,
                    account=account,
                    performance_context=performance_context,
                    candidate_source=candidate_source,
                    paper_probe=paper_probe,
                    ai_review=ai_review,
                    selected_strategy=selected_strategy,
                )
                if drift.get("proceed"):
                    item = drift["item"]
                    plan = drift["plan"]
                    paper_validation = bool(drift.get("paper_validation"))
                else:
                    results.append(drift["result"])
                    continue

            async with self._symbol_lock(item.symbol):
                if plan.strategy_id in self.executed_strategy_ids:
                    results.append({"symbol": item.symbol, "decision": "SKIP", "reason": "strategy_id_seen"})
                    continue

                execution_item, execution_refresh = await self._prepare_latest_item_for_ai(item, force_scan=False)
                price_ok, price_reason = self._pre_trade_price_ok(execution_refresh)
                if not price_ok:
                    results.append(
                        {
                            "symbol": item.symbol,
                            "candidate_source": candidate_source,
                            "decision": "SKIP_MARKET_PRICE_UNSAFE",
                            "reason": price_reason,
                            "execution_market_refresh": execution_refresh,
                        }
                    )
                    continue
                item = execution_item

                if paper_validation:
                    plan = self._mark_paper_validation_plan(plan, item, candidate_source)
                exec_plan = auto_trading_risk_model.decide(
                    item,
                    plan,
                    account,
                    market,
                    paper_probe=paper_probe,
                )
                if paper_validation and exec_plan.reason.startswith("paper_probe; "):
                    exec_plan = replace(
                        exec_plan,
                        reason=exec_plan.reason.replace("paper_probe; ", "paper_validation; ", 1),
                    )
                quality = plan.raw.get("quality_gate", {})
                performance = plan.raw.get("performance_guard", {})
                learned = plan.raw.get("learned_guard", {})
                if exec_plan.decision not in ["OPEN", "PAPER_ONLY"]:
                    retry_symbols, cooldown_until, lock_released = await self._append_paper_top_retry_candidates(
                        candidates,
                        candidate_source,
                        performance_context,
                        attempted_candidate_symbols,
                        item,
                    )
                    observation = self._record_decision_observation(
                        item=item,
                        plan=plan,
                        decision=exec_plan.decision,
                        reason=exec_plan.reason,
                        candidate_source=candidate_source,
                        stage="risk_model",
                        paper_validation=paper_validation,
                        context={
                            "execution_decision": {
                                "mode": exec_plan.mode,
                                "margin": exec_plan.dynamic_margin,
                                "notional": exec_plan.notional,
                                "leverage": exec_plan.dynamic_leverage,
                                "risk_usdt": exec_plan.risk_usdt,
                                "risk_pct": exec_plan.risk_pct,
                            },
                            "quality": quality,
                            "performance": performance,
                            "learned_guard": learned,
                        },
                    )
                    results.append(
                        {
                            "symbol": item.symbol,
                            "candidate_source": candidate_source,
                            "decision": exec_plan.decision,
                            "reason": exec_plan.reason,
                            "paper_validation_allowed": paper_validation,
                            "drift_regenerated": bool(plan.raw.get("drift_regeneration")),
                            "retry_candidates_added": retry_symbols,
                            "candidate_lock_released": lock_released,
                            "candidate_wait_cooldown_until_ms": cooldown_until,
                            "quality": quality,
                            "performance": performance,
                            "learned_guard": learned,
                            "ai_decision_observation": observation,
                        }
                    )
                    continue

                if (
                    settings.trade_mode == "live"
                    and settings.live_trading_enabled
                    and exec_plan.decision == "OPEN"
                    and exec_plan.mode != "live"
                ):
                    exec_plan = replace(
                        exec_plan,
                        decision="PAPER_ONLY",
                        reason=f"live execution blocked because exec_plan.mode={exec_plan.mode}; {exec_plan.reason}",
                    )

                if (
                    settings.trade_mode == "live"
                    and settings.live_trading_enabled
                    and exec_plan.decision == "OPEN"
                    and exec_plan.mode == "live"
                ):
                    live_ok, live_reason, readiness = self._real_live_execution_guard()
                    if not live_ok:
                        results.append(
                            {
                                "symbol": item.symbol,
                                "candidate_source": candidate_source,
                                "decision": "LIVE_READINESS_BLOCKED",
                                "reason": live_reason,
                                "live_readiness": readiness,
                                "paper_validation_allowed": paper_validation,
                                "drift_regenerated": bool(plan.raw.get("drift_regeneration")),
                            }
                        )
                        continue
                    p = await live_executor.open_position(radar_engine.last_scan_id, plan.strategy_id, item.score, exec_plan)
                    decision_label = "OPEN_LIVE_TEST" if settings.live_use_test_order else "OPEN_LIVE"
                else:
                    p = await paper_executor.open_position(radar_engine.last_scan_id, plan.strategy_id, item.score, exec_plan)
                    if paper_probe:
                        decision_label = "OPEN_PAPER_PROBE"
                    elif paper_validation:
                        decision_label = "OPEN_PAPER_VALIDATION"
                    else:
                        decision_label = "OPEN_PAPER"

                try:
                    ai_feedback = ai_strategy_feedback.record_open(
                        plan=plan,
                        item=item,
                        exec_plan=exec_plan,
                        position=p,
                        candidate_source=candidate_source,
                        paper_validation=paper_validation,
                        selected_strategy_id=selected_strategy.get("strategy_id") if selected_strategy else "",
                    )
                except Exception as exc:
                    ai_feedback = {"recorded": False, "reason": f"feedback_error:{type(exc).__name__}"}

                self.executed_strategy_ids.add(plan.strategy_id)
                results.append(
                    {
                        "symbol": item.symbol,
                        "decision": decision_label,
                        "position_id": p.position_id,
                        "reason": exec_plan.reason,
                        "candidate_source": candidate_source,
                        "execution_context": account.get("execution_context"),
                        "paper_validation": paper_validation,
                        "drift_regenerated": bool(plan.raw.get("drift_regeneration")),
                        "account": account_summary,
                        "execution": {
                            "margin": exec_plan.dynamic_margin,
                            "notional": exec_plan.notional,
                            "leverage": exec_plan.dynamic_leverage,
                            "risk_usdt": exec_plan.risk_usdt,
                            "risk_pct": exec_plan.risk_pct,
                        },
                        "quality": quality,
                        "performance": performance,
                        "learned_guard": learned,
                        "active_strategy_id": selected_strategy.get("strategy_id") if selected_strategy else "",
                        "ai_strategy_feedback": ai_feedback,
                    }
                )
                break

        self.last_result = {"results": results}
        return self.last_result

    async def _prepare_latest_item_for_ai(self, item, *, force_scan: bool) -> tuple[Any, dict[str, Any]]:
        before_scan_id = radar_engine.last_scan_id
        report: dict[str, Any] = {
            "force_scan": bool(force_scan),
            "scan_id_before": before_scan_id,
            "scan_id_after": before_scan_id,
            "scan_ok": True,
            "scan_error": "",
            "symbol_present_after_scan": False,
            "candidate_ts_ms": int(getattr(item, "ts_ms", 0) or 0),
        }
        if force_scan:
            try:
                await asyncio.wait_for(radar_engine.scan(force_refresh=True), timeout=90.0)
            except asyncio.TimeoutError:
                report["scan_ok"] = False
                report["scan_error"] = "radar_scan_timeout"
            except Exception as exc:
                report["scan_ok"] = False
                report["scan_error"] = f"{type(exc).__name__}:{exc}"
        report["scan_id_after"] = radar_engine.last_scan_id
        report.update(self._market_data_health())
        latest = self._latest_candidate_snapshot(item)
        report["symbol_present_after_scan"] = any(row.symbol == getattr(item, "symbol", "") for row in radar_engine.top50)
        refreshed, trade_price = await self._refresh_item_price_with_report(latest)
        report["trade_price"] = trade_price
        report.update(
            {
                "symbol": getattr(refreshed, "symbol", ""),
                "side": getattr(refreshed, "direction", ""),
                "rank": int(getattr(refreshed, "rank", 0) or 0),
                "score": round(float(getattr(refreshed, "score", 0.0) or 0.0), 4),
                "fund_confirm": f"{getattr(refreshed, 'fund_confirm_count', 0)}/{getattr(refreshed, 'fund_confirm_total', 0)}",
                "fake_breakout_risk": getattr(refreshed, "fake_breakout_risk", ""),
                "price": float(getattr(refreshed, "price", 0.0) or 0.0),
                "candidate_ts_ms": int(getattr(refreshed, "ts_ms", 0) or 0),
                "candidate_age_seconds": round(max(0, now_ms() - int(getattr(refreshed, "ts_ms", 0) or 0)) / 1000, 3),
            }
        )
        return refreshed, report

    async def _generate_strategy_plan(
        self,
        item,
        account: dict[str, Any],
        performance_context: dict[str, Any],
        candidate_source: str,
        paper_probe: bool,
        paper_validation: bool,
        selected_strategy: dict[str, Any] | None,
        *,
        retry_context: dict[str, Any] | None = None,
        pre_ai_refresh: dict[str, Any] | None = None,
        strategy_geometry_sample: dict[str, Any] | None = None,
    ):
        candidate_selection = {
            "source": candidate_source,
            "paper_validation": bool(paper_validation),
            "paper_probe": bool(paper_probe),
            "strict_candidate": candidate_source == "strict",
            "latest_market_required": True,
        }
        if retry_context:
            candidate_selection["retry_context"] = retry_context
        if pre_ai_refresh:
            candidate_selection["pre_ai_market_refresh"] = pre_ai_refresh
        if isinstance(strategy_geometry_sample, dict) and strategy_geometry_sample:
            candidate_selection["strategy_geometry_preselected"] = True
            candidate_selection["strategy_geometry_status"] = strategy_geometry_sample.get("status")
            candidate_selection["strategy_geometry_model"] = strategy_geometry_sample.get("sample_model")
        position_context = {
            "open_positions": account["open_positions"],
            "performance_guard": performance_context,
            "candidate_selection": candidate_selection,
            "active_evolved_strategy": {
                "strategy_id": selected_strategy.get("strategy_id"),
                "name": selected_strategy.get("name"),
                "filters": selected_strategy.get("filters"),
                "metrics": selected_strategy.get("metrics"),
            } if selected_strategy else None,
        }
        if isinstance(strategy_geometry_sample, dict) and strategy_geometry_sample:
            position_context["strategy_geometry_sample"] = strategy_geometry_sample
        return await ai_service.generate_strategy(
            item,
            position_context,
        )

    def _record_decision_observation(
        self,
        *,
        item,
        decision: str,
        reason: str,
        candidate_source: str,
        stage: str,
        plan=None,
        paper_validation: bool = False,
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        try:
            return ai_strategy_feedback.record_observation(
                item=item,
                plan=plan,
                decision=decision,
                reason=reason,
                candidate_source=candidate_source,
                stage=stage,
                paper_validation=paper_validation,
                context=context or {},
            )
        except Exception as exc:
            return {"recorded": False, "reason": f"observation_error:{type(exc).__name__}"}

    def _record_gate_observation(
        self,
        *,
        decision: str,
        reason: str,
        candidate_source: str,
        diagnostics: dict[str, Any],
    ) -> dict[str, Any]:
        try:
            return ai_strategy_feedback.record_gate_observation(
                decision=decision,
                reason=reason,
                candidate_source=candidate_source,
                diagnostics=diagnostics,
            )
        except Exception as exc:
            return {"recorded": False, "reason": f"observation_error:{type(exc).__name__}"}

    async def _regenerate_after_ai_drift(
        self,
        *,
        item,
        stale_plan,
        account: dict[str, Any],
        performance_context: dict[str, Any],
        candidate_source: str,
        paper_probe: bool,
        ai_review: bool,
        selected_strategy: dict[str, Any] | None,
    ) -> dict[str, Any]:
        latest_item, pre_ai_refresh = await self._prepare_latest_item_for_ai(item, force_scan=True)
        paper_validation = ai_review and self._paper_validation_allowed(latest_item, performance_context)
        retry_plan = await self._generate_strategy_plan(
            latest_item,
            account,
            performance_context,
            candidate_source,
            paper_probe,
            paper_validation,
            selected_strategy,
            retry_context={
                "reason": "plan_side_mismatch_after_ai",
                "previous_plan_side": stale_plan.side,
                "latest_side": latest_item.direction,
                "instruction": "Regenerate from the latest radar snapshot. Do not reuse stale direction geometry.",
            },
            pre_ai_refresh=pre_ai_refresh,
        )
        ok, reason = strategy_validator.validate(retry_plan)
        if not ok:
            return {
                "proceed": False,
                "result": {
                    "symbol": latest_item.symbol,
                    "candidate_source": candidate_source,
                    "decision": "INVALID_PLAN",
                    "reason": f"regenerated_after_drift:{reason}",
                    "drift_regenerated": True,
                    "previous_plan_side": stale_plan.side,
                    "latest_side": latest_item.direction,
                },
            }
        if retry_plan.action == "WAIT":
            wd = wait_manager.evaluate(latest_item, retry_plan)
            cooldown_until = 0
            lock_released = False
            if ai_review:
                cooldown_until = self._cooldown_ai_wait_candidate(latest_item, retry_plan, wd)
            if ai_review and (cooldown_until or wd.get("decision") in {"EXPIRED", "WAIT_EXPIRED"}):
                lock_released = self._release_ai_candidate_lock(latest_item.symbol)
            return {
                "proceed": False,
                "result": {
                    "symbol": latest_item.symbol,
                    "candidate_source": candidate_source,
                    "decision": wd["decision"],
                    "reason": wd["reason"],
                    "paper_validation_allowed": paper_validation,
                    "drift_regenerated": True,
                    "previous_plan_side": stale_plan.side,
                    "latest_side": latest_item.direction,
                    "candidate_lock_released": lock_released,
                    "candidate_wait_cooldown_until_ms": cooldown_until,
                },
            }

        latest_item, post_ai_refresh = await self._prepare_latest_item_for_ai(latest_item, force_scan=False)
        fresh_ok, fresh_report = self._ai_candidate_freshness_report(latest_item, candidate_source, performance_context)
        if not fresh_ok:
            lock_released = False
            cooldown_until = 0
            if ai_review:
                cooldown_until = self._cooldown_stale_candidate(latest_item)
                lock_released = self._release_ai_candidate_lock(latest_item.symbol)
            return {
                "proceed": False,
                "result": {
                    "symbol": latest_item.symbol,
                    "candidate_source": candidate_source,
                    "decision": "STALE_AFTER_AI",
                    "reason": ",".join(fresh_report["reasons"][:4]),
                    "freshness": fresh_report,
                    "post_ai_market_refresh": post_ai_refresh,
                    "drift_regenerated": True,
                    "previous_plan_side": stale_plan.side,
                    "candidate_lock_released": lock_released,
                    "candidate_wait_cooldown_until_ms": cooldown_until,
                },
            }
        if retry_plan.side in {"LONG", "SHORT"} and latest_item.direction in {"LONG", "SHORT"} and retry_plan.side != latest_item.direction:
            lock_released = False
            cooldown_until = 0
            if ai_review:
                cooldown_until = self._cooldown_stale_candidate(latest_item)
                lock_released = self._release_ai_candidate_lock(latest_item.symbol)
            return {
                "proceed": False,
                "result": {
                    "symbol": latest_item.symbol,
                    "candidate_source": candidate_source,
                    "decision": "STALE_AFTER_AI",
                    "reason": "plan_side_mismatch_after_ai_retry",
                    "plan_side": retry_plan.side,
                    "latest_side": latest_item.direction,
                    "drift_regenerated": True,
                    "previous_plan_side": stale_plan.side,
                    "candidate_lock_released": lock_released,
                    "candidate_wait_cooldown_until_ms": cooldown_until,
                },
            }
        retry_plan.raw = {
            **retry_plan.raw,
            "drift_regeneration": {
                "reason": "plan_side_mismatch_after_ai",
                "previous_plan_side": stale_plan.side,
                "latest_side": latest_item.direction,
            },
        }
        return {
            "proceed": True,
            "item": latest_item,
            "plan": retry_plan,
            "paper_validation": paper_validation,
        }

    def _candidate_batch(self, performance_context: dict | None = None):
        limit = max(1, int(settings.auto_trading_candidate_limit or 5))
        performance_context = performance_context or {}
        mode = str(settings.auto_trading_candidate_mode).lower()
        if mode == "paper_top":
            pool = self._paper_top_candidates(performance_context, 5)
            stable = self._stable_paper_top_candidates(pool)
            if stable:
                return stable, "paper_top"
            probe_candidates, probe_source = self._paper_probe_batch(
                performance_context,
                "paper_probe_paper_top_empty",
            )
            if probe_candidates:
                return probe_candidates, probe_source
            return [], "paper_top"

        strict = radar_engine.select_ai_candidates(radar_engine.top50)
        if strict:
            return strict[:limit], "strict"
        paper_closed_loop = not (settings.trade_mode == "live" and settings.live_trading_enabled)
        if paper_closed_loop:
            review = radar_engine.select_ai_review_candidates(radar_engine.top50)
            if review:
                return review[:limit], "strict_review"
        recovery_mode = bool(performance_context.get("recovery_mode"))
        learned_reverse = self._learned_reverse_candidates(recovery_mode, limit)
        if learned_reverse:
            return learned_reverse, "learned_reverse"
        return [], "strict_empty"

    async def _refresh_item_price(self, item):
        refreshed, _ = await self._refresh_item_price_with_report(item)
        return refreshed

    async def _refresh_item_price_with_report(self, item):
        report = {
            "ok": False,
            "symbol": getattr(item, "symbol", ""),
            "price": 0.0,
            "source": "",
            "age_seconds": 999999.0,
            "stale": True,
            "safe_for_execution": False,
            "error": "",
            "bid": 0.0,
            "ask": 0.0,
        }
        try:
            quote = await market_service.price_quote(item.symbol, getattr(item, "direction", None))
        except Exception as exc:
            report["error"] = f"price_quote:{type(exc).__name__}"
            return item, report
        safe = self._quote_safe_for_execution(quote)
        report.update(
            {
                "ok": bool(quote.price > 0 and not quote.stale and safe),
                "symbol": quote.symbol,
                "price": quote.price,
                "source": quote.source,
                "age_seconds": quote.age_seconds,
                "stale": quote.stale,
                "safe_for_execution": safe,
                "error": quote.error,
                "bid": quote.bid,
                "ask": quote.ask,
            }
        )
        if report["ok"]:
            return replace(item, price=quote.price), report
        return item, report

    def _pre_trade_price_ok(self, refresh_report: dict[str, Any] | None) -> tuple[bool, str]:
        report = refresh_report or {}
        if bool(report.get("market_refresh_degraded")):
            return False, "market_refresh_degraded"
        price = report.get("trade_price") or {}
        if not bool(price.get("ok")):
            reasons = []
            if price.get("stale"):
                reasons.append("trade_price_stale")
            if not price.get("safe_for_execution"):
                reasons.append("trade_price_source_unsafe")
            if price.get("error"):
                reasons.append(str(price.get("error")))
            return False, ",".join(reasons[:4]) or "trade_price_unavailable"
        return True, ""

    def _quote_safe_for_execution(self, quote) -> bool:
        source = str(getattr(quote, "source", "") or "")
        safe_prefixes = (
            "book_ticker_",
            "ticker_price",
            "premium_mark_price",
            "ticker_24hr_last_price",
        )
        return source.startswith(safe_prefixes)

    def _market_data_ok(self) -> tuple[bool, str]:
        if settings.market_data_mode.lower() != "binance":
            return True, ""
        if binance_factor_source.last_refresh_degraded:
            return False, "market_refresh_degraded"
        if binance_factor_source.last_refresh_source in {"", "none"} and self._effective_market_snapshot_count() == 0:
            return False, "market_refresh_missing"
        return True, ""

    def _real_live_execution_guard(self) -> tuple[bool, str, dict[str, Any]]:
        if not (settings.trade_mode == "live" and settings.live_trading_enabled and not settings.live_use_test_order):
            return True, "ok", {}
        from backend.trading.live_readiness import live_readiness

        freeze = db.get_kv("live_executor.trading_freeze", {}) or {}
        if isinstance(freeze, dict) and freeze.get("active"):
            reason = str(freeze.get("reason") or "unknown")
            return False, f"live_trading_freeze:{reason}", {"trading_freeze": freeze}

        readiness = live_readiness.summary()
        phases = readiness.get("phases") or []
        phase = next((row for row in phases if row.get("name") == "micro_live"), None)
        if phase is None:
            phase = next((row for row in phases if row.get("name") == "scale_live"), None)
        if not phase:
            return False, "live_readiness_phase_missing", readiness
        blockers = [
            blocker
            for blocker in phase.get("blockers") or []
            if blocker.get("code") != "live_trading_already_enabled"
        ]
        if blockers:
            codes = [str(blocker.get("code")) for blocker in blockers if blocker.get("code")]
            return False, "live_readiness_blocked:" + ",".join(codes), readiness
        prg_metrics = readiness_engine.metrics_from_readiness(readiness)
        prg_report = readiness_engine.enforce(prg_metrics)
        if not prg_report.get("allowed"):
            return False, f"prg_blocked:{prg_report.get('reason')}", {"live_readiness": readiness, "prg": prg_report}
        acceptance = db.get_kv("production_acceptance.last_report", {}) or {}
        acceptance_evidence = {"live_readiness": readiness, "production_acceptance": acceptance}
        if not isinstance(acceptance, dict) or not acceptance.get("ok") or not (acceptance.get("production_acceptance") or {}).get("passed"):
            return False, "production_acceptance_not_passed", acceptance_evidence
        if acceptance.get("mode") != "real_order":
            return False, "production_acceptance_mode_not_real_order", acceptance_evidence
        finished_ms = int(acceptance.get("finished_ms") or 0)
        max_age_ms = max(1, int(settings.production_acceptance_max_age_seconds or 3600)) * 1000
        if finished_ms <= 0 or now_ms() - finished_ms > max_age_ms:
            return False, "production_acceptance_stale", acceptance_evidence
        return True, "ok", readiness

    def _market_data_health(self) -> dict[str, Any]:
        return {
            "market_refresh_degraded": bool(binance_factor_source.last_refresh_degraded),
            "market_refresh_error": binance_factor_source.last_refresh_error,
            "market_refresh_source": binance_factor_source.last_refresh_source,
            "market_snapshot_count": binance_factor_source.last_snapshot_count,
            "effective_market_snapshot_count": self._effective_market_snapshot_count(),
            "market_service_snapshot_count": len(market_service.last_snapshots),
            "radar_top50_count": len(radar_engine.top50),
            "market_symbol_count": binance_factor_source.last_symbol_count,
            "market_failed_symbols": list(binance_factor_source.last_failed_symbols or [])[:8],
        }

    def _effective_market_snapshot_count(self) -> int:
        return max(
            int(binance_factor_source.last_snapshot_count or 0),
            len(market_service.last_snapshots),
            len(radar_engine.top50),
        )

    def _latest_candidate_snapshot(self, item):
        current = next((row for row in radar_engine.top50 if row.symbol == item.symbol), None)
        if current is None:
            return item
        if position_registry.has_symbol(current.symbol):
            return item
        return current

    def _ai_candidate_freshness_report(self, item, candidate_source: str, performance_context: dict | None = None) -> tuple[bool, dict[str, Any]]:
        reasons: list[str] = []
        source = str(candidate_source or "")
        current = next((row for row in radar_engine.top50 if row.symbol == item.symbol), None)
        present = current is not None
        if not present:
            reasons.append("candidate_left_radar_top50")
        if item.direction not in {"LONG", "SHORT"}:
            reasons.append("direction_invalid")
        if item.fake_breakout_risk == "HIGH":
            reasons.append("fake_breakout_high")
        rank = int(getattr(item, "rank", 0) or 0)
        if rank > 25 and not self._is_ai_review_source(source):
            reasons.append("rank_decayed_beyond_ai_window")
        age_seconds = round(max(0, now_ms() - int(getattr(item, "ts_ms", 0) or 0)) / 1000, 3)
        max_age = max(
            int(settings.ai_candidate_max_stale_seconds or 300),
            int(settings.binance_factor_ttl_seconds or 30) * 4,
        )
        if age_seconds > max_age:
            reasons.append("candidate_snapshot_stale")

        confirmations = radar_engine._direction_confirmations(item)
        if self._is_paper_probe_source(source):
            if int(getattr(item, "fund_confirm_count", 0) or 0) < max(0, int(settings.paper_probe_min_fund_confirm)):
                reasons.append("paper_probe_fund_confirm_low")
            if confirmations < max(1, int(settings.paper_probe_min_direction_confirmations)):
                reasons.append("paper_probe_direction_confirmations_low")
            if not self._paper_noise_budget_ok(item):
                reasons.append("paper_probe_noise_budget_exceeded")
        elif self._is_ai_review_source(source):
            if not self._full_fund_confirm(item) and not self._paper_validation_allowed(item, performance_context):
                reasons.append("ai_review_not_clean_enough_for_validation")
        else:
            if not self._full_fund_confirm(item):
                reasons.append("fund_confirm_3_required")

        quality_feedback = ai_strategy_feedback.evaluate_candidate(item)
        return not reasons, {
            "ok": not reasons,
            "reasons": reasons,
            "symbol": item.symbol,
            "side": item.direction,
            "rank": rank,
            "score": round(float(getattr(item, "score", 0.0) or 0.0), 4),
            "fund_confirm": f"{item.fund_confirm_count}/{item.fund_confirm_total}",
            "direction_confirmations": confirmations,
            "fake_breakout_risk": item.fake_breakout_risk,
            "age_seconds": age_seconds,
            "candidate_source": source,
            "quality_feedback": {
                "quality_bias": quality_feedback.get("quality_bias"),
                "avoid_repeating": quality_feedback.get("avoid_repeating", [])[:3],
                "review_lessons": quality_feedback.get("review_lessons", [])[:3],
            },
        }

    def _paper_top_candidates(self, performance_context: dict | None, limit: int) -> list:
        directional = [item for item in self._paper_balanced_scope() if item.direction in {"LONG", "SHORT"}]
        top5_symbols = {item.symbol for item in self._paper_top_scope()}
        eligible = [
            item
            for item in directional
            if self._paper_noise_budget_ok(item)
            and (self._full_fund_confirm(item) or self._paper_validation_allowed(item, performance_context))
        ]
        directional = eligible
        ranked = sorted(
            directional,
            key=lambda item: (
                self._paper_openability_score(item),
                1 if item.symbol in top5_symbols else 0,
                1 if self._full_fund_confirm(item) else 0,
                radar_engine._direction_confirmations(item),
                *candidate_feature_enhancer.rank_key(item),
            ),
            reverse=True,
        )
        return ranked[: max(1, int(limit or 1))]

    async def _geometry_supported_candidate_order(
        self,
        candidates: list,
        candidate_source: str,
        performance_context: dict | None = None,
    ) -> tuple[list, list[dict[str, Any]]]:
        source = str(candidate_source or "")
        if not candidates or not self._uses_strategy_geometry_candidate_order(source):
            return candidates, []

        pool = self._geometry_candidate_pool(candidates, source, performance_context)
        if not pool:
            return candidates, []

        reports: list[dict[str, Any]] = []
        sample_by_symbol: dict[str, dict[str, Any]] = {}
        for item in pool:
            try:
                sample = await strategy_geometry_sampler.evaluate(item)
            except Exception as exc:
                sample = {
                    "enabled": True,
                    "status": "unavailable",
                    "reason": f"geometry_sample_error:{type(exc).__name__}",
                    "symbol": getattr(item, "symbol", ""),
                    "side": getattr(item, "direction", ""),
                    "samples": {},
                }
            symbol = str(getattr(item, "symbol", "") or "")
            sample_by_symbol[symbol] = sample
            reports.append(self._geometry_candidate_report(item, sample))

        self._candidate_geometry_samples.update(sample_by_symbol)
        if not any(row.get("geometry_status") == "ok" for row in reports):
            return candidates, reports

        ranked = sorted(
            pool,
            key=lambda item: self._geometry_candidate_rank_key(item, sample_by_symbol.get(getattr(item, "symbol", "")) or {}),
            reverse=True,
        )
        ordered = [item for item in ranked if getattr(item, "symbol", "")]
        if source == "paper_top" and ordered:
            original_symbol = getattr(candidates[0], "symbol", "") if candidates else ""
            selected_symbol = getattr(ordered[0], "symbol", "")
            if selected_symbol and selected_symbol != original_symbol:
                now = now_ms()
                self.ai_candidate_lock = self._paper_top_lock_snapshot(
                    ordered[0],
                    now,
                    now,
                    "geometry_supported_candidate",
                )
        return ordered[: max(1, len(candidates))], reports

    def _uses_strategy_geometry_candidate_order(self, candidate_source: str) -> bool:
        source = str(candidate_source or "")
        return source in {"paper_top", "strict", "strict_review"} or self._is_paper_probe_source(source)

    def _geometry_candidate_pool(self, candidates: list, candidate_source: str, performance_context: dict | None = None) -> list:
        source = str(candidate_source or "")
        limit = max(1, int(settings.auto_trading_candidate_limit or len(candidates) or 1))
        max_eval = max(limit, 5 if source == "paper_top" else len(candidates))
        pool: list = []
        if source == "paper_top":
            pool.extend(self._paper_top_candidates(performance_context or {}, max_eval))
        pool.extend(candidates)

        seen: set[str] = set()
        out: list = []
        for item in pool:
            symbol = str(getattr(item, "symbol", "") or "")
            if not symbol or symbol in seen:
                continue
            if getattr(item, "direction", "") not in {"LONG", "SHORT"}:
                continue
            if source == "paper_top" and symbol in self.ai_candidate_wait_cooldowns:
                continue
            if position_registry.has_symbol(symbol):
                continue
            seen.add(symbol)
            out.append(item)
            if len(out) >= max_eval:
                break
        return out

    def _geometry_candidate_report(self, item, sample: dict[str, Any]) -> dict[str, Any]:
        samples = sample.get("samples") if isinstance(sample.get("samples"), dict) else {}
        return {
            "symbol": getattr(item, "symbol", ""),
            "side": getattr(item, "direction", ""),
            "score": round(float(getattr(item, "score", 0.0) or 0.0), 4),
            "geometry_status": str(sample.get("status") or "unavailable"),
            "sample_model": sample.get("sample_model"),
            "sample_count": int(samples.get("sample_count") or 0),
            "win_rate": round(float(samples.get("win_rate") or 0.0), 4),
            "expected_r": round(float(samples.get("expected_r") or 0.0), 4),
            "profit_factor": round(float(samples.get("profit_factor") or 0.0), 4),
            "pass_count": int(sample.get("pass_count") or 0),
        }

    def _geometry_candidate_rank_key(self, item, sample: dict[str, Any]) -> tuple:
        samples = sample.get("samples") if isinstance(sample.get("samples"), dict) else {}
        status_rank = 2 if sample.get("status") == "ok" else 1 if sample.get("status") == "weak" else 0
        return (
            status_rank,
            int(sample.get("pass_count") or 0),
            float(samples.get("expected_r") or 0.0),
            float(samples.get("profit_factor") or 0.0),
            float(samples.get("win_rate") or 0.0),
            int(samples.get("sample_count") or 0),
            self._paper_openability_score(item),
            1 if self._full_fund_confirm(item) else 0,
            radar_engine._direction_confirmations(item),
            *candidate_feature_enhancer.rank_key(item),
        )

    async def _retry_candidates_after_pre_ai_stale(
        self,
        candidate_source: str,
        performance_context: dict[str, Any],
        attempted_symbols: set[str],
    ) -> list:
        source = str(candidate_source or "")
        if source != "paper_top":
            return []
        max_attempts = 3
        if len([symbol for symbol in attempted_symbols if symbol]) >= max_attempts:
            return []
        try:
            candidates, next_source = self._candidate_batch(performance_context)
        except Exception:
            return []
        if next_source != source or not candidates:
            return []
        ordered, _reports = await self._geometry_supported_candidate_order(
            candidates,
            next_source,
            performance_context,
        )
        out = []
        for item in ordered:
            symbol = str(getattr(item, "symbol", "") or "")
            if not symbol or symbol in attempted_symbols:
                continue
            out.append(item)
            if len(out) >= max_attempts - len(attempted_symbols):
                break
        return out

    async def _append_paper_top_retry_candidates(
        self,
        candidates: list,
        candidate_source: str,
        performance_context: dict[str, Any],
        attempted_symbols: set[str],
        rejected_item,
        *,
        cooldown_current: bool = True,
    ) -> tuple[list[str], int, bool]:
        if str(candidate_source or "") != "paper_top":
            return [], 0, False
        if len([symbol for symbol in attempted_symbols if symbol]) >= 3:
            return [], 0, False

        cooldown_until = 0
        if cooldown_current:
            cooldown_until = self._cooldown_paper_top_attempt(rejected_item)
        lock_released = self._release_ai_candidate_lock(str(getattr(rejected_item, "symbol", "") or ""))
        retry_candidates = await self._retry_candidates_after_pre_ai_stale(
            candidate_source,
            performance_context,
            attempted_symbols,
        )
        if not retry_candidates:
            return [], cooldown_until, lock_released

        existing_symbols = {str(getattr(row, "symbol", "") or "") for row in candidates}
        retry_symbols = []
        for retry_item in retry_candidates:
            retry_symbol = str(getattr(retry_item, "symbol", "") or "")
            if retry_symbol and retry_symbol not in existing_symbols:
                candidates.append(retry_item)
                existing_symbols.add(retry_symbol)
                retry_symbols.append(retry_symbol)
        return retry_symbols, cooldown_until, lock_released

    def _cooldown_paper_top_attempt(self, item) -> int:
        symbol = str(getattr(item, "symbol", "") or "")
        if not symbol:
            return 0
        cooldown_seconds = max(60, int(settings.ai_candidate_lock_seconds or 180))
        until = now_ms() + cooldown_seconds * 1000
        self.ai_candidate_wait_cooldowns[symbol] = until
        return until

    def candidate_lock_status(self) -> dict[str, Any]:
        if not self.ai_candidate_lock:
            return {}
        out = dict(self.ai_candidate_lock)
        locked_at = int(out.get("locked_at_ms") or 0)
        updated_at = int(out.get("updated_at_ms") or 0)
        now = now_ms()
        out["age_seconds"] = round(max(0, now - locked_at) / 1000, 3) if locked_at else 0.0
        out["last_seen_seconds"] = round(max(0, now - updated_at) / 1000, 3) if updated_at else 0.0
        return out

    def _stable_paper_top_candidates(self, pool: list, mutate: bool = True) -> list:
        self._purge_ai_wait_cooldowns()
        tradable_pool = [
            item
            for item in pool
            if not position_registry.has_symbol(item.symbol)
            and item.symbol not in self.ai_candidate_wait_cooldowns
        ]
        if not tradable_pool:
            if mutate:
                self.ai_candidate_lock = {}
            return []

        best = tradable_pool[0]
        locked = dict(self.ai_candidate_lock or {})
        locked_symbol = str(locked.get("symbol") or "")
        now = now_ms()
        current = next((item for item in tradable_pool if item.symbol == locked_symbol), None)
        current_in_pool = current is not None
        if current is None:
            current = self._locked_candidate_from_scan(locked_symbol)
        invalid_reason = self._paper_top_lock_invalid_reason(locked, current, now)

        if not locked or invalid_reason:
            selected = best
            reason = invalid_reason or "new_candidate"
            locked_at = now
        else:
            selected = current
            reason = "locked_candidate" if current_in_pool else "locked_candidate_retained_outside_current_top5"
            locked_at = int(locked.get("locked_at_ms") or now)
            if best.symbol != current.symbol and self._paper_top_should_replace(current, best, locked_at, now):
                selected = best
                reason = "stronger_candidate_replaced_lock"
                locked_at = now

        if mutate:
            self.ai_candidate_lock = self._paper_top_lock_snapshot(selected, locked_at, now, reason)
        return [selected]

    def _paper_top_lock_invalid_reason(self, locked: dict[str, Any], current, now: int) -> str:
        if not locked or not locked.get("symbol"):
            return ""
        if current is None:
            return "locked_candidate_left_radar_top50"
        max_stale_ms = max(1, int(settings.ai_candidate_max_stale_seconds or 300)) * 1000
        if now - int(locked.get("locked_at_ms") or now) > max_stale_ms:
            return "locked_candidate_stale"
        if current.direction not in {"LONG", "SHORT"}:
            return "locked_candidate_direction_invalid"
        if current.fake_breakout_risk == "HIGH":
            return "locked_candidate_fake_breakout_high"
        if current.symbol in self.ai_candidate_wait_cooldowns:
            return "locked_candidate_ai_wait_cooldown"
        if int(getattr(current, "fund_confirm_count", 0) or 0) < 1:
            return "locked_candidate_fund_confirm_lost"
        if radar_engine._direction_confirmations(current) < 3:
            return "locked_candidate_confirmation_lost"
        if not self._paper_noise_budget_ok(current):
            return "locked_candidate_noise_budget_lost"
        return ""

    def _locked_candidate_from_scan(self, symbol: str):
        if not symbol:
            return None
        item = next((row for row in radar_engine.top50 if row.symbol == symbol), None)
        if item is None or position_registry.has_symbol(item.symbol):
            return None
        return item

    def _paper_top_should_replace(self, current, best, locked_at: int, now: int) -> bool:
        lock_ms = max(1, int(settings.ai_candidate_lock_seconds or 180)) * 1000
        if now - locked_at < lock_ms:
            return False
        margin = float(settings.ai_candidate_replace_score_margin or 0.0)
        current_score = float(getattr(current, "score", 0.0) or 0.0)
        best_score = float(getattr(best, "score", 0.0) or 0.0)
        if best_score >= current_score + margin:
            return True
        best_full = self._full_fund_confirm(best)
        current_full = self._full_fund_confirm(current)
        if best_full and not current_full and best_score >= current_score - max(1.0, margin * 0.5):
            return True
        return False

    def _paper_top_lock_snapshot(self, item, locked_at: int, now: int, reason: str) -> dict[str, Any]:
        lock_seconds = max(1, int(settings.ai_candidate_lock_seconds or 180))
        enhanced = candidate_feature_enhancer.evaluate(item).asdict()
        return {
            "symbol": item.symbol,
            "side": item.direction,
            "score": round(float(getattr(item, "score", 0.0) or 0.0), 4),
            "enhanced_selection_score": enhanced.get("selection_score"),
            "estimated_win_rate": enhanced.get("estimated_win_rate"),
            "rank": int(getattr(item, "rank", 0) or 0),
            "fund_confirm": f"{item.fund_confirm_count}/{item.fund_confirm_total}",
            "direction_confirmations": radar_engine._direction_confirmations(item),
            "fake_breakout_risk": item.fake_breakout_risk,
            "locked_at_ms": locked_at,
            "updated_at_ms": now,
            "replaceable_after_ms": locked_at + lock_seconds * 1000,
            "reason": reason,
            "lock_seconds": lock_seconds,
            "replace_score_margin": float(settings.ai_candidate_replace_score_margin or 0.0),
            "max_stale_seconds": int(settings.ai_candidate_max_stale_seconds or 300),
            "enhanced": enhanced,
        }

    def _release_ai_candidate_lock(self, symbol: str) -> bool:
        locked_symbol = str((self.ai_candidate_lock or {}).get("symbol") or "")
        if locked_symbol and locked_symbol == symbol:
            self.ai_candidate_lock = {}
            return True
        return False

    def _cooldown_ai_wait_candidate(self, item, plan, wait_decision: dict[str, Any]) -> int:
        if item is None or not getattr(item, "symbol", ""):
            return 0
        decision = str((wait_decision or {}).get("decision") or "")
        if decision not in {"KEEP_WAITING", "PAPER_OBSERVE", "EXPIRED", "WAIT_EXPIRED"}:
            return 0
        wait_type = str(getattr(plan, "wait_type", "") or "").upper()
        reason = str(getattr(plan, "reason", "") or "").upper()
        quality_wait = any(token in wait_type or token in reason for token in ("QUALITY", "AVOID", "CONFIRMATION", "WAIT"))
        if not quality_wait:
            return 0
        cooldown_seconds = max(
            int(settings.ai_candidate_lock_seconds or 180),
            min(int(getattr(plan, "expire_after_seconds", 0) or 0), 1800),
        )
        until = now_ms() + cooldown_seconds * 1000
        self.ai_candidate_wait_cooldowns[item.symbol] = until
        return until

    def _cooldown_stale_candidate(self, item) -> int:
        if item is None or not getattr(item, "symbol", ""):
            return 0
        cooldown_seconds = max(60, int(settings.ai_candidate_lock_seconds or 180))
        until = now_ms() + cooldown_seconds * 1000
        self.ai_candidate_wait_cooldowns[item.symbol] = until
        return until

    def _purge_ai_wait_cooldowns(self) -> None:
        now = now_ms()
        self.ai_candidate_wait_cooldowns = {
            symbol: until
            for symbol, until in self.ai_candidate_wait_cooldowns.items()
            if int(until or 0) > now
        }

    def _paper_probe_batch(self, performance_context: dict | None = None, source: str = "paper_probe", preferred: list | None = None):
        if not self._paper_probe_enabled():
            return [], source
        limit = max(1, int(settings.auto_trading_candidate_limit or 1))
        candidates = self._paper_probe_candidates(preferred or radar_engine.top50, performance_context)
        return candidates[:limit], source

    def _paper_probe_candidates(self, items: list, performance_context: dict | None = None) -> list:
        if not self._paper_probe_enabled():
            return []
        min_fund = max(0, int(settings.paper_probe_min_fund_confirm))
        min_confirms = max(1, int(settings.paper_probe_min_direction_confirmations))
        directional = [
            item
            for item in items
            if item.direction in {"LONG", "SHORT"}
            and item.fake_breakout_risk != "HIGH"
            and self._paper_noise_budget_ok(item)
            and item.fund_confirm_count >= min_fund
            and radar_engine._direction_confirmations(item) >= min_confirms
        ]
        top_score = max([float(item.score or 0.0) for item in directional], default=0.0)
        floor = max(float(settings.paper_probe_min_score_floor), top_score * 0.72 if top_score else 0.0)
        out = [item for item in directional if float(item.score or 0.0) >= floor]
        return sorted(
            out,
            key=lambda item: (
                int(item.fund_confirm_count),
                radar_engine._direction_confirmations(item),
                float(item.score or 0.0),
                1 if item.fake_breakout_risk == "LOW" else 0,
            ),
            reverse=True,
        )

    def _paper_probe_enabled(self) -> bool:
        return bool(settings.paper_probe_enabled and not (settings.trade_mode == "live" and settings.live_trading_enabled))

    def _is_paper_probe_source(self, candidate_source: str) -> bool:
        return str(candidate_source or "").startswith("paper_probe")

    def _is_ai_review_source(self, candidate_source: str) -> bool:
        return str(candidate_source or "") in {"paper_top", "strict_review"}

    def _paper_validation_allowed(self, item, performance_context: dict | None = None) -> bool:
        gate = self._paper_top_gate(performance_context)
        if not gate["paper_closed_loop"]:
            return False
        fund_total = max(1, int(getattr(item, "fund_confirm_total", 3) or 3))
        min_partial_fund = min(2, fund_total)
        return (
            item.direction in {"LONG", "SHORT"}
            and item.fake_breakout_risk != "HIGH"
            and self._paper_noise_budget_ok(item)
            and int(getattr(item, "fund_confirm_count", 0) or 0) >= min_partial_fund
            and radar_engine._direction_confirmations(item) >= int(gate["min_direction_confirmations"])
        )

    def _mark_paper_validation_plan(self, plan, item, source: str = "paper_top"):
        contract = dict(plan.raw.get("strategy_contract") or {})
        if contract:
            execution = dict(contract.get("execution") or {})
            execution["stage"] = "paper_validation"
            execution["live_permission"] = "false"
            execution["sample_purpose"] = "paper-only forward validation for position-management learning"
            contract["execution"] = execution
            allowed_stages = dict(contract.get("allowed_stages") or {})
            allowed_stages["paper_validation"] = True
            allowed_stages["live"] = False
            contract["allowed_stages"] = allowed_stages
        plan.raw = {
            **plan.raw,
            "paper_validation": {
                "source": source,
                "fund_confirm": f"{item.fund_confirm_count}/{item.fund_confirm_total}",
                "direction_confirmations": radar_engine._direction_confirmations(item),
                "purpose": "keep paper positions open long enough to learn MFE, MAE, drawdown, hold time, and exit quality",
            },
        }
        if contract:
            plan.raw["strategy_contract"] = contract
        return plan

    def _paper_top_gate(self, performance_context: dict | None = None) -> dict[str, Any]:
        performance_context = performance_context or {}
        recovery_mode = bool(performance_context.get("recovery_mode"))
        paper_closed_loop = not (settings.trade_mode == "live" and settings.live_trading_enabled)
        configured_min_score = float(settings.auto_trading_candidate_min_score or 0.0)
        if paper_closed_loop:
            base_min_score = max(configured_min_score, 55.0)
        else:
            base_min_score = max(
                configured_min_score,
                float(settings.strategy_recovery_min_score if recovery_mode else 55.0),
            )

        top5 = self._paper_top_scope()
        directional = [
            item
            for item in top5
            if item.direction in {"LONG", "SHORT"} and item.fake_breakout_risk != "HIGH"
        ]
        top_score = max([float(item.score or 0.0) for item in directional], default=0.0)
        adaptive = False
        min_score = base_min_score
        if paper_closed_loop and top_score > 0 and top_score < base_min_score:
            min_score = max(20.0, round(top_score * 0.88, 2))
            adaptive = True

        full_confirm_available = any(self._full_fund_confirm(item) for item in directional)
        min_fund_confirm = 3

        return {
            "candidate_scope": "balanced_top20_from_top50",
            "paper_closed_loop": paper_closed_loop,
            "recovery_mode": recovery_mode,
            "configured_min_score": configured_min_score,
            "base_min_score": base_min_score,
            "top_directional_score": top_score,
            "min_score": min_score,
            "adaptive_score_floor": adaptive,
            "min_fund_confirm": min_fund_confirm,
            "paper_validation_min_fund_confirm": 2,
            "full_fund_confirm_available": full_confirm_available,
            "min_direction_confirmations": 5 if recovery_mode else 4,
            "max_wick_ratio": float(settings.paper_probe_max_wick_ratio),
            "hard_current_wick_ratio": 0.88,
            "balanced_current_wick_ratio": self._paper_balanced_current_wick_limit(),
            "balanced_min_score": 85.0,
            "balanced_scope_limit": 20,
            "paper_noise_budget_gating": True,
            "candidate_role": "ai_review_first",
            "open_permission": (
                "formal paper open requires ai_open plus at least 3 current-market confirmations and risk_model; "
                "paper validation open requires ai_open plus hard-risk filters, balanced noise budget, partial confirmation, "
                "not HIGH fake risk, direction confirmation, and full non-probe risk_model"
            ),
        }

    def candidate_diagnostics(self, performance_context: dict | None = None) -> dict[str, Any]:
        gate = self._paper_top_gate(performance_context)
        items = list(radar_engine.top50)
        top5 = self._paper_top_scope()
        balanced_scope = self._paper_balanced_scope()
        top5_symbols = {item.symbol for item in top5}
        balanced_symbols = {item.symbol for item in balanced_scope}
        directional = [item for item in balanced_scope if item.direction in {"LONG", "SHORT"}]
        non_high = [item for item in directional if item.fake_breakout_risk != "HIGH"]
        min_score = float(gate["min_score"])
        min_confirms = int(gate["min_direction_confirmations"])
        score_ok = [item for item in non_high if float(item.score or 0.0) >= min_score]
        fund_ok = [item for item in score_ok if self._full_fund_confirm(item)]
        confirms_ok = [item for item in fund_ok if radar_engine._direction_confirmations(item) >= min_confirms]
        wick_ok = [item for item in confirms_ok if self._paper_noise_budget_ok(item)]
        wick_flagged = [item for item in confirms_ok if not self._paper_noise_budget_ok(item)]
        wick_balanced = [
            item
            for item in wick_ok
            if self._paper_noise_budget_report(item).get("mode") == "balanced"
        ]
        enhanced_pool = self._paper_top_candidates(performance_context, 5)
        ai_review_candidates = self._stable_paper_top_candidates(enhanced_pool, mutate=False)
        enhanced_top5 = [
            {
                **candidate_feature_enhancer.evaluate(item).asdict(),
                "paper_openability_score": self._paper_openability_score(item),
            }
            for item in enhanced_pool[:5]
        ]
        validation_candidates = [item for item in ai_review_candidates if self._paper_validation_allowed(item, performance_context)]
        probe_candidates = self._paper_probe_candidates(items, performance_context)
        strict_review_candidates = radar_engine.select_ai_review_candidates(items)
        scores = [float(item.score or 0.0) for item in items]
        rejection_counts: dict[str, int] = {}
        examples = []
        for item in items[:12]:
            reasons = self._candidate_rejection_reasons(item, gate, balanced_symbols)
            for reason in reasons:
                rejection_counts[reason] = rejection_counts.get(reason, 0) + 1
            examples.append(
                {
                    "symbol": item.symbol,
                    "side": item.direction,
                    "score": item.score,
                    "fund_confirm": f"{item.fund_confirm_count}/{item.fund_confirm_total}",
                    "fake_breakout_risk": item.fake_breakout_risk,
                    "direction_confirmations": radar_engine._direction_confirmations(item),
                    "failed": reasons,
                    "warnings": self._candidate_warnings(item),
                }
            )
        return {
            "mode": settings.auto_trading_candidate_mode,
            "gate": gate,
            "candidate_lock": self.candidate_lock_status(),
            "ai_wait_cooldowns": self.ai_candidate_wait_cooldowns,
            "enhanced_top5": enhanced_top5,
            "counts": {
                "top50": len(items),
                "top5_scope": len(top5),
                "balanced_scope": len(balanced_scope),
                "ai_review_candidates": len(ai_review_candidates),
                "direction_long_short": len(directional),
                "fake_not_high": len(non_high),
                "score_ok": len(score_ok),
                "fund_confirm_ok": len(fund_ok),
                "direction_confirmations_ok": len(confirms_ok),
                "paper_top_all_gates": len(wick_ok),
                "paper_validation_candidates": len(validation_candidates),
                "paper_noise_budget_ok": len(wick_ok),
                "paper_noise_budget_flagged": len(wick_flagged),
                "paper_noise_budget_balanced": len(wick_balanced),
                "paper_probe_candidates": len(probe_candidates),
                "strict_candidates": len(radar_engine.select_ai_candidates(items)),
                "strict_review_candidates": len(strict_review_candidates),
            },
            "market_data": {
                "degraded": binance_factor_source.last_refresh_degraded,
                "error": binance_factor_source.last_refresh_error,
                "source": binance_factor_source.last_refresh_source,
                "symbol_count": binance_factor_source.last_symbol_count,
                "snapshot_count": binance_factor_source.last_snapshot_count,
                "failed_symbols": binance_factor_source.last_failed_symbols,
            },
            "production_candidates": radar_engine.production_candidate_diagnostics(items),
            "score_stats": {
                "max": round(max(scores), 4) if scores else 0.0,
                "avg": round(sum(scores) / len(scores), 4) if scores else 0.0,
            },
            "rejection_counts_top12": rejection_counts,
            "examples_top12": examples,
        }

    def candidate_diagnostics_light(self, performance_context: dict | None = None) -> dict[str, Any]:
        performance_context = performance_context or {}
        gate = self._paper_top_gate(performance_context)
        items = list(radar_engine.top50)
        top5 = self._paper_top_scope()
        balanced_scope = self._paper_balanced_scope()
        balanced_symbols = {item.symbol for item in balanced_scope}
        directional = [item for item in balanced_scope if item.direction in {"LONG", "SHORT"}]
        non_high = [item for item in directional if item.fake_breakout_risk != "HIGH"]
        min_score = float(gate["min_score"])
        min_confirms = int(gate["min_direction_confirmations"])
        score_ok = [item for item in non_high if float(item.score or 0.0) >= min_score]
        fund_ok = [item for item in score_ok if self._full_fund_confirm(item)]
        confirms_ok = [item for item in fund_ok if radar_engine._direction_confirmations(item) >= min_confirms]
        wick_ok = [item for item in confirms_ok if self._paper_noise_budget_ok(item)]
        wick_flagged = [item for item in confirms_ok if not self._paper_noise_budget_ok(item)]
        wick_balanced = [
            item
            for item in wick_ok
            if self._paper_noise_budget_report(item).get("mode") == "balanced"
        ]
        validation_candidates = [
            item
            for item in balanced_scope
            if self._paper_validation_allowed(item, performance_context)
        ]
        probe_candidates = self._paper_probe_candidates(items, performance_context)
        strict_candidates = [item for item in items if self._strict_candidate_light_ok(item)]
        strict_review_candidates = [
            item
            for item in balanced_scope
            if item.direction in {"LONG", "SHORT"}
            and item.fake_breakout_risk != "HIGH"
            and self._paper_noise_budget_ok(item)
            and (self._full_fund_confirm(item) or self._paper_validation_allowed(item, performance_context))
        ]
        preview_candidates, preview_source = self._candidate_preview_light(
            performance_context,
            strict_candidates=strict_candidates,
            strict_review_candidates=strict_review_candidates,
            probe_candidates=probe_candidates,
        )
        scores = [float(item.score or 0.0) for item in items]
        rejection_counts: dict[str, int] = {}
        examples = []
        for item in items[:12]:
            reasons = self._candidate_rejection_reasons(item, gate, balanced_symbols)
            for reason in reasons:
                rejection_counts[reason] = rejection_counts.get(reason, 0) + 1
            examples.append(
                {
                    "symbol": item.symbol,
                    "side": item.direction,
                    "score": item.score,
                    "fund_confirm": f"{item.fund_confirm_count}/{item.fund_confirm_total}",
                    "fake_breakout_risk": item.fake_breakout_risk,
                    "direction_confirmations": radar_engine._direction_confirmations(item),
                    "failed": reasons,
                    "warnings": self._candidate_warnings(item),
                }
            )
        return {
            "lightweight": True,
            "mode": settings.auto_trading_candidate_mode,
            "gate": gate,
            "candidate_lock": self.candidate_lock_status(),
            "ai_wait_cooldowns": self.ai_candidate_wait_cooldowns,
            "candidate_source": preview_source,
            "candidate_symbols": [item.symbol for item in preview_candidates],
            "enhanced_top5": [],
            "counts": {
                "top50": len(items),
                "top5_scope": len(top5),
                "balanced_scope": len(balanced_scope),
                "ai_review_candidates": len(strict_review_candidates),
                "direction_long_short": len(directional),
                "fake_not_high": len(non_high),
                "score_ok": len(score_ok),
                "fund_confirm_ok": len(fund_ok),
                "direction_confirmations_ok": len(confirms_ok),
                "paper_top_all_gates": len(wick_ok),
                "paper_validation_candidates": len(validation_candidates),
                "paper_noise_budget_ok": len(wick_ok),
                "paper_noise_budget_flagged": len(wick_flagged),
                "paper_noise_budget_balanced": len(wick_balanced),
                "paper_probe_candidates": len(probe_candidates),
                "strict_candidates": len(strict_candidates),
                "strict_review_candidates": len(strict_review_candidates),
            },
            "market_data": {
                "degraded": binance_factor_source.last_refresh_degraded,
                "error": binance_factor_source.last_refresh_error,
                "source": binance_factor_source.last_refresh_source,
                "symbol_count": binance_factor_source.last_symbol_count,
                "snapshot_count": binance_factor_source.last_snapshot_count,
                "failed_symbols": binance_factor_source.last_failed_symbols,
            },
            "production_candidates": {"lightweight": True, "skipped": "deep_candidate_diagnostics_not_run"},
            "score_stats": {
                "max": round(max(scores), 4) if scores else 0.0,
                "avg": round(sum(scores) / len(scores), 4) if scores else 0.0,
            },
            "rejection_counts_top12": rejection_counts,
            "examples_top12": examples,
        }

    def _candidate_preview_light(
        self,
        performance_context: dict | None = None,
        *,
        strict_candidates: list | None = None,
        strict_review_candidates: list | None = None,
        probe_candidates: list | None = None,
    ) -> tuple[list, str]:
        limit = max(1, int(settings.auto_trading_candidate_limit or 5))
        mode = str(settings.auto_trading_candidate_mode).lower()
        performance_context = performance_context or {}
        strict_candidates = strict_candidates or []
        strict_review_candidates = strict_review_candidates or []
        probe_candidates = probe_candidates or []
        if mode == "paper_top":
            pool = self._paper_top_candidates_light(performance_context, 5)
            if pool:
                return pool[:limit], "paper_top"
            if probe_candidates:
                return probe_candidates[:limit], "paper_probe_paper_top_empty"
            return [], "paper_top"
        if strict_candidates:
            return strict_candidates[:limit], "strict"
        paper_closed_loop = not (settings.trade_mode == "live" and settings.live_trading_enabled)
        if paper_closed_loop and strict_review_candidates:
            return strict_review_candidates[:limit], "strict_review"
        if probe_candidates:
            return probe_candidates[:limit], "paper_probe_strict_empty"
        return [], "strict_empty"

    def _paper_top_candidates_light(self, performance_context: dict | None, limit: int) -> list:
        top5_symbols = {item.symbol for item in self._paper_top_scope()}
        eligible = [
            item
            for item in self._paper_balanced_scope()
            if item.direction in {"LONG", "SHORT"}
            and self._paper_noise_budget_ok(item)
            and (self._full_fund_confirm(item) or self._paper_validation_allowed(item, performance_context))
        ]
        ranked = sorted(
            eligible,
            key=lambda item: (
                self._paper_openability_score_light(item),
                1 if item.symbol in top5_symbols else 0,
                1 if self._full_fund_confirm(item) else 0,
                radar_engine._direction_confirmations(item),
                float(getattr(item, "score", 0.0) or 0.0),
                -int(getattr(item, "rank", 999) or 999),
            ),
            reverse=True,
        )
        return ranked[: max(1, int(limit or 1))]

    def _paper_openability_score_light(self, item) -> float:
        confirmations = radar_engine._direction_confirmations(item)
        score = float(getattr(item, "score", 0.0) or 0.0) * 0.45
        score += min(18.0, confirmations * 3.0)
        score += min(15.0, int(getattr(item, "fund_confirm_count", 0) or 0) * 5.0)
        score += 8.0 if getattr(item, "fake_breakout_risk", "") == "LOW" else 0.0
        score += 6.0 if self._paper_noise_budget_ok(item) else -12.0
        score += min(8.0, max(0.0, float(getattr(item, "volume_spike", 0.0) or 0.0) - 1.0) * 3.0)
        return round(score, 4)

    def _strict_candidate_light_ok(self, item) -> bool:
        if radar_engine._is_major_symbol(item.symbol):
            return False
        if item.direction not in {"LONG", "SHORT"}:
            return False
        if item.fake_breakout_risk != "LOW":
            return False
        if not self._full_fund_confirm(item):
            return False
        if radar_engine._direction_confirmations(item) < 4:
            return False
        if not self._paper_noise_budget_ok(item):
            return False
        return float(getattr(item, "score", 0.0) or 0.0) >= max(55.0, float(settings.auto_trading_candidate_min_score or 0.0))

    def _candidate_rejection_reasons(self, item, gate: dict[str, Any], scope_symbols: set[str] | None = None) -> list[str]:
        reasons: list[str] = []
        if scope_symbols is not None and item.symbol not in scope_symbols:
            return ["outside_balanced_top20_scope"]
        if item.direction not in {"LONG", "SHORT"}:
            reasons.append("direction_neutral")
        if item.fake_breakout_risk == "HIGH":
            reasons.append("fake_breakout_high")
        if float(item.score or 0.0) < float(gate["min_score"]):
            reasons.append("score_below_effective_min")
        if not self._full_fund_confirm(item):
            reasons.append("fund_confirm_below_3")
        if radar_engine._direction_confirmations(item) < int(gate["min_direction_confirmations"]):
            reasons.append("direction_confirmations_low")
        if not self._paper_noise_budget_ok(item):
            reasons.append("paper_noise_budget_exceeded")
        return reasons

    def _candidate_warnings(self, item) -> list[str]:
        report = self._paper_noise_budget_report(item)
        return list(report.get("warnings") or [])

    def _paper_top_scope(self) -> list:
        return list(radar_engine.select_confirmed_top5(radar_engine.top50))

    def _paper_balanced_scope(self) -> list:
        candidates = [
            item
            for item in list(radar_engine.top50)[:20]
            if not radar_engine._is_major_symbol(item.symbol)
            and item.direction in {"LONG", "SHORT"}
            and item.fake_breakout_risk != "HIGH"
            and int(getattr(item, "fund_confirm_count", 0) or 0) >= 1
            and radar_engine._direction_confirmations(item) >= 3
            and radar_engine._current_wick_ratio(item) <= 0.90
        ]
        return sorted(candidates, key=radar_engine._trade_top5_rank, reverse=True)

    def _full_fund_confirm(self, item) -> bool:
        return int(getattr(item, "fund_confirm_count", 0) or 0) >= min(3, int(getattr(item, "fund_confirm_total", 3) or 3))

    def _paper_noise_budget_ok(self, item) -> bool:
        return bool(self._paper_noise_budget_report(item).get("ok"))

    def _paper_noise_budget_report(self, item) -> dict[str, Any]:
        max_wick = max(0.0, self._float_value(settings.paper_probe_max_wick_ratio, 0.0))
        current_wick = self._float_value(radar_engine._current_wick_ratio(item), 0.0)
        metrics = self._candidate_structure_metrics(item)
        recent_max_wick = self._float_value(metrics.get("max_wick_ratio_14"), self._float_value(getattr(item, "wick_ratio", 0.0), 0.0))
        avg_wick = self._float_value(metrics.get("avg_wick_ratio_14"), recent_max_wick)
        bars_since_max = self._int_value(metrics.get("bars_since_max_wick"), 0)
        hard_current = 0.88
        balanced_current = self._paper_balanced_current_wick_limit()
        avg_limit = max(0.65, max_wick + 0.10)
        warnings: list[str] = []

        base = {
            "current_wick_ratio": round(current_wick, 6),
            "max_wick_ratio": round(recent_max_wick, 6),
            "avg_wick_ratio": round(avg_wick, 6),
            "bars_since_max_wick": bars_since_max,
            "configured_max_wick_ratio": round(max_wick, 6),
            "hard_current_wick_ratio": hard_current,
            "balanced_current_wick_ratio": balanced_current,
        }
        if max_wick <= 0:
            return {**base, "ok": True, "mode": "disabled", "reasons": [], "warnings": warnings}
        if current_wick >= hard_current:
            return {
                **base,
                "ok": False,
                "mode": "blocked",
                "reasons": ["current_wick_extreme"],
                "warnings": ["wick_above_paper_noise_budget", "current_wick_extreme"],
            }
        if current_wick > balanced_current:
            return {
                **base,
                "ok": False,
                "mode": "blocked",
                "reasons": ["current_wick_above_balance_limit"],
                "warnings": ["wick_above_paper_noise_budget", "current_wick_above_balance_limit"],
            }
        if recent_max_wick <= max_wick:
            return {**base, "ok": True, "mode": "clean", "reasons": [], "warnings": warnings}

        reasons: list[str] = []
        if getattr(item, "fake_breakout_risk", "") != "LOW":
            reasons.append("balanced_noise_requires_low_fake_risk")
        if not self._full_fund_confirm(item):
            reasons.append("balanced_noise_requires_full_fund_confirm")
        if radar_engine._direction_confirmations(item) < 5:
            reasons.append("balanced_noise_requires_direction_confirmations_5")
        if float(getattr(item, "score", 0.0) or 0.0) < 85.0:
            reasons.append("balanced_noise_requires_score_85")
        if recent_max_wick > 0.85 and bars_since_max < 3:
            reasons.append("recent_wick_spike_unresolved")
        if avg_wick > avg_limit:
            reasons.append("average_wick_noise_high")

        if reasons:
            return {
                **base,
                "ok": False,
                "mode": "blocked",
                "reasons": reasons,
                "warnings": ["wick_above_paper_noise_budget", *reasons],
            }
        warnings.append("historical_wick_noise_balanced")
        return {**base, "ok": True, "mode": "balanced", "reasons": [], "warnings": warnings}

    def _paper_balanced_current_wick_limit(self) -> float:
        max_wick = max(0.0, self._float_value(settings.paper_probe_max_wick_ratio, 0.0))
        return round(min(0.75, max(0.65, max_wick + 0.10)), 6)

    def _candidate_structure_metrics(self, item) -> dict[str, Any]:
        features = getattr(item, "score_features", None)
        if not isinstance(features, dict):
            return {}
        metrics = features.get("structure_metrics")
        return metrics if isinstance(metrics, dict) else {}

    def _float_value(self, value, default: float = 0.0) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    def _int_value(self, value, default: int = 0) -> int:
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return default

    def _paper_openability_score(self, item) -> float:
        report = candidate_feature_enhancer.evaluate(item).asdict()
        positives = set(report.get("positive_factors") or [])
        risks = set(report.get("failure_risks") or [])
        score = 0.0
        score += float(report.get("estimated_win_rate") or 0.0) * 40.0
        score += float(report.get("feature_score") or 0.0) * 0.18
        score += float(report.get("selection_score") or 0.0) * 0.08
        score += 10.0 if "fake_breakout_risk_low" in positives else 0.0
        score += 8.0 if "fund_confirm_full" in positives else (5.0 if "fund_confirm_partial" in positives else 0.0)
        score += 9.0 if "flow_positive" in positives else 0.0
        score += 4.0 if "trend_positive" in positives else 0.0
        score += 4.0 if "structure_positive" in positives else 0.0
        score += 3.0 if "liquidity_positive" in positives else 0.0
        score += 6.0 if "estimated_win_rate_above_paper_gate" in positives else -8.0
        if "wick_above_paper_noise_budget" in risks:
            score -= 14.0
        else:
            score += 5.0
        if "flow_negative" in risks:
            score -= 13.0
        if "fake_breakout_risk_medium" in risks:
            score -= 10.0
        if "fake_breakout_risk_high" in risks:
            score -= 40.0
        if "estimated_win_rate_below_paper_gate" in risks:
            score -= 18.0
        score -= min(12.0, len(risks) * 1.25)
        return round(score, 4)

    def strategy_filter_diagnostics(self, active_strategy: dict[str, Any], candidates: list) -> dict[str, Any]:
        filters = active_strategy.get("filters", active_strategy) or {}
        rejection_counts: dict[str, int] = {}
        examples = []
        matched = 0
        for item in candidates:
            reasons = self._strategy_filter_rejection_reasons(filters, item)
            if not reasons:
                matched += 1
            for reason in reasons:
                rejection_counts[reason] = rejection_counts.get(reason, 0) + 1
            if len(examples) < 12:
                examples.append(
                    {
                        "symbol": item.symbol,
                        "side": item.direction,
                        "score": item.score,
                        "fund_confirm": f"{item.fund_confirm_count}/{item.fund_confirm_total}",
                        "fake_breakout_risk": item.fake_breakout_risk,
                        "direction_confirmations": strategy_direction_confirmations(item.asdict(), item.direction),
                        "failed": reasons,
                    }
                )
        return {
            "strategy_id": active_strategy.get("strategy_id"),
            "name": active_strategy.get("name"),
            "candidate_count": len(candidates),
            "matched_count": matched,
            "filters": filters,
            "rejection_counts": rejection_counts,
            "examples": examples,
        }

    def strategy_selection_diagnostics(self, active_strategy: dict[str, Any], candidates: list) -> dict[str, Any]:
        strategies = self._candidate_strategies(active_strategy)
        per_strategy = [self.strategy_filter_diagnostics(strategy, candidates) for strategy in strategies[:8]]
        return {
            "active_strategy_id": active_strategy.get("strategy_id") if active_strategy else "",
            "usable_strategy_count": len(strategies),
            "eligible_strategy_ids": [strategy.get("strategy_id") for strategy in strategies],
            "per_strategy": per_strategy,
        }

    def _best_matching_strategy(self, item, active_strategy: dict[str, Any] | None) -> dict[str, Any] | None:
        for strategy in self._candidate_strategies(active_strategy):
            if strategy_matches(strategy, item):
                return strategy
        return None

    def _candidate_strategies(self, active_strategy: dict[str, Any] | None) -> list[dict[str, Any]]:
        out = []
        seen = set()
        if active_strategy:
            out.append(active_strategy)
            seen.add(active_strategy.get("strategy_id"))
        for strategy in strategy_registry.list(limit=50):
            strategy_id = strategy.get("strategy_id")
            if strategy_id in seen:
                continue
            metrics = strategy.get("metrics") or {}
            if strategy.get("status") == "ACTIVE" or metrics.get("eligible"):
                out.append(strategy)
                seen.add(strategy_id)
        return sorted(out, key=self._strategy_rank_key, reverse=True)

    def _strategy_rank_key(self, strategy: dict[str, Any]) -> tuple:
        metrics = strategy.get("metrics") or {}
        holdout = metrics.get("holdout") or {}
        return (
            1 if metrics.get("eligible") else 0,
            1 if strategy.get("status") == "ACTIVE" else 0,
            float(holdout.get("pnl", 0.0) or 0.0),
            float(holdout.get("win_rate", 0.0) or 0.0),
            float(metrics.get("pnl", 0.0) or 0.0),
            float(metrics.get("profit_factor", 0.0) or 0.0),
        )

    def _strategy_filter_rejection_reasons(self, filters: dict[str, Any], item) -> list[str]:
        row = item.asdict()
        side = row.get("side") or row.get("direction")
        reasons: list[str] = []
        if side not in {"LONG", "SHORT"}:
            reasons.append("side_invalid")
            return reasons
        if side not in set(filters.get("allowed_sides") or ["LONG", "SHORT"]):
            reasons.append("side_not_allowed")
        if row.get("symbol") in set(filters.get("blocked_symbols") or []):
            reasons.append("symbol_blocked")
        if _f(row.get("score")) < _f(filters.get("min_score"), 0):
            reasons.append("score_below_strategy_min")
        if _f(row.get("fund_confirm_count")) < _f(filters.get("min_fund_confirm"), 0):
            reasons.append("fund_confirm_below_strategy_min")
        if row.get("fake_breakout_risk") not in set(filters.get("allowed_fake_risks") or ["LOW"]):
            reasons.append("fake_risk_not_allowed")
        if strategy_direction_confirmations(row, side) < int(filters.get("min_direction_confirmations", 0)):
            reasons.append("direction_confirmations_below_strategy_min")
        if _f(row.get("volume_spike")) < _f(filters.get("min_volume_spike"), 0):
            reasons.append("volume_below_strategy_min")
        if _f(row.get("wick_ratio")) > _f(filters.get("max_wick_ratio"), 999):
            reasons.append("wick_above_strategy_max")
        if filters.get("require_oi_positive") and _f(row.get("oi_change")) < 0:
            reasons.append("oi_not_positive")
        if filters.get("require_timeframe_alignment") and not self._timeframes_aligned(row, side):
            reasons.append("timeframe_not_aligned")
        if filters.get("require_taker_alignment") and not self._taker_aligned(row, side):
            reasons.append("taker_not_aligned")
        if filters.get("require_depth_alignment") and not self._depth_aligned(row, side):
            reasons.append("depth_not_aligned")
        if filters.get("require_sm_delta_alignment") and not self._sm_aligned(row, side):
            reasons.append("sm_delta_not_aligned")
        return reasons

    def _timeframes_aligned(self, row: dict[str, Any], side: str) -> bool:
        if side == "LONG":
            return _f(row.get("change_5m")) > 0 and _f(row.get("change_15m")) > 0 and _f(row.get("change_1h")) >= 0
        return _f(row.get("change_5m")) < 0 and _f(row.get("change_15m")) < 0 and _f(row.get("change_1h")) <= 0

    def _taker_aligned(self, row: dict[str, Any], side: str) -> bool:
        return _f(row.get("taker_buy_ratio"), 0.5) >= 0.58 if side == "LONG" else _f(row.get("taker_sell_ratio"), 0.5) >= 0.58

    def _depth_aligned(self, row: dict[str, Any], side: str) -> bool:
        return _f(row.get("depth_imbalance")) >= 0.12 if side == "LONG" else _f(row.get("depth_imbalance")) <= -0.12

    def _sm_aligned(self, row: dict[str, Any], side: str) -> bool:
        return _f(row.get("sm_delta")) >= 0 if side == "LONG" else _f(row.get("sm_delta")) <= 0

    def _learned_reverse_candidates(self, recovery_mode: bool, limit: int):
        out = []
        min_score = max(
            float(settings.auto_trading_candidate_min_score or 0.0),
            float(settings.strategy_recovery_min_score if recovery_mode else 55.0),
        )
        seen = set()
        for item in radar_engine.top50[:25]:
            if item.symbol in seen or item.direction not in {"LONG", "SHORT"}:
                continue
            if float(item.score or 0.0) < min_score:
                continue
            reverse_item, report = learned_risk_guard.maybe_reverse_candidate(item, recovery_mode=recovery_mode)
            if reverse_item is None:
                continue
            reverse_item = replace(reverse_item, ai_candidate=True)
            out.append(reverse_item)
            seen.add(item.symbol)
            if len(out) >= limit:
                break
        return out

    async def _account_context(self, open_positions: int):
        paper_context = not (settings.trade_mode == "live" and settings.live_trading_enabled)
        if paper_context:
            equity = max(1.0, float(settings.paper_account_equity_usdt or 1000.0))
            used_margin = sum(float(p.margin) for p in position_registry.list_open())
            available = max(0.0, equity - used_margin)
            summary = {
                "mode": "paper_closed_loop",
                "configured": True,
                "canTrade": True,
                "walletBalance": round(equity, 4),
                "availableBalance": round(available, 4),
                "marginBalance": round(equity, 4),
                "usedMargin": round(used_margin, 4),
                "live_trading_enabled": settings.live_trading_enabled,
            }
            account = {
                "equity": equity,
                "available_balance": available,
                "trade_mode": "paper",
                "can_trade": True,
                "loss_streak": self._loss_streak(),
                "open_positions": open_positions,
                "max_open_positions": settings.max_open_positions,
                "execution_context": "paper_closed_loop",
            }
            return summary, account

        summary = await account_service.get_account_summary()
        account = {
            "equity": float(summary.get("marginBalance") or summary.get("walletBalance") or 0.0),
            "available_balance": float(summary.get("availableBalance") or 0.0),
            "trade_mode": settings.trade_mode,
            "can_trade": bool(summary.get("canTrade")),
            "loss_streak": self._loss_streak(),
            "open_positions": open_positions,
            "max_open_positions": settings.max_open_positions,
            "execution_context": "live",
        }
        return summary, account

    def _loss_streak(self) -> int:
        streak = 0
        for closed in position_registry.list_closed():
            if float(closed.get("pnl", 0.0)) < 0:
                streak += 1
                continue
            break
        return streak

    def _volatility_regime(self) -> str:
        items = radar_engine.top50[:20]
        if not items:
            return "normal"
        avg_atr = sum(float(getattr(item, "atr_pct", 0.0)) for item in items) / len(items)
        if avg_atr >= 3.5:
            return "extreme"
        if avg_atr >= 2.2:
            return "high"
        if avg_atr <= 0.45:
            return "low"
        return "normal"


def _f(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


autotrader = AutoTrader()
