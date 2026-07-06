from __future__ import annotations

import asyncio
import json
from typing import Any

from backend.ai_strategy.ai_service import ai_service
from backend.ai_strategy.strategy_contract import contract_quality
from backend.ai_strategy.strategy_validator import strategy_validator
from backend.config import settings
from backend.models import RadarItem, now_ms


EXPECTED_ACTION = "OPEN_LONG"
ACCEPTANCE_SYMBOL = "BTCUSDT"


def build_acceptance_item() -> RadarItem:
    price = 62700.0
    entry_low = 62600.0
    entry_high = 62830.0
    stop_loss = 62050.0
    tp1 = 63820.0
    tp2 = 65350.0
    item = RadarItem(
        rank=1,
        symbol=ACCEPTANCE_SYMBOL,
        base_asset="BTC",
        price=price,
        direction="LONG",
        stage="confirmed_acceleration",
        trigger_mode="score_flow_structure_alignment",
        score=98.0,
        score_history=[61.0, 72.0, 84.0, 91.0, 98.0],
        rank_history=[9, 5, 3, 2, 1],
        heat_slope=13.5,
        slope_score=96.0,
        fake_breakout_risk="LOW",
        change_5m=0.72,
        change_15m=1.46,
        change_1h=2.85,
        oi_change=1.35,
        fund_confirm_count=5,
        fund_confirm_total=5,
        dealer_radar="long_extension_confirmed",
        sm_position=69.0,
        sm_delta=1.35,
        volume_spike=3.45,
        funding_rate=0.00008,
        taker_buy_ratio=0.71,
        taker_sell_ratio=0.29,
        depth_imbalance=0.26,
        atr_pct=0.92,
        wick_ratio=0.16,
        ai_candidate=True,
        ts_ms=now_ms(),
    )
    item.score_explain = {
        "top_positive": [
            "fund_confirm_full",
            "taker_buy_pressure",
            "depth_bid_imbalance",
            "multi_timeframe_continuation",
        ],
        "top_penalty": [],
        "calibration": {"source": "codex_generation_acceptance", "trusted": True},
        "caveat": "acceptance fixture; no exchange order is sent by strategy generation",
    }
    item.score_features = {
        "structure_metrics": {
            "current_wick_ratio": 0.16,
            "current_body_ratio": 0.72,
            "higher_high_break": True,
            "pullback_depth_pct": 0.28,
        },
        "universal_anomaly_model": {
            "direction": "LONG",
            "long_probability": 0.79,
            "short_probability": 0.08,
            "neutral_probability": 0.13,
            "confidence": 0.78,
            "features": {
                "flow_alignment": 0.84,
                "depth_alignment": 0.76,
                "momentum_alignment": 0.81,
            },
        },
        "market_structure": {"setup": "pullback_continuation", "quality": "high"},
    }
    item.market_structure = {
        "action": EXPECTED_ACTION,
        "setup": "pullback_continuation",
        "side": "LONG",
        "entry_zone_low": entry_low,
        "entry_zone_high": entry_high,
        "ideal_entry_price": price,
        "stop_loss": stop_loss,
        "tp1": tp1,
        "tp2": tp2,
        "confidence": 92.0,
        "no_trade_reasons": [],
        "evidence": [
            "5m/15m/1h momentum aligned upward",
            "fund_confirm=5/5",
            "taker_buy_ratio=0.71",
            "depth_imbalance=0.26",
            "current_wick_ratio=0.16",
        ],
    }
    return item


def build_acceptance_context(item: RadarItem | None = None) -> dict[str, Any]:
    item = item or build_acceptance_item()
    geometry = {
        "side": "LONG",
        "entry": 62700.0,
        "entry_zone_low": 62600.0,
        "entry_zone_high": 62830.0,
        "stop_loss": 62050.0,
        "tp1": 63820.0,
        "tp2": 65350.0,
        "risk_pct": 0.010367,
        "tp1_r": 1.7231,
        "tp2_r": 4.0769,
    }
    return {
        "candidate_selection": {
            "source": "production_acceptance",
            "acceptance_mode": True,
            "paper_validation": True,
            "latest_market_required": False,
            "candidate_symbols": [item.symbol],
            "pre_ai_market_refresh": {
                "scan_ok": True,
                "symbol_present_after_scan": True,
                "market_data_source": "acceptance_fixture",
                "age_seconds": 0,
            },
        },
        "strategy_geometry_sample": {
            "enabled": True,
            "status": "ok",
            "reason": "deterministic acceptance geometry with positive R and clean first-touch sample",
            "sample_model": "codex_acceptance_geometry_v1",
            "symbol": item.symbol,
            "side": "LONG",
            "interval": "5m",
            "variant_count": 42,
            "pass_count": 31,
            "selected_geometry": geometry,
            "samples": {
                "sample_count": 96,
                "win_rate": 0.625,
                "expected_r": 0.48,
                "profit_factor": 1.82,
                "cost_r": 0.0868,
                "tp2_hit_rate": 0.365,
                "stop_hit_rate": 0.281,
                "timeout_rate": 0.354,
                "max_adverse_pct": 0.74,
                "horizon_steps": 36,
                "pass_gate": True,
            },
            "instruction": "Use selected_geometry unless current evidence invalidates it; live permission remains false.",
        },
        "ai_strategy_quality_feedback": {
            "candidate_feedback": {
                "generation_gate": {
                    "allow_open_plan": True,
                    "reasons": [],
                    "review_required": False,
                    "instruction": "This deterministic acceptance candidate is allowed to produce an OPEN plan.",
                }
            }
        },
        "cyqnt_feature_enhancement": {
            "symbol": item.symbol,
            "side": "LONG",
            "cyqnt_available": True,
            "feature_score": 93.4,
            "estimated_win_rate": 0.64,
            "selection_score": 94.7,
            "attribution_samples": 96,
            "attribution_win_rate": 0.61,
            "attribution_profit_factor": 1.76,
            "event_samples": 84,
            "event_win_rate": 0.63,
            "event_profit_factor": 1.81,
            "reasons": [
                "acceptance_feature_report",
                "feature_score_strong",
                "estimated_win_rate_above_paper_gate",
            ],
            "contributions": {
                "trend": 5.7,
                "flow": 6.4,
                "structure": 5.5,
                "liquidity": 6.8,
                "noise": 4.2,
                "funding": -0.1,
            },
            "positive_factors": [
                "feature_score_strong",
                "estimated_win_rate_above_paper_gate",
                "fund_confirm_full",
                "fake_breakout_risk_low",
                "flow_positive",
                "liquidity_positive",
            ],
            "failure_risks": [],
        },
        "trade_attribution": {
            "enabled": True,
            "sample_count": 96,
            "win_rate": 0.61,
            "profit_factor": 1.76,
            "pnl": 24.8,
            "avg_pnl": 0.258333,
            "main_loss_causes": [],
            "main_profit_drivers": ["flow_alignment", "clean_wick_profile", "positive_depth"],
            "blocked_symbol_sides": [],
            "current_signal_attribution": {
                "pattern": "acceptance_pullback_continuation_LONG",
                "factors": ["trend_positive", "flow_positive", "structure_positive"],
                "matched_samples": 96,
                "match_level": "production_acceptance_fixture",
                "win_rate": 0.61,
                "profit_factor": 1.76,
                "pnl": 24.8,
                "paper_ok": True,
                "reasons": ["acceptance_attribution_positive"],
                "advice": ["paper-only acceptance; do not grant live permission"],
            },
            "instruction": "Use this deterministic acceptance attribution instead of local database history for this fixture only.",
        },
        "event_calibration": {
            "enabled": True,
            "sample_count": 84,
            "global_win_rate": 0.63,
            "global_profit_factor": 1.81,
            "global_pnl": 21.6,
            "minimums": {
                "samples": 20,
                "paper_win_rate": 0.6,
                "paper_profit_factor": 1.2,
                "live_win_rate": 0.62,
                "live_profit_factor": 1.35,
            },
            "best_patterns": ["acceptance_pullback_continuation_LONG"],
            "worst_patterns": [],
            "similar_current_event": {
                "match_level": "production_acceptance_fixture",
                "samples": 84,
                "win_rate": 0.63,
                "profit_factor": 1.81,
                "pnl": 21.6,
            },
            "instruction": "Use this deterministic acceptance event calibration instead of local database history for this fixture only.",
        },
        "performance_guard": {"recovery_mode": False},
        "risk_policy": {
            "real_order_allowed": False,
            "live_permission": False,
            "paper_only": True,
            "max_position_risk_pct": 0.012,
        },
        "required_acceptance": {
            "expected_action": EXPECTED_ACTION,
            "expected_side": "LONG",
            "must_use_provider": "codex_cli",
            "must_not_use_provider": "codex_cli_unavailable",
            "must_pass": [
                "strategy_validator.validate",
                "contract_quality",
                "strategy_contract_quality",
            ],
            "contract_requirements": [
                "signal/risk/execution separated",
                "position lifecycle with hold/reduce/add/exit/time_stop",
                "review_metrics include MFE, MAE, R_multiple",
                "research_review has role_a_researcher and role_b_risk_officer",
                "allowed_stages.live must be false without explicit user approval",
            ],
        },
    }


async def run_acceptance() -> tuple[int, dict[str, Any]]:
    item = build_acceptance_item()
    context = build_acceptance_context(item)
    config_error = _config_error()
    status = ai_service.status(candidate_count=1, candidate_source="production_acceptance")
    before_audit = status.get("audit") if isinstance(status.get("audit"), dict) else {}
    before_total = int(before_audit.get("total") or 0)
    codex_status = status.get("codex_cli") if isinstance(status.get("codex_cli"), dict) else {}
    if config_error:
        return _fail(config_error, status=status)
    if not codex_status.get("ready_for_generation"):
        return _fail(str(codex_status.get("availability_reason") or "codex_not_ready"), status=status)

    plan = await ai_service.generate_strategy(item, context)
    post_status = ai_service.status(candidate_count=1, candidate_source="production_acceptance")
    audit = post_status.get("audit") if isinstance(post_status.get("audit"), dict) else {}
    validator_ok, validator_reason = strategy_validator.validate(plan)
    contract = plan.raw.get("strategy_contract") if isinstance(plan.raw, dict) else None
    contract_ok, contract_reasons = contract_quality(contract if isinstance(contract, dict) else None)
    raw_provider = str((plan.raw or {}).get("provider") or "")
    raw_quality = (plan.raw or {}).get("strategy_contract_quality")
    allowed_stages = contract.get("allowed_stages") if isinstance(contract, dict) else {}

    failures: list[str] = []
    if raw_provider != "codex_cli":
        failures.append(f"provider_not_codex_cli:{raw_provider or 'missing'}")
    if raw_provider == "codex_cli_unavailable":
        failures.append("codex_cli_unavailable")
    if (plan.raw or {}).get("fallback_reason"):
        failures.append(f"fallback_reason:{plan.raw.get('fallback_reason')}")
    if plan.action != EXPECTED_ACTION:
        failures.append(f"action_not_{EXPECTED_ACTION}:{plan.action}")
    if plan.side != "LONG":
        failures.append(f"side_not_LONG:{plan.side}")
    if not (plan.stop_loss < plan.ideal_entry_price < plan.tp1 < plan.tp2):
        failures.append("invalid_long_geometry")
    if not validator_ok:
        failures.append(f"strategy_validator:{validator_reason}")
    if not contract_ok:
        failures.append("contract_quality:" + ",".join(contract_reasons[:6]))
    if not isinstance(raw_quality, dict) or raw_quality.get("ok") is not True:
        failures.append("strategy_contract_quality_not_ok")
    if isinstance(allowed_stages, dict) and allowed_stages.get("live") is True:
        failures.append("live_stage_enabled_without_explicit_approval")
    tradable_by_source = (
        audit.get("tradable_strategy_by_source")
        if isinstance(audit.get("tradable_strategy_by_source"), dict)
        else {}
    )
    tradable_by_source_provider = (
        audit.get("tradable_strategy_by_source_provider")
        if isinstance(audit.get("tradable_strategy_by_source_provider"), dict)
        else {}
    )
    production_acceptance_providers = (
        tradable_by_source_provider.get("production_acceptance")
        if isinstance(tradable_by_source_provider.get("production_acceptance"), dict)
        else {}
    )
    if int(tradable_by_source.get("production_acceptance") or 0) < 1:
        failures.append("ai_task_audit_missing_production_acceptance_tradable_strategy")
    if int(production_acceptance_providers.get("codex_cli") or 0) < 1:
        failures.append("ai_task_audit_missing_production_acceptance_codex_tradable_strategy")
    last_tradable = audit.get("last_tradable_strategy") if isinstance(audit.get("last_tradable_strategy"), dict) else {}
    recent_tasks = audit.get("recent_strategy_tasks") if isinstance(audit.get("recent_strategy_tasks"), list) else []
    after_total = int(audit.get("total") or 0)
    if after_total <= before_total:
        failures.append(f"ai_task_audit_not_incremented:{before_total}->{after_total}")
    current_acceptance_task = _find_current_acceptance_task(recent_tasks, item.symbol)
    if not current_acceptance_task:
        failures.append("ai_task_audit_missing_current_production_acceptance_tradable_strategy")
    if failures:
        return _fail("codex_strategy_generation_failed", failures=failures, status=post_status, plan=plan)

    return 0, {
        "ok": True,
        "code": "codex_real_strategy_generated",
        "symbol": plan.symbol,
        "action": plan.action,
        "side": plan.side,
        "provider": raw_provider,
        "strategy_id": plan.strategy_id,
        "model": (plan.raw or {}).get("model"),
        "model_route": (plan.raw or {}).get("model_route"),
        "reasoning_effort": (plan.raw or {}).get("reasoning_effort"),
        "entry": plan.ideal_entry_price,
        "stop_loss": plan.stop_loss,
        "tp1": plan.tp1,
        "tp2": plan.tp2,
        "confidence": plan.confidence,
        "validator": validator_reason,
        "strategy_contract_quality": raw_quality,
        "contract_sections": sorted(contract.keys()) if isinstance(contract, dict) else [],
        "allowed_stages": allowed_stages,
        "codex_status": _compact_codex_status(codex_status),
        "ai_task_audit": {
            "tradable_strategy_count": audit.get("tradable_strategy_count"),
            "tradable_strategy_by_source": tradable_by_source,
            "tradable_strategy_by_source_provider": tradable_by_source_provider,
            "invalid_strategy_count": audit.get("invalid_strategy_count"),
            "last_tradable_strategy": last_tradable,
            "current_acceptance_task": current_acceptance_task,
            "recent_strategy_tasks": recent_tasks[:3],
        },
    }


def _config_error() -> str:
    provider = str(settings.ai_strategy_provider or "").strip().lower()
    if not settings.ai_enabled:
        return "ai_enabled_false"
    if provider != "codex_cli":
        return f"provider_not_codex_cli:{provider or 'missing'}"
    if not settings.require_codex_strategy_for_entry:
        return "require_codex_strategy_for_entry_false"
    return ""


def _fail(
    code: str,
    *,
    failures: list[str] | None = None,
    status: dict[str, Any] | None = None,
    plan: Any | None = None,
) -> tuple[int, dict[str, Any]]:
    return 1, {
        "ok": False,
        "code": code,
        "failures": failures or [],
        "status": _compact_status(status or {}),
        "plan": _plan_summary(plan),
    }


def _find_current_acceptance_task(tasks: list[Any], symbol: str) -> dict[str, Any]:
    for task in tasks:
        if not isinstance(task, dict):
            continue
        if task.get("candidate_source") != "production_acceptance":
            continue
        if task.get("provider") != "codex_cli":
            continue
        if task.get("symbol") != symbol:
            continue
        if task.get("action") != EXPECTED_ACTION:
            continue
        if task.get("tradable_strategy") is not True:
            continue
        if task.get("valid") is not True:
            continue
        return task
    return {}


def _compact_status(status: dict[str, Any]) -> dict[str, Any]:
    codex = status.get("codex_cli") if isinstance(status.get("codex_cli"), dict) else {}
    return {
        "enabled": status.get("enabled"),
        "provider": status.get("provider"),
        "candidate_source": status.get("candidate_source"),
        "candidate_count_before_ai": status.get("candidate_count_before_ai"),
        "will_invoke_for_current_candidates": status.get("will_invoke_for_current_candidates"),
        "not_invoked_reason": status.get("not_invoked_reason"),
        "codex_cli": _compact_codex_status(codex),
    }


def _compact_codex_status(codex: dict[str, Any]) -> dict[str, Any]:
    return {
        "ready_for_generation": codex.get("ready_for_generation"),
        "availability_reason": codex.get("availability_reason"),
        "command_found": codex.get("command_found"),
        "schema_exists": codex.get("schema_exists"),
        "auth_available": codex.get("auth_available"),
        "auth_source": codex.get("auth_source"),
        "model": codex.get("model"),
        "model_route": codex.get("model_route"),
        "reasoning_effort": codex.get("reasoning_effort"),
        "timeout_seconds": codex.get("timeout_seconds"),
        "last_status": codex.get("last_status"),
        "last_error": codex.get("last_error"),
    }


def _plan_summary(plan: Any | None) -> dict[str, Any]:
    if plan is None:
        return {}
    raw = plan.raw if isinstance(getattr(plan, "raw", None), dict) else {}
    return {
        "action": getattr(plan, "action", None),
        "symbol": getattr(plan, "symbol", None),
        "side": getattr(plan, "side", None),
        "entry": getattr(plan, "ideal_entry_price", None),
        "stop_loss": getattr(plan, "stop_loss", None),
        "tp1": getattr(plan, "tp1", None),
        "tp2": getattr(plan, "tp2", None),
        "confidence": getattr(plan, "confidence", None),
        "reason": getattr(plan, "reason", None),
        "wait_type": getattr(plan, "wait_type", None),
        "provider": raw.get("provider"),
        "fallback_reason": raw.get("fallback_reason"),
        "quality_block": raw.get("quality_block"),
        "strategy_contract_quality": raw.get("strategy_contract_quality"),
    }


def main() -> int:
    code, payload = asyncio.run(run_acceptance())
    print(json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
