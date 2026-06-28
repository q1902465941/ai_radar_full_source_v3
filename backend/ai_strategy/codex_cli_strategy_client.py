from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import threading
from typing import Any, Callable, Sequence

from backend.config import settings
from backend.models import RadarItem, StrategyPlan, new_id, now_ms
from backend.ai_strategy.context_compressor import context_compressor
from backend.ai_strategy.strategy_contract import attach_contract, build_rule_contract, contract_quality
from backend.ai_strategy.strategy_validator import strategy_validator
from backend.learning.strategy_geometry_sampler import strategy_geometry_sampler
from backend.radar.candidate_feature_enhancer import candidate_feature_enhancer


CODEX_PROMPT = """You are the AI strategy module for a crypto radar system.
Return exactly one JSON object that matches the provided StrategyPlan schema.
Do not output Markdown, comments, code fences, or text outside the JSON object.

Hard rules:
1. If signal quality is not sufficient, return WAIT.
2. LONG geometry must satisfy stop_loss < ideal_entry_price < tp1 < tp2.
3. SHORT geometry must satisfy tp2 < tp1 < ideal_entry_price < stop_loss.
4. WAIT must include wait_type, expire_after_seconds, and upgrade_condition.
5. OPEN_LONG side must be LONG, OPEN_SHORT side must be SHORT, WAIT side should be NEUTRAL.
6. Generate a strategy plan only. Do not generate an exchange order instruction.
7. Prefer WAIT for live-quality decisions unless the setup has positive expectancy after taker fees and slippage: clear direction alignment, sufficient funding confirmation, TP2 at least the local min_tp2_r, and estimated win rate likely above the local quality gate.
8. If local_quality_gate.paper_closed_loop is true, you may return OPEN_LONG or OPEN_SHORT for a Top5, strict_review, or paper_probe paper-only validation plan only when it is clean enough to become a learning sample. Formal paper trades still prefer at least 3 current-market confirmations, and controlled validation/probe samples may use lower confirmation only when the market hypothesis is clear. The execution layer will keep it paper-only. The validation plan must still have valid geometry, cost coverage, no HIGH fake-breakout risk, no conflicting taker/depth/timeframe evidence, and a passing paper_probe_policy. Treat extreme current wick noise as a WAIT condition; older historical wick spikes may be balanced only when current structure is clean and stronger score, fund, direction, and cost evidence compensate. If local_quality_gate.candidate_selection.source is strict, the item already passed local strict selection; radar rank remains diagnostic and is not an automatic veto by itself.
9. If position_context.performance_guard.recovery_mode is true, live-quality OPEN requires exceptional setups. In paper_closed_loop recovery, OPEN is allowed only as a controlled paper probe with allowed_stages.live=false and live_permission=false.
10. Do not force trades. Low confidence, mixed timeframe movement, weak taker/depth confirmation, extreme funding, negative symbol history, trap labels, or wick noise that invalidates the entry geometry should return WAIT.
11. For OPEN plans, include strategy_contract. The contract must explain hypothesis, signal, risk, execution, position_lifecycle, hold_logic, reduce_logic, add_logic, exit_logic, time_stop, review_metrics, entry_conditions, avoid_conditions, invalidation, position_management, cost_constraints, learning_tags, allowed_stages, and research_review. It should prove the plan is a complete strategy, not just a direction guess.
12. Treat yourself as a strict strategy research process executor, not a market predictor. Do not open a plan if the market hypothesis is unclear.
13. The strategy_contract must separate signal, risk, and execution: signal explains why to enter, risk explains how much can be lost and when the idea is wrong, execution explains how it can be filled after fees, slippage, and liquidity.
14. research_review must include Role A as the strategy researcher and Role B as the risk officer. Role B should actively explain where the strategy can fail.
15. Scanning is not trading. A scan result is evidence for a candidate opportunity, never a direct buy/sell command.
16. The strategy must define a position lifecycle: WAITING, ENTRY_READY, OPENED, PROTECTING, TREND_HOLD, SCALE_IN, SCALE_OUT, DEFENSIVE, EXIT_READY, CLOSED.
17. hold_logic is mandatory. Do not exit the core position for a minor reverse signal unless the trade thesis is invalidated, risk limit is hit, time stop fires, or market structure breaks.
18. review_metrics must include MFE, MAE, and R_multiple so the system can learn whether it exited too early, held too long, or used the wrong stop.
19. Use cyqnt_feature_enhancement as the local evidence layer. If estimated_win_rate is below the local paper gate, feature_score is weak, or negative noise/funding contributions dominate, prefer WAIT. If OPEN, strategy_contract must reference the strongest cyqnt positive features and the main cyqnt failure risks.
20. In production_acceptance strict mode, a candidate has already passed local production selection. If current direction, fund confirmation, fake-breakout risk, cyqnt feature score, historical calibration, and cost geometry are valid, generate a constrained OPEN strategy. If historical attribution, event calibration, market backtest, or cyqnt failure_risks show negative expectancy or insufficient support, return WAIT and explain what evidence is missing. Downstream quality gates, risk_model, and live_readiness still decide whether execution is allowed; downstream quality gates, risk_model, and live_readiness are never a reason to ignore weak evidence in the strategy itself.
21. Read ai_strategy_quality_feedback. avoid_repeating is a hard no-repeat constraint: do not copy that losing symbol/side, strategy_kind, entry geometry, invalidation, or lifecycle pattern. review_lessons are coaching notes, not an automatic veto; for paper-only learning, OPEN is allowed when the current candidate has materially stronger evidence and strategy_contract clearly states what changed versus the losing bucket.
22. In paper_closed_loop with ai_strategy_quality_feedback.summary.trading_lessons.learning_mode=paper_forward_training, your job is to teach the system with small controlled paper validation strategies without polluting the learning set. Do not force trades. Return OPEN only when current cyqnt evidence is above the paper gate, fake risk is not HIGH, wick/noise budget passes, direction evidence is coherent, costs and geometry are valid, negative historical/event attribution is not present, and hard avoid_repeating is empty. Keep allowed_stages.live=false.
23. Generate from the latest refreshed market snapshot only. Use market_freshness, radar, market_changes, current_price, and cyqnt_feature_enhancement as the source of truth. If market_freshness.latest_market_required is true but pre_ai_market_refresh.scan_ok is false, symbol_present_after_scan is false, item_age_seconds is stale, or the refreshed side/price conflicts with your planned side/geometry, return WAIT. Never reuse stale direction geometry from an older scan.
24. Read strategy_geometry_sample before choosing OPEN geometry. When strategy_geometry_sample.status is ok, use selected_geometry as the preferred TP/SL geometry unless current market invalidates it. When status is weak or unavailable, explicitly lower confidence, keep live=false, and do not claim the strategy has production-grade sample support.
25. Use universal_anomaly_model as coin-agnostic microstructure confirmation. If its direction disagrees with side_bias or is NEUTRAL, lower confidence and prefer WAIT unless current cyqnt, structure, fund, and geometry evidence clearly overrides it. Never use universal_anomaly_model alone as order permission.

OPEN strategy_contract shape:
- strategy_contract must be an object, never null, for OPEN_LONG or OPEN_SHORT.
- signal: entry string, evidence string array, not_enough_if string array.
- risk: max_loss string, failure_modes string array, reject_if string array.
- execution: stage string, order_plan string, fill_assumption string, cost_checks string array, live_permission string.
- position_lifecycle: states string array, initial_state_after_fill string, principle string.
- hold_logic: continue_holding_if string array, do_not_exit_for string array, evidence_from_scan string array.
- reduce_logic: reduce_if string array, tp1_close_ratio number, after_reduce string, never_reduce_below_core_without_exit_reason boolean.
- add_logic: add_if string array, max_adds 0, reason string. If scale-in is disabled, add_if may be [] but reason must explicitly say scale-in is disabled until forward data proves it helps.
- exit_logic: core_exit_only_if string array, minor_reverse_signal_action string, final_targets object with tp1, tp2, stop_loss.
- time_stop: seconds number, rule string, requires_no_favorable_development boolean.
- invalidation: hard_stop number/string, signal_failure string array, time_failure_seconds number.
- position_management: entry_zone two-number array, tp1, tp2, tp1_close_ratio, after_tp1, max_adds.
- cost_constraints: round_trip_cost_pct, stop_distance_pct, min_net_profit_usdt, min_profit_cost_ratio, tp2_min_r.
- learning_tags: symbol, side, cyqnt_estimated_win_rate, cyqnt_feature_score, cyqnt_selection_score, main_positive_features string array, main_failure_risks string array.
- allowed_stages: paper_probe, paper_formal, shadow_live, live_test_order, micro_live, live booleans. live must be false unless live readiness and explicit user approval exist.
- research_review: role_a_researcher, role_b_risk_officer, must_report, report_template, decision_bias.
- graduation_rule: min_forward_trades, min_win_rate, min_profit_factor, min_pnl, rule.
- review_metrics must include MFE, MAE, and R_multiple.
- If you cannot provide this complete contract, return WAIT instead of OPEN.

StrategyContext:
{context_json}
"""

CODEX_REPAIR_PROMPT = """You are repairing a StrategyPlan JSON object that failed local validation.
Return exactly one corrected JSON object matching the same StrategyPlan schema.
Do not output Markdown, comments, code fences, or text outside JSON.

Failure:
{failure_reason}

Repair rules:
1. If action is OPEN_LONG or OPEN_SHORT, produce a complete strategy_contract using the exact OPEN strategy_contract shape below.
2. If you cannot produce a complete strategy_contract, change action to WAIT with side NEUTRAL, zero targets/stops, wait_type WAIT_FOR_STRATEGY_CONTRACT, and an upgrade_condition.
3. Do not invent live permission. allowed_stages.live, micro_live, and live_test_order must be false unless explicitly allowed by context.
4. Do not generate exchange order instructions. The strategy is research and execution planning only.
5. Keep geometry valid: LONG stop_loss < ideal_entry_price < tp1 < tp2; SHORT tp2 < tp1 < ideal_entry_price < stop_loss.

Required OPEN strategy_contract shape:
- strategy_contract must be an object, never null.
- signal: entry string, evidence string array, not_enough_if string array.
- risk: max_loss string, failure_modes string array, reject_if string array.
- execution: stage string, order_plan string, fill_assumption string, cost_checks string array, live_permission string.
- position_lifecycle: states string array, initial_state_after_fill string, principle string.
- hold_logic: continue_holding_if string array, do_not_exit_for string array, evidence_from_scan string array.
- reduce_logic: reduce_if string array, tp1_close_ratio number, after_reduce string, never_reduce_below_core_without_exit_reason boolean.
- add_logic: add_if string array, max_adds 0, reason string. If scale-in is disabled, add_if may be [] but reason must explicitly say scale-in is disabled until forward data proves it helps.
- exit_logic: core_exit_only_if string array, minor_reverse_signal_action string, final_targets object with tp1, tp2, stop_loss.
- time_stop: seconds number, rule string, requires_no_favorable_development boolean.
- review_metrics string array including MFE, MAE, R_multiple.
- entry_conditions string array, avoid_conditions string array.
- invalidation: hard_stop number/string, signal_failure string array, time_failure_seconds number.
- position_management: entry_zone two-number array, tp1, tp2, tp1_close_ratio, after_tp1, max_adds.
- cost_constraints: round_trip_cost_pct, stop_distance_pct, min_net_profit_usdt, min_profit_cost_ratio, tp2_min_r.
- learning_tags: symbol, side, cyqnt_estimated_win_rate, cyqnt_feature_score, cyqnt_selection_score, main_positive_features string array, main_failure_risks string array.
- allowed_stages: paper_probe, paper_formal, shadow_live, live_test_order, micro_live, live booleans.
- research_review: role_a_researcher, role_b_risk_officer, must_report, report_template, decision_bias.
- graduation_rule: min_forward_trades, min_win_rate, min_profit_factor, min_pnl, rule.

CompactContext:
{context_json}

DraftPlan:
{draft_json}
"""

Runner = Callable[..., subprocess.CompletedProcess[str]]
REASONING_EFFORTS = {"none", "minimal", "low", "medium", "high", "xhigh"}
SERVICE_TIERS = {"fast", "flex"}
CODEX_CLI_LOCK = threading.Lock()


@dataclass(frozen=True)
class CodexRuntime:
    route: str
    model: str
    reasoning_effort: str
    service_tier: str
    timeout_seconds: float


class CodexCLIStrategyClient:
    def __init__(
        self,
        *,
        runner: Runner | None = None,
        codex_command: str | None = None,
        schema_path: str | Path | None = None,
    ) -> None:
        self.runner = runner or run_command
        self.codex_command = codex_command or settings.codex_command or default_codex_command()
        self.schema_path = Path(schema_path) if schema_path else Path(__file__).with_name("strategy_plan.schema.json")
        self.invocation_count = 0
        self.last_invoked_ms = 0
        self.last_status = "never_invoked"
        self.last_error = ""
        self.last_model = ""
        self.last_route = ""
        self.last_reasoning_effort = ""
        self.last_timeout_seconds = 0.0
        self.last_symbol = ""
        self.last_action = ""
        self.last_repair_attempted = False
        self.last_repair_reason = ""
        self.last_plan: dict[str, Any] = {}
        self.recent_plans: list[dict[str, Any]] = []

    async def generate(self, item: RadarItem, position_context: dict | None = None) -> StrategyPlan:
        self.invocation_count += 1
        self.last_invoked_ms = now_ms()
        self.last_status = "running"
        self.last_error = ""
        runtime = codex_runtime_for_strategy(position_context)
        self.last_model = runtime.model
        self.last_route = runtime.route
        self.last_reasoning_effort = runtime.reasoning_effort
        self.last_timeout_seconds = runtime.timeout_seconds
        self.last_symbol = item.symbol
        self.last_action = ""
        self.last_repair_attempted = False
        self.last_repair_reason = ""
        position_context = dict(position_context or {})
        geometry_sample = position_context.get("strategy_geometry_sample")
        if not isinstance(geometry_sample, dict) or not geometry_sample:
            geometry_sample = await self._strategy_geometry_sample(item)
        self._active_geometry_sample = geometry_sample
        position_context["strategy_geometry_sample"] = geometry_sample
        context = context_compressor.build_strategy_context(item, position_context)
        prompt = CODEX_PROMPT.format(context_json=json.dumps(context, ensure_ascii=False, indent=2))
        try:
            raw = await asyncio.to_thread(self._run_codex, prompt, runtime)
            data = json.loads(raw)
            try:
                plan = self._plan_from_dict(data, item, runtime)
            except ValueError as exc:
                if not str(exc).startswith("codex_open_missing_valid_strategy_contract"):
                    raise
                self.last_repair_attempted = True
                self.last_repair_reason = str(exc)
                repair_raw = await asyncio.to_thread(self._repair_codex_plan, data, item, context, runtime, str(exc))
                plan = self._plan_from_dict(json.loads(repair_raw), item, runtime)
        except json.JSONDecodeError:
            self.last_status = "fallback_wait"
            self.last_error = "codex_invalid_json"
            return self._fallback(item, "codex_invalid_json", runtime)
        except subprocess.TimeoutExpired:
            self.last_status = "fallback_wait"
            self.last_error = "codex_timeout"
            return self._fallback(item, "codex_timeout", runtime)
        except Exception as exc:
            if str(exc) == "codex_cli_busy":
                self.last_status = "fallback_wait"
                self.last_error = "codex_busy"
                return self._fallback(item, "codex_busy", runtime)
            self.last_status = "fallback_wait"
            detail = compact_error_detail(redact_secret(str(exc)))
            reason = f"codex_error:{type(exc).__name__}:{detail}" if detail else f"codex_error:{type(exc).__name__}"
            self.last_error = reason
            return self._fallback(item, reason, runtime)

        plan.raw = {
            **plan.raw,
            "strategy_geometry_sample": geometry_sample,
            "strategy_geometry_sample_required": True,
        }
        ok, reason = strategy_validator.validate(plan)
        if not ok:
            self.last_status = "fallback_wait"
            self.last_error = f"codex_invalid_plan:{reason}"
            return self._fallback(item, f"codex_invalid_plan:{reason}", runtime)
        self.last_status = "ok"
        self.last_action = plan.action
        return self._record_plan(item, plan, "ok", "", runtime)

    async def _strategy_geometry_sample(self, item: RadarItem) -> dict[str, Any]:
        try:
            return await strategy_geometry_sampler.evaluate(item)
        except Exception as exc:
            return {
                "enabled": True,
                "status": "unavailable",
                "reason": f"geometry_sample_error:{type(exc).__name__}",
                "symbol": item.symbol,
                "side": item.direction,
                "selected_geometry": {},
                "samples": {"sample_count": 0, "pass_gate": False},
            }

    def _run_codex(self, prompt: str, runtime: CodexRuntime) -> str:
        with tempfile.TemporaryDirectory(prefix="ai_radar_codex_") as tmp:
            output_path = Path(tmp) / "strategy_plan.json"
            cmd = self._command(output_path, runtime)
            completed = self.runner(
                cmd,
                cwd=str(Path(__file__).resolve().parents[2]),
                input=prompt,
                text=True,
                encoding="utf-8",
                capture_output=True,
                timeout=runtime.timeout_seconds,
            )
            if completed.returncode != 0:
                message = (completed.stderr or completed.stdout or "").strip()
                raise RuntimeError(message or f"codex exited with {completed.returncode}")
            if output_path.exists():
                return output_path.read_text(encoding="utf-8").strip()
            return (completed.stdout or "").strip()

    def _repair_codex_plan(
        self,
        draft: dict[str, Any],
        item: RadarItem,
        context: dict[str, Any],
        runtime: CodexRuntime,
        failure_reason: str,
    ) -> str:
        compact_context = {
            "symbol": item.symbol,
            "side_bias": item.direction,
            "current_price": item.price,
            "radar": context.get("radar"),
            "market_changes": context.get("market_changes"),
            "market_freshness": context.get("market_freshness"),
            "local_quality_gate": context.get("local_quality_gate"),
            "cyqnt_feature_enhancement": context.get("cyqnt_feature_enhancement"),
            "event_calibration": context.get("event_calibration"),
            "trade_attribution": context.get("trade_attribution"),
            "ai_strategy_quality_feedback": context.get("ai_strategy_quality_feedback"),
            "position_context": context.get("position_context"),
        }
        prompt = CODEX_REPAIR_PROMPT.format(
            failure_reason=failure_reason,
            context_json=json.dumps(compact_context, ensure_ascii=False, indent=2),
            draft_json=json.dumps(draft, ensure_ascii=False, indent=2),
        )
        return self._run_codex(prompt, runtime)

    def _command(self, output_path: Path, runtime: CodexRuntime) -> list[str]:
        cmd = [
            self.codex_command,
            "exec",
            "--ignore-user-config",
            *codex_provider_config_args(),
            "-c",
            f"model_reasoning_effort={runtime.reasoning_effort}",
        ]
        if runtime.service_tier:
            cmd.extend(["-c", f"service_tier={runtime.service_tier}"])
        cmd.extend(
            [
            "--skip-git-repo-check",
            "--ephemeral",
            "--sandbox",
            "read-only",
            "-C",
            str(Path(__file__).resolve().parents[2]),
            "--output-schema",
            str(self.schema_path),
            "--output-last-message",
            str(output_path),
            ]
        )
        if runtime.model:
            cmd.extend(["-m", runtime.model])
        cmd.append("-")
        return cmd

    def _plan_from_dict(self, data: dict[str, Any], item: RadarItem, runtime: CodexRuntime) -> StrategyPlan:
        raw_upgrade = data.get("upgrade_condition") or {}
        if isinstance(raw_upgrade, dict):
            raw_upgrade = {k: v for k, v in raw_upgrade.items() if v is not None}
        else:
            raw_upgrade = {}
        plan = StrategyPlan(
            strategy_id=new_id("codex"),
            action=data.get("action", "WAIT"),
            symbol=data.get("symbol") or item.symbol,
            side=data.get("side") or ("NEUTRAL" if data.get("action") == "WAIT" else item.direction),
            entry_zone_low=float(data.get("entry_zone_low", item.price) or 0),
            entry_zone_high=float(data.get("entry_zone_high", item.price) or 0),
            ideal_entry_price=float(data.get("ideal_entry_price", item.price) or item.price),
            stop_loss=float(data.get("stop_loss", 0) or 0),
            tp1=float(data.get("tp1", 0) or 0),
            tp2=float(data.get("tp2", 0) or 0),
            confidence=float(data.get("confidence", 0) or 0),
            reason=str(data.get("reason") or "codex_plan"),
            wait_type=str(data.get("wait_type") or ""),
            expire_after_seconds=int(data.get("expire_after_seconds", 180) or 180),
            raw={
                "provider": "codex_cli",
                "upgrade_condition": raw_upgrade,
                "model": runtime.model,
                "model_route": runtime.route,
                "reasoning_effort": runtime.reasoning_effort,
                "strategy_geometry_sample": getattr(self, "_active_geometry_sample", {}),
                "strategy_geometry_sample_required": True,
                "cyqnt_feature_enhancement": candidate_feature_enhancer.evaluate(item).asdict(),
            },
        )
        raw_contract = data.get("strategy_contract")
        contract_ok, contract_reasons = contract_quality(raw_contract if isinstance(raw_contract, dict) else None)
        if isinstance(raw_contract, dict) and contract_ok:
            return attach_contract(plan, raw_contract)
        if plan.action != "WAIT":
            detail = ",".join(contract_reasons[:12])
            raise ValueError(f"codex_open_missing_valid_strategy_contract:{detail}")
        return attach_contract(plan, build_rule_contract(item, plan, paper_probe=False))

    def _fallback(self, item: RadarItem, reason: str, runtime: CodexRuntime | None = None) -> StrategyPlan:
        runtime = runtime or codex_runtime_for_strategy(None)
        self.last_action = "WAIT"
        plan = StrategyPlan(
            strategy_id=new_id("codex_wait"),
            action="WAIT",
            symbol=item.symbol,
            side="NEUTRAL",
            entry_zone_low=item.price,
            entry_zone_high=item.price,
            ideal_entry_price=item.price,
            stop_loss=0,
            tp1=0,
            tp2=0,
            confidence=0,
            reason="codex unavailable; wait instead of rule fallback",
            wait_type="WAIT_FOR_AI_RECOVERY",
            expire_after_seconds=180,
            raw={
                "provider": "codex_cli_unavailable",
                "fallback_reason": reason,
                "model": runtime.model,
                "model_route": runtime.route,
                "reasoning_effort": runtime.reasoning_effort,
                "cyqnt_feature_enhancement": candidate_feature_enhancer.evaluate(item).asdict(),
            },
        )
        return self._record_plan(item, plan, "fallback_wait", reason, runtime)

    def _record_plan(self, item: RadarItem, plan: StrategyPlan, status: str, reason: str, runtime: CodexRuntime | None = None) -> StrategyPlan:
        runtime = runtime or codex_runtime_for_strategy(None)
        row = {
            "ts_ms": now_ms(),
            "provider": "codex_cli",
            "model": runtime.model,
            "model_route": runtime.route,
            "reasoning_effort": runtime.reasoning_effort,
            "timeout_seconds": runtime.timeout_seconds,
            "status": status,
            "status_reason": reason,
            "symbol": item.symbol,
            "signal": {
                "rank": item.rank,
                "side": item.direction,
                "score": item.score,
                "fund_confirm": f"{item.fund_confirm_count}/{item.fund_confirm_total}",
                "fake_breakout_risk": item.fake_breakout_risk,
                "volume_spike": item.volume_spike,
                "wick_ratio": item.wick_ratio,
                "atr_pct": item.atr_pct,
                "taker_buy_ratio": item.taker_buy_ratio,
                "taker_sell_ratio": item.taker_sell_ratio,
                "depth_imbalance": item.depth_imbalance,
                "sm_delta": item.sm_delta,
                "cyqnt_feature_enhancement": candidate_feature_enhancer.evaluate(item).asdict(),
            },
            "plan": asdict(plan),
        }
        self.last_plan = row
        self.recent_plans.insert(0, row)
        self.recent_plans = self.recent_plans[:30]
        return plan

    def status(self) -> dict[str, Any]:
        resolved = shutil.which(self.codex_command) or ""
        return {
            "configured_command": self.codex_command,
            "resolved_command": resolved or self.codex_command,
            "command_found": bool(resolved),
            "model_provider": normalized_codex_model_provider() or "openai",
            "provider_name": settings.codex_provider_name,
            "provider_requires_openai_auth": bool(settings.codex_provider_requires_openai_auth),
            "provider_supports_websockets": bool(settings.codex_provider_supports_websockets),
            "model": settings.codex_model,
            "timeout_seconds": settings.codex_timeout_seconds,
            "reasoning_effort": normalized_codex_reasoning_effort(),
            "service_tier": normalized_codex_service_tier(),
            "routing": {
                "primary_strategy": asdict(codex_runtime_for_strategy(None)),
                "fast_validation": asdict(
                    codex_runtime_for_strategy({"candidate_selection": {"source": "strict_review"}})
                ),
            },
            "schema_path": str(self.schema_path),
            "schema_exists": self.schema_path.exists(),
            "invocation_count": self.invocation_count,
            "last_invoked_ms": self.last_invoked_ms,
            "last_status": self.last_status,
            "last_error": self.last_error,
            "last_model": self.last_model,
            "last_route": self.last_route,
            "last_reasoning_effort": self.last_reasoning_effort,
            "last_timeout_seconds": self.last_timeout_seconds,
            "last_symbol": self.last_symbol,
            "last_action": self.last_action,
            "last_repair_attempted": self.last_repair_attempted,
            "last_repair_reason": self.last_repair_reason,
            "last_plan": self.last_plan,
            "recent_plans": self.recent_plans[:10],
        }


def run_command(
    cmd: Sequence[str],
    *,
    cwd: str,
    input: str,
    text: bool,
    encoding: str,
    capture_output: bool,
    timeout: float,
) -> subprocess.CompletedProcess[str]:
    lock_timeout = max(1.0, min(float(timeout or 1.0), 60.0))
    if not CODEX_CLI_LOCK.acquire(timeout=lock_timeout):
        raise RuntimeError("codex_cli_busy")
    stdout = subprocess.PIPE if capture_output else None
    stderr = subprocess.PIPE if capture_output else None
    try:
        popen_cmd = _windows_batch_wrapper(cmd)
        process = subprocess.Popen(
            popen_cmd,
            cwd=cwd,
            stdin=subprocess.PIPE,
            stdout=stdout,
            stderr=stderr,
            text=text,
            encoding=encoding,
        )
        try:
            out, err = process.communicate(input=input, timeout=timeout)
        except subprocess.TimeoutExpired:
            kill_process_tree(process.pid)
            process.wait()
            raise
        return subprocess.CompletedProcess(cmd, process.returncode, stdout=out, stderr=err)
    finally:
        CODEX_CLI_LOCK.release()


def _windows_batch_wrapper(cmd: Sequence[str]) -> list[str]:
    values = list(cmd)
    command = str(values[0]) if values else ""
    if os.name != "nt" or not command:
        return values

    resolved = shutil.which(command) or command
    values[0] = resolved
    if resolved.lower().endswith((".cmd", ".bat")):
        return ["cmd.exe", "/d", "/s", "/c", subprocess.list2cmdline(values)]
    return values


def normalized_reasoning_effort(effort: str | None, default: str = "medium") -> str:
    fallback = (default or "medium").strip().lower()
    if fallback not in REASONING_EFFORTS:
        fallback = "medium"
    effort = (effort or default).strip().lower()
    return effort if effort in REASONING_EFFORTS else fallback


def normalized_codex_reasoning_effort() -> str:
    return normalized_reasoning_effort(settings.codex_reasoning_effort, "medium")


def normalized_service_tier(tier: str | None, default: str = "fast", *, allow_empty: bool = False) -> str:
    tier = (tier or "").strip().lower()
    if tier in {"", "none", "off", "auto"}:
        if allow_empty:
            return ""
        tier = (default or "fast").strip().lower()
    if tier == "priority":
        return "fast"
    if tier in SERVICE_TIERS:
        return tier
    if allow_empty:
        return ""
    fallback = (default or "fast").strip().lower()
    if fallback == "priority":
        return "fast"
    return fallback if fallback in SERVICE_TIERS else "fast"


def normalized_codex_service_tier() -> str:
    return normalized_service_tier(settings.codex_service_tier, "fast")


def normalized_codex_model_provider() -> str:
    provider = str(settings.codex_model_provider or "").strip()
    return provider if re.fullmatch(r"[A-Za-z0-9_]+", provider) else ""


def _toml_string(value: str) -> str:
    return json.dumps(str(value), ensure_ascii=True)


def codex_provider_config_args() -> list[str]:
    provider = normalized_codex_model_provider()
    if not provider:
        return []
    prefix = f"model_providers.{provider}"
    return [
        "-c",
        f"model_provider={_toml_string(provider)}",
        "-c",
        f"{prefix}.name={_toml_string(settings.codex_provider_name or provider)}",
        "-c",
        f"{prefix}.requires_openai_auth={str(bool(settings.codex_provider_requires_openai_auth)).lower()}",
        "-c",
        f"{prefix}.supports_websockets={str(bool(settings.codex_provider_supports_websockets)).lower()}",
        "-c",
        f'{prefix}.wire_api="responses"',
    ]


def codex_runtime_for_strategy(position_context: dict | None = None) -> CodexRuntime:
    context = position_context or {}
    selection = context.get("candidate_selection") if isinstance(context.get("candidate_selection"), dict) else {}
    source = str(selection.get("source") or context.get("candidate_source") or "").strip()
    production_acceptance = bool(selection.get("production_acceptance"))
    use_fast_validation = (
        (source in {"paper_top", "strict_review"} or source.startswith("paper_probe"))
        and not production_acceptance
        and bool(str(settings.codex_fast_model or "").strip())
    )
    if use_fast_validation:
        return CodexRuntime(
            route="fast_validation",
            model=str(settings.codex_fast_model or "").strip(),
            reasoning_effort=normalized_reasoning_effort(settings.codex_fast_reasoning_effort, "low"),
            service_tier=normalized_service_tier(settings.codex_fast_service_tier, "", allow_empty=True),
            timeout_seconds=max(10.0, float(settings.codex_fast_timeout_seconds or settings.codex_timeout_seconds or 90.0)),
        )
    return CodexRuntime(
        route="primary_strategy",
        model=str(settings.codex_model or "").strip(),
        reasoning_effort=normalized_codex_reasoning_effort(),
        service_tier=normalized_codex_service_tier(),
        timeout_seconds=max(10.0, float(settings.codex_timeout_seconds or 90.0)),
    )


def kill_process_tree(pid: int) -> None:
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        subprocess.run(["kill", "-TERM", str(pid)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def default_codex_command() -> str:
    if os.name == "nt":
        return shutil.which("codex.cmd") or shutil.which("codex.exe") or "codex.cmd"
    return shutil.which("codex") or "codex"


def redact_secret(value: Any) -> str:
    return re.sub(r"sk-[A-Za-z0-9_-]{8,}", "sk-***REDACTED***", str(value or ""))


def compact_error_detail(value: str) -> str:
    text = str(value or "").replace("\r", " ").replace("\n", " ")
    if len(text) <= 700:
        return text
    return f"{text[:180]} ... {text[-500:]}"


codex_cli_strategy_client = CodexCLIStrategyClient()
