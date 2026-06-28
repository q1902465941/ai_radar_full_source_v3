from __future__ import annotations

from typing import Any

from backend.config import settings
from backend.learning.strategy_filter import direction_confirmations
from backend.models import RadarItem, StrategyPlan
from backend.radar.candidate_feature_enhancer import candidate_feature_enhancer
from backend.trading.trade_economics import round_trip_cost_pct, stop_distance_pct


REQUIRED_SECTIONS = [
    "hypothesis",
    "signal",
    "risk",
    "execution",
    "position_lifecycle",
    "hold_logic",
    "reduce_logic",
    "add_logic",
    "exit_logic",
    "time_stop",
    "review_metrics",
    "entry_conditions",
    "avoid_conditions",
    "invalidation",
    "position_management",
    "cost_constraints",
    "learning_tags",
    "allowed_stages",
    "research_review",
]


def build_rule_contract(item: RadarItem, plan: StrategyPlan, *, paper_probe: bool = False) -> dict[str, Any]:
    side = plan.side if plan.side in {"LONG", "SHORT"} else item.direction
    confirmations = direction_confirmations(item.asdict(), side)
    cyqnt_report = candidate_feature_enhancer.evaluate(item).asdict()
    return {
        "version": "strategy_contract_v1",
        "strategy_kind": "paper_probe_event_followthrough" if paper_probe else "radar_event_followthrough",
        "hypothesis": _hypothesis(item, side, paper_probe),
        "signal": _signal_section(item, side, cyqnt_report),
        "risk": _risk_section(plan),
        "execution": _execution_section(plan, paper_probe),
        "position_lifecycle": _position_lifecycle(),
        "hold_logic": _hold_logic(item, side, cyqnt_report),
        "reduce_logic": _reduce_logic(plan),
        "add_logic": _add_logic(),
        "exit_logic": _exit_logic(plan),
        "time_stop": _time_stop(plan),
        "review_metrics": ["MFE", "MAE", "R_multiple", "max_drawdown", "hold_time", "early_exit", "late_exit"],
        "entry_conditions": _entry_conditions(item, side),
        "avoid_conditions": _avoid_conditions(item, side),
        "invalidation": _invalidation(plan),
        "position_management": _position_management(plan),
        "cost_constraints": _cost_constraints(plan),
        "learning_tags": _learning_tags(item, side, confirmations, cyqnt_report),
        "cyqnt_feature_enhancement": cyqnt_report,
        "allowed_stages": _allowed_stages(paper_probe),
        "research_review": _research_review(item, plan, side, paper_probe, cyqnt_report),
        "graduation_rule": {
            "paper_to_test_order": "Require positive paper PnL, win rate >= 52%, recent win rate >= 50%, attribution PF >= 1.05, no recovery mode.",
            "test_order_to_micro_live": "Require explicit user approval, test order success, protection orders enabled, and max_open_positions=1.",
        },
    }


def contract_quality(contract: dict[str, Any] | None) -> tuple[bool, list[str]]:
    if not isinstance(contract, dict):
        return False, ["strategy_contract_missing"]
    missing = [section for section in REQUIRED_SECTIONS if not contract.get(section)]
    reasons: list[str] = [f"contract_missing:{section}" for section in missing]

    entry = contract.get("entry_conditions")
    avoid = contract.get("avoid_conditions")
    invalidation = contract.get("invalidation")
    cost = contract.get("cost_constraints")
    stages = contract.get("allowed_stages")
    signal = contract.get("signal")
    risk = contract.get("risk")
    execution = contract.get("execution")
    lifecycle = contract.get("position_lifecycle")
    hold_logic = contract.get("hold_logic")
    reduce_logic = contract.get("reduce_logic")
    add_logic = contract.get("add_logic")
    exit_logic = contract.get("exit_logic")
    time_stop = contract.get("time_stop")
    review_metrics = contract.get("review_metrics")
    review = contract.get("research_review")
    if not isinstance(signal, dict) or not signal.get("entry") or not signal.get("evidence"):
        reasons.append("contract_signal_incomplete")
    if not isinstance(risk, dict) or not risk.get("max_loss") or not risk.get("failure_modes"):
        reasons.append("contract_risk_incomplete")
    if not isinstance(execution, dict) or not execution.get("order_plan") or not execution.get("fill_assumption"):
        reasons.append("contract_execution_incomplete")
    if not isinstance(lifecycle, dict) or not lifecycle.get("states") or not lifecycle.get("principle"):
        reasons.append("contract_lifecycle_incomplete")
    if not isinstance(hold_logic, dict) or not hold_logic.get("continue_holding_if"):
        reasons.append("contract_hold_logic_incomplete")
    if not isinstance(reduce_logic, dict) or not reduce_logic.get("reduce_if"):
        reasons.append("contract_reduce_logic_incomplete")
    if not _add_logic_complete(add_logic):
        reasons.append("contract_add_logic_incomplete")
    if not isinstance(exit_logic, dict) or not exit_logic.get("core_exit_only_if"):
        reasons.append("contract_exit_logic_incomplete")
    if not isinstance(time_stop, dict) or not time_stop.get("rule"):
        reasons.append("contract_time_stop_incomplete")
    if not isinstance(review_metrics, list) or not {"MFE", "MAE", "R_multiple"}.issubset(set(review_metrics)):
        reasons.append("contract_review_metrics_incomplete")
    if not isinstance(entry, list) or len(entry) < 3:
        reasons.append("contract_entry_conditions_too_thin")
    if not isinstance(avoid, list) or len(avoid) < 2:
        reasons.append("contract_avoid_conditions_too_thin")
    if not isinstance(invalidation, dict) or "hard_stop" not in invalidation or not invalidation.get("signal_failure"):
        reasons.append("contract_invalidation_incomplete")
    if not isinstance(cost, dict) or "min_net_profit_usdt" not in cost or "min_profit_cost_ratio" not in cost:
        reasons.append("contract_cost_constraints_incomplete")
    if not isinstance(stages, dict) or "paper_probe" not in stages or "live" not in stages:
        reasons.append("contract_allowed_stages_incomplete")
    if not isinstance(review, dict) or not review.get("role_a_researcher") or not review.get("role_b_risk_officer"):
        reasons.append("contract_research_review_incomplete")
    return not reasons, reasons


def _add_logic_complete(add_logic: Any) -> bool:
    if not isinstance(add_logic, dict) or "max_adds" not in add_logic:
        return False
    try:
        max_adds = int(float(add_logic.get("max_adds") or 0))
    except Exception:
        return False
    add_if = add_logic.get("add_if")
    reason = str(add_logic.get("reason") or "").strip()
    if max_adds == 0:
        return isinstance(add_if, list) and bool(reason)
    return isinstance(add_if, list) and bool(add_if) and bool(reason)


def attach_contract(plan: StrategyPlan, contract: dict[str, Any]) -> StrategyPlan:
    ok, reasons = contract_quality(contract)
    plan.raw = {
        **plan.raw,
        "strategy_contract": contract,
        "strategy_contract_quality": {
            "ok": ok,
            "reasons": reasons,
        },
    }
    return plan


def _hypothesis(item: RadarItem, side: str, paper_probe: bool) -> str:
    mode = "paper probe" if paper_probe else "trade candidate"
    if side == "LONG":
        direction_text = "upside continuation after radar acceleration"
    elif side == "SHORT":
        direction_text = "downside continuation after radar acceleration"
    else:
        direction_text = "no directional edge"
    return f"{mode}: {item.symbol} has possible {direction_text}; only valid while flow, depth, and timeframe evidence stay aligned."


def _signal_section(item: RadarItem, side: str, cyqnt_report: dict[str, Any]) -> dict[str, Any]:
    return {
        "entry": f"Observe {item.symbol} only when radar direction, fund confirmation, taker flow, depth, and timeframe movement agree with {side}.",
        "evidence": [
            f"fund_confirm_count={item.fund_confirm_count}/{item.fund_confirm_total}",
            f"score={item.score}",
            f"fake_breakout_risk={item.fake_breakout_risk}",
            f"taker_buy_ratio={item.taker_buy_ratio}",
            f"taker_sell_ratio={item.taker_sell_ratio}",
            f"depth_imbalance={item.depth_imbalance}",
            f"cyqnt_feature_score={cyqnt_report.get('feature_score')}",
            f"cyqnt_estimated_win_rate={cyqnt_report.get('estimated_win_rate')}",
            f"cyqnt_selection_score={cyqnt_report.get('selection_score')}",
        ],
        "cyqnt_contributions": cyqnt_report.get("contributions") or {},
        "cyqnt_reasons": cyqnt_report.get("reasons") or [],
        "not_enough_if": [
            "indicator-only direction without flow/depth confirmation",
            "single spike without follow-through",
            "mixed 5m/15m/1h movement",
            "cyqnt feature report shows weak feature score or dominant noise/funding risk",
        ],
    }


def _risk_section(plan: StrategyPlan) -> dict[str, Any]:
    entry = float(plan.ideal_entry_price or 0.0)
    stop_pct = stop_distance_pct(entry, plan.stop_loss) if entry > 0 else 0.0
    return {
        "max_loss": {
            "hard_stop": plan.stop_loss,
            "stop_distance_pct": round(stop_pct, 6),
            "defined_before_entry": True,
        },
        "failure_modes": [
            "signal reverses after entry",
            "cost drag is larger than expected edge",
            "similar learned attribution bucket remains negative",
            "breakout is a wick/trap instead of continuation",
        ],
        "reject_if": [
            "reward after fees and slippage is too small",
            "stop distance is too tight relative to round-trip cost",
            "no clean invalidation level exists",
        ],
    }


def _execution_section(plan: StrategyPlan, paper_probe: bool) -> dict[str, Any]:
    stage = "paper_probe" if paper_probe else "paper_or_shadow_only"
    return {
        "stage": stage,
        "order_plan": "No real exchange order is generated by StrategyPlan; execution layer must decide separately.",
        "fill_assumption": "Entry must be achievable inside the entry zone after configured fees and slippage.",
        "cost_checks": [
            f"min_net_profit_usdt={settings.trade_min_net_profit_usdt}",
            f"min_profit_cost_ratio={settings.trade_min_profit_cost_ratio}",
            f"round_trip_cost_pct={round(round_trip_cost_pct(), 6)}",
        ],
        "live_permission": "false unless user explicitly approves live_test_order and readiness gates pass",
    }


def _position_lifecycle() -> dict[str, Any]:
    return {
        "states": [
            "WAITING",
            "ENTRY_READY",
            "OPENED",
            "PROTECTING",
            "TREND_HOLD",
            "SCALE_IN",
            "SCALE_OUT",
            "DEFENSIVE",
            "EXIT_READY",
            "CLOSED",
        ],
        "initial_state_after_fill": "PROTECTING",
        "principle": "scan results are evidence, not orders; position management decides hold, reduce, add, or exit after entry.",
    }


def _hold_logic(item: RadarItem, side: str, cyqnt_report: dict[str, Any]) -> dict[str, Any]:
    if side == "LONG":
        structure = "higher-low structure is intact or price remains above the invalidation level"
        flow = "taker/depth evidence remains neutral-to-long"
    elif side == "SHORT":
        structure = "lower-high structure is intact or price remains below the invalidation level"
        flow = "taker/depth evidence remains neutral-to-short"
    else:
        structure = "directional structure is intact"
        flow = "flow evidence remains aligned"
    return {
        "continue_holding_if": [
            structure,
            flow,
            "price has not hit hard stop",
            "minor counter-signal does not break the trade thesis",
        ],
        "do_not_exit_for": [
            "one small reverse tick",
            "minor score noise while risk remains controlled",
            "temporary pullback that does not break structure",
        ],
        "evidence_from_scan": [
            f"score_at_entry={item.score}",
            f"fund_confirm={item.fund_confirm_count}/{item.fund_confirm_total}",
            f"fake_breakout_risk={item.fake_breakout_risk}",
            f"cyqnt_feature_score_at_entry={cyqnt_report.get('feature_score')}",
            f"cyqnt_estimated_win_rate_at_entry={cyqnt_report.get('estimated_win_rate')}",
        ],
    }


def _reduce_logic(plan: StrategyPlan) -> dict[str, Any]:
    return {
        "reduce_if": [
            "price reaches TP1 and net partial profit is positive after fees and slippage",
            "trade thesis weakens but hard invalidation has not fired",
            "volatility expands against the position while position is not yet invalid",
        ],
        "tp1_close_ratio": 0.5,
        "after_reduce": "protect the remaining core position with a net breakeven or ATR-based stop.",
        "never_reduce_below_core_without_exit_reason": True,
    }


def _add_logic() -> dict[str, Any]:
    return {
        "add_if": [
            "disabled in current system until paper data proves scale-in improves expectancy",
            "only after unrealized profit protects original risk",
            "only if fresh scan evidence confirms the same thesis",
        ],
        "max_adds": 0,
        "reason": "No scale-in is allowed until MFE/MAE and drawdown data show it is safer than holding the core position.",
    }


def _exit_logic(plan: StrategyPlan) -> dict[str, Any]:
    return {
        "core_exit_only_if": [
            f"hard stop is touched: {plan.stop_loss}",
            "trade thesis is invalidated by structure plus flow, not just a minor reverse signal",
            "risk limit is hit",
            "time stop fires with no favorable development",
            "TP2 is reached",
        ],
        "minor_reverse_signal_action": "mark defensive or tighten risk first; do not immediately close the core position unless thesis invalidation also occurs.",
        "final_targets": {
            "tp1": plan.tp1,
            "tp2": plan.tp2,
        },
    }


def _time_stop(plan: StrategyPlan) -> dict[str, Any]:
    return {
        "seconds": int(plan.expire_after_seconds or 180),
        "rule": "If the position does not develop before the time stop and remains non-profitable, reduce risk or exit; do not hold dead trades indefinitely.",
        "requires_no_favorable_development": True,
    }


def _entry_conditions(item: RadarItem, side: str) -> list[str]:
    if side == "LONG":
        taker = "taker_buy_ratio >= 0.55"
        depth = "depth_imbalance >= 0.08"
        timeframe = "5m/15m/1h changes stay non-negative or improving"
    elif side == "SHORT":
        taker = "taker_sell_ratio >= 0.55"
        depth = "depth_imbalance <= -0.08"
        timeframe = "5m/15m/1h changes stay non-positive or weakening"
    else:
        taker = "directional taker flow required"
        depth = "directional depth required"
        timeframe = "timeframe alignment required"
    return [
        f"radar direction remains {side}",
        f"fund_confirm_count >= {min(3, item.fund_confirm_total)} for formal trades; paper probe may collect lower-confirm samples",
        taker,
        depth,
        timeframe,
        "fake_breakout_risk is not HIGH",
    ]


def _avoid_conditions(item: RadarItem, side: str) -> list[str]:
    avoid = [
        "fake_breakout_risk == HIGH",
        "wick_ratio is elevated and price is chasing after a spike",
        "round-trip cost consumes too much of stop distance",
        "similar learned attribution bucket has negative PnL and low profit factor",
    ]
    if side == "LONG":
        avoid.append("taker_buy_ratio weakens below 0.50 or depth turns negative")
    elif side == "SHORT":
        avoid.append("taker_sell_ratio weakens below 0.50 or depth turns positive")
    return avoid


def _invalidation(plan: StrategyPlan) -> dict[str, Any]:
    return {
        "hard_stop": plan.stop_loss,
        "signal_failure": [
            "radar direction flips against position",
            "funding/taker/depth alignment fails after entry",
            "price leaves entry premise before fill",
        ],
        "time_failure_seconds": int(plan.expire_after_seconds or 180),
    }


def _position_management(plan: StrategyPlan) -> dict[str, Any]:
    return {
        "entry_zone": [plan.entry_zone_low, plan.entry_zone_high],
        "tp1": plan.tp1,
        "tp2": plan.tp2,
        "tp1_close_ratio": 0.5,
        "after_tp1": "move stop toward breakeven and trail only if signal remains aligned",
        "max_adds": 0,
    }


def _cost_constraints(plan: StrategyPlan) -> dict[str, Any]:
    entry = float(plan.ideal_entry_price or 0.0)
    stop_pct = stop_distance_pct(entry, plan.stop_loss) if entry > 0 else 0.0
    return {
        "round_trip_cost_pct": round(round_trip_cost_pct(), 6),
        "stop_distance_pct": round(stop_pct, 6),
        "min_net_profit_usdt": float(settings.trade_min_net_profit_usdt),
        "min_profit_cost_ratio": float(settings.trade_min_profit_cost_ratio),
        "tp2_min_r": float(settings.strategy_min_tp2_r),
    }


def _learning_tags(item: RadarItem, side: str, confirmations: int, cyqnt_report: dict[str, Any]) -> dict[str, Any]:
    return {
        "symbol": item.symbol,
        "side": side,
        "score_bucket": int(float(item.score or 0.0) // 10 * 10),
        "fund_confirm": item.fund_confirm_count,
        "fake_breakout_risk": item.fake_breakout_risk,
        "direction_confirmations": confirmations,
        "timeframe_aligned": item.change_5m * (1 if side == "LONG" else -1) > 0 and item.change_15m * (1 if side == "LONG" else -1) > 0,
        "taker_aligned": item.taker_buy_ratio >= 0.55 if side == "LONG" else item.taker_sell_ratio >= 0.55,
        "depth_aligned": item.depth_imbalance >= 0.08 if side == "LONG" else item.depth_imbalance <= -0.08,
        "wick_high": item.wick_ratio > 0.55,
        "volume_spike": item.volume_spike,
        "cyqnt_feature_score": cyqnt_report.get("feature_score"),
        "cyqnt_selection_score": cyqnt_report.get("selection_score"),
        "cyqnt_estimated_win_rate": cyqnt_report.get("estimated_win_rate"),
        "main_positive_features": cyqnt_report.get("positive_factors") or [],
        "main_failure_risks": cyqnt_report.get("failure_risks") or [],
        "cyqnt_reasons": cyqnt_report.get("reasons") or [],
    }


def _allowed_stages(paper_probe: bool) -> dict[str, bool]:
    return {
        "paper_probe": True,
        "paper_formal": not paper_probe,
        "shadow_live": True,
        "live_test_order": False,
        "micro_live": False,
        "live": False,
    }


def _research_review(item: RadarItem, plan: StrategyPlan, side: str, paper_probe: bool, cyqnt_report: dict[str, Any]) -> dict[str, Any]:
    stage = "paper probe sample" if paper_probe else "candidate strategy"
    return {
        "role_a_researcher": (
            f"{stage}: test whether {item.symbol} {side} radar continuation has positive expectancy after cost. "
            f"Local cyqnt evidence: feature_score={cyqnt_report.get('feature_score')}, "
            f"estimated_win_rate={cyqnt_report.get('estimated_win_rate')}, "
            f"selection_score={cyqnt_report.get('selection_score')}."
        ),
        "role_b_risk_officer": (
            "Do not trust the signal until it survives cost, slippage, sample-out validation, drawdown, "
            "learned loss-bucket checks, and cyqnt negative contribution review. Reject if it only looks good as an indicator direction."
        ),
        "must_report": True,
        "report_template": "trading_lab/strategy_template.md",
        "decision_bias": "reject_or_wait_when_market_hypothesis_is_unclear",
    }
