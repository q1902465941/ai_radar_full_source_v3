from __future__ import annotations

from collections import Counter
from typing import Any

from backend.config import settings
from backend.learning.strategy_filter import direction_confirmations
from backend.learning.strategy_registry import strategy_registry
from backend.models import ClosedPosition, ExecutionPlan, Position, RadarItem, StrategyPlan, new_id, now_ms
from backend.positions.position_registry import position_registry
from backend.radar.candidate_feature_enhancer import candidate_feature_enhancer
from backend.storage.db import db


AI_PLAN_PROVIDERS = {"deepseek", "codex_cli", "openai", "rule"}
NON_LEARNING_CLOSE_REASONS = {"RESTORED_STALE_RECONCILE", "PRICE_SOURCE_STALE_RECONCILE"}


class AIStrategyFeedback:
    def quality_summary(self, limit: int = 100) -> dict[str, Any]:
        strategies = self._ai_strategies(limit)
        samples = self._ai_forward_samples(strategies)
        observations = self.decision_observations(limit=limit)
        metrics = self._sample_metrics(samples)
        return {
            "tracked_strategies": len(strategies),
            "closed_samples": metrics["trades"],
            "wins": metrics["wins"],
            "losses": metrics["losses"],
            "win_rate": metrics["win_rate"],
            "profit_factor": metrics["profit_factor"],
            "pnl": metrics["pnl"],
            "eligible_strategy_count": sum(1 for strategy in strategies if (strategy.get("metrics") or {}).get("eligible")),
            "rejected_strategy_count": sum(1 for strategy in strategies if strategy.get("status") == "REJECTED"),
            "worst_symbol_side": self._bucket_summary(samples, ("symbol", "side"), positive=False)[:6],
            "worst_strategy_kinds": self._bucket_summary(samples, ("strategy_kind", "side"), positive=False)[:6],
            "best_strategy_kinds": self._bucket_summary(samples, ("strategy_kind", "side"), positive=True)[:6],
            "trading_lessons": self._trading_lessons(samples),
            "decision_observations": self._observation_lessons(observations),
            "recent_samples": samples[:8],
            "instruction": (
                "Use this as AI strategy feedback. Losing symbol/side or strategy_kind buckets should not be repeated "
                "unless the new plan states a material difference in signal, invalidation, execution, and hold logic."
            ),
        }

    def evaluate_candidate(self, item: RadarItem, plan: StrategyPlan | None = None, limit: int = 100) -> dict[str, Any]:
        strategies = self._ai_strategies(limit)
        samples = self._ai_forward_samples(strategies)
        side = plan.side if plan and plan.side in {"LONG", "SHORT"} else item.direction
        contract = plan.raw.get("strategy_contract") if plan and isinstance(plan.raw, dict) else {}
        strategy_kind = str((contract or {}).get("strategy_kind") or "")
        feature = candidate_feature_enhancer.evaluate(item).asdict()
        current_risks = _string_set(
            feature.get("failure_risks")
            or feature.get("negative_factors")
            or feature.get("main_failure_risks")
            or []
        )
        current_positives = _string_set(feature.get("positive_factors") or feature.get("main_positive_features") or [])

        exact = [sample for sample in samples if sample["symbol"] == item.symbol and sample["side"] == side]
        same_kind = [
            sample
            for sample in samples
            if strategy_kind and sample["side"] == side and sample["strategy_kind"] == strategy_kind
        ]
        same_side = [sample for sample in samples if sample["side"] == side]
        feature_overlap = [
            sample
            for sample in samples
            if sample["side"] == side
            and (
                current_risks.intersection(_string_set(sample.get("failure_risks") or []))
                or current_positives.intersection(_string_set(sample.get("positive_features") or []))
            )
        ]
        matches = [
            self._match_report("exact_symbol_side", exact),
            self._match_report("same_strategy_kind_side", same_kind),
            self._match_report("same_side", same_side),
            self._match_report("feature_overlap_side", feature_overlap),
        ]
        matches = [match for match in matches if match["samples"] > 0]
        hard_avoid = [match for match in matches if match["severity"] == "AVOID"]
        review = [match for match in matches if match["severity"] == "REVIEW"]
        generation_gate = self._generation_gate(
            hard_avoid=hard_avoid,
            review=review,
            feature=feature,
        )
        return {
            "enabled": True,
            "symbol": item.symbol,
            "side": side,
            "strategy_kind": strategy_kind,
            "closed_ai_samples": len(samples),
            "quality_bias": "AVOID_REPEAT" if hard_avoid else ("REVIEW" if review else "NEUTRAL"),
            "generation_gate": generation_gate,
            "matches": matches[:6],
            "avoid_repeating": hard_avoid[:4],
            "review_lessons": review[:4],
            "candidate_learning_delta": self._candidate_learning_delta(feature, samples, side),
            "candidate_feature_snapshot": {
                "feature_score": feature.get("feature_score"),
                "selection_score": feature.get("selection_score"),
                "estimated_win_rate": feature.get("estimated_win_rate"),
                "positive_factors": sorted(current_positives)[:6],
                "failure_risks": sorted(current_risks)[:6],
            },
            "instruction": (
                "avoid_repeating is a hard no-repeat constraint. review_lessons are coaching notes, not a veto. "
                "For paper-only learning, OPEN is acceptable when current evidence is materially stronger and the plan "
                "explicitly changes entry_conditions, avoid_conditions, invalidation, cost constraints, and position lifecycle "
                "versus the losing bucket."
            ),
        }

    def compact_context(self, item: RadarItem, plan: StrategyPlan | None = None) -> dict[str, Any]:
        evaluation = self.evaluate_candidate(item, plan, limit=100)
        summary = self.quality_summary(limit=100)
        observations = self.decision_observations(limit=100)
        return {
            "summary": {
                "tracked_strategies": summary["tracked_strategies"],
                "closed_samples": summary["closed_samples"],
                "win_rate": summary["win_rate"],
                "profit_factor": summary["profit_factor"],
                "pnl": summary["pnl"],
                "worst_symbol_side": summary["worst_symbol_side"][:3],
                "worst_strategy_kinds": summary["worst_strategy_kinds"][:3],
                "trading_lessons": summary["trading_lessons"],
            },
            "candidate_feedback": {
                "quality_bias": evaluation["quality_bias"],
                "generation_gate": evaluation["generation_gate"],
                "matches": evaluation["matches"][:4],
                "avoid_repeating": evaluation["avoid_repeating"][:3],
                "review_lessons": evaluation["review_lessons"][:3],
                "candidate_learning_delta": evaluation["candidate_learning_delta"],
                "candidate_feature_snapshot": evaluation["candidate_feature_snapshot"],
            },
            "decision_observations": self._observation_lessons(observations, item=item),
            "instruction": evaluation["instruction"],
        }

    def record_open(
        self,
        *,
        plan: StrategyPlan,
        item: RadarItem,
        exec_plan: ExecutionPlan,
        position: Position,
        candidate_source: str,
        paper_validation: bool,
        selected_strategy_id: str = "",
    ) -> dict[str, Any]:
        provider = self._provider(plan)
        if provider not in AI_PLAN_PROVIDERS:
            return {"recorded": False, "reason": "not_ai_generated"}

        existing = strategy_registry.get(plan.strategy_id) or {}
        forward = dict(existing.get("forward") or {})
        open_ids = list(forward.get("open_position_ids") or [])
        if position.position_id not in open_ids:
            open_ids.append(position.position_id)
        forward["opened"] = max(int(forward.get("opened") or 0), len(open_ids))
        forward["open_position_ids"] = open_ids[-20:]
        forward.setdefault("closed_position_ids", [])
        forward.setdefault("samples", [])
        forward["last_opened_at"] = int(position.open_time or now_ms())

        record = {
            **existing,
            "strategy_id": plan.strategy_id,
            "name": existing.get("name") or self._name(provider, item, plan),
            "source": f"ai_generated_{provider}",
            "status": existing.get("status") or "WATCH",
            "version": existing.get("version") or 1,
            "created_at": existing.get("created_at") or now_ms(),
            "filters": existing.get("filters") or self._filters_from_item(item, plan),
            "provider": provider,
            "model": plan.raw.get("model", ""),
            "candidate_source": candidate_source,
            "paper_validation": bool(paper_validation),
            "selected_strategy_id": selected_strategy_id,
            "last_plan": self._plan_snapshot(plan),
            "last_signal": self._signal_snapshot(item),
            "last_cyqnt_feature": self._feature_snapshot(item),
            "last_execution": self._execution_snapshot(exec_plan),
            "strategy_contract": plan.raw.get("strategy_contract") or {},
            "forward": forward,
            "metrics": self._metrics_from_forward(forward),
            "rationale": plan.reason,
        }
        saved = strategy_registry.save(record)
        return {
            "recorded": True,
            "strategy_id": saved["strategy_id"],
            "status": saved.get("status", "WATCH"),
            "closed": saved.get("metrics", {}).get("trades", 0),
            "eligible": bool(saved.get("metrics", {}).get("eligible")),
        }

    def record_observation(
        self,
        *,
        item: RadarItem | None,
        decision: str,
        reason: str,
        candidate_source: str,
        stage: str,
        plan: StrategyPlan | None = None,
        paper_validation: bool = False,
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        raw = plan.raw if plan and isinstance(plan.raw, dict) else {}
        provider = self._provider(plan) if plan else str((context or {}).get("provider") or "local_gate")
        feature = self._feature_snapshot(item) if item is not None else {}
        payload = {
            "observation_id": new_id("aiobs"),
            "created_at": now_ms(),
            "sample_type": "decision_observation_not_trade_outcome",
            "symbol": item.symbol if item is not None else "",
            "side": (
                plan.side
                if plan and plan.side in {"LONG", "SHORT"}
                else (item.direction if item is not None else "")
            ),
            "decision": str(decision or ""),
            "reason": str(reason or ""),
            "stage": str(stage or ""),
            "candidate_source": str(candidate_source or ""),
            "provider": provider,
            "model": raw.get("model", ""),
            "plan_action": plan.action if plan else "",
            "wait_type": plan.wait_type if plan else "",
            "paper_validation": bool(paper_validation),
            "radar": self._signal_snapshot(item) if item is not None else {},
            "cyqnt_feature_enhancement": feature,
            "plan": self._plan_snapshot(plan) if plan else {},
            "context": context or {},
            "learning_role": (
                "Teaches why current-market AI/gate decisions did not open. "
                "This is not a PnL sample and cannot approve live trading."
            ),
        }
        db.save_ai_observation(payload)
        return {
            "recorded": True,
            "observation_id": payload["observation_id"],
            "sample_type": payload["sample_type"],
            "decision": payload["decision"],
            "reason": payload["reason"],
        }

    def record_gate_observation(
        self,
        *,
        decision: str,
        reason: str,
        candidate_source: str,
        diagnostics: dict[str, Any] | None = None,
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        diagnostics = diagnostics or {}
        gate = diagnostics.get("gate") if isinstance(diagnostics.get("gate"), dict) else {}
        counts = diagnostics.get("counts") if isinstance(diagnostics.get("counts"), dict) else {}
        payload = {
            "observation_id": new_id("aiobs"),
            "created_at": now_ms(),
            "sample_type": "candidate_gate_observation_not_trade_outcome",
            "symbol": "",
            "side": "",
            "decision": str(decision or ""),
            "reason": str(reason or ""),
            "stage": "candidate_selection",
            "candidate_source": str(candidate_source or ""),
            "provider": "local_gate",
            "model": "",
            "plan_action": "",
            "wait_type": "",
            "paper_validation": False,
            "radar": {},
            "cyqnt_feature_enhancement": {},
            "plan": {},
            "context": {
                **(context or {}),
                "gate": gate,
                "counts": counts,
                "rejection_counts_top12": diagnostics.get("rejection_counts_top12", {}),
                "examples_top12": list(diagnostics.get("examples_top12") or [])[:5],
            },
            "learning_role": (
                "Teaches why candidate selection produced no tradeable item. "
                "This is not a PnL sample and cannot approve live trading."
            ),
        }
        db.save_ai_observation(payload)
        return {
            "recorded": True,
            "observation_id": payload["observation_id"],
            "sample_type": payload["sample_type"],
            "decision": payload["decision"],
            "reason": payload["reason"],
        }

    def decision_observations(self, limit: int = 100) -> list[dict[str, Any]]:
        try:
            rows = db.list_ai_observations(limit=max(1, int(limit)))
        except Exception:
            return []
        return rows

    def record_close(self, closed: ClosedPosition) -> dict[str, Any]:
        if str(closed.close_reason or "") in NON_LEARNING_CLOSE_REASONS:
            return self._record_non_learning_close(closed)
        strategy = strategy_registry.get(closed.strategy_id)
        if not strategy or not str(strategy.get("source") or "").startswith("ai_generated_"):
            return {"recorded": False, "reason": "strategy_not_tracked"}

        forward = dict(strategy.get("forward") or {})
        closed_ids = list(forward.get("closed_position_ids") or [])
        if closed.position_id in closed_ids:
            return {"recorded": False, "reason": "already_recorded"}

        closed_ids.append(closed.position_id)
        samples = list(forward.get("samples") or [])
        samples.append(self._closed_sample(closed))
        samples = samples[-50:]
        open_ids = [pid for pid in list(forward.get("open_position_ids") or []) if pid != closed.position_id]

        forward["closed_position_ids"] = closed_ids[-50:]
        forward["open_position_ids"] = open_ids[-20:]
        forward["closed"] = len(closed_ids)
        forward["opened"] = max(int(forward.get("opened") or 0), len(closed_ids) + len(open_ids))
        forward["samples"] = samples
        forward["last_closed_at"] = int(closed.close_time or now_ms())

        metrics = self._metrics_from_forward(forward)
        strategy["forward"] = forward
        strategy["metrics"] = metrics
        strategy["status"] = self._status_from_metrics(metrics)
        saved = strategy_registry.save(strategy)
        return {
            "recorded": True,
            "strategy_id": saved["strategy_id"],
            "status": saved["status"],
            "closed": metrics["trades"],
            "win_rate": metrics["win_rate"],
            "profit_factor": metrics["profit_factor"],
            "pnl": metrics["pnl"],
            "eligible": metrics["eligible"],
        }

    def _record_non_learning_close(self, closed: ClosedPosition) -> dict[str, Any]:
        strategy = strategy_registry.get(closed.strategy_id)
        if not strategy or not str(strategy.get("source") or "").startswith("ai_generated_"):
            return {"recorded": False, "reason": "non_learning_close_reason"}

        forward = dict(strategy.get("forward") or {})
        open_ids = [pid for pid in list(forward.get("open_position_ids") or []) if pid != closed.position_id]
        closed_ids = list(forward.get("closed_position_ids") or [])
        non_learning_ids = list(forward.get("non_learning_position_ids") or [])
        if closed.position_id not in non_learning_ids:
            non_learning_ids.append(closed.position_id)
        non_learning_reasons = dict(forward.get("non_learning_reasons") or {})
        non_learning_reasons[closed.position_id] = str(closed.close_reason or "")

        forward["open_position_ids"] = open_ids[-20:]
        forward["closed_position_ids"] = closed_ids[-50:]
        forward["non_learning_position_ids"] = non_learning_ids[-50:]
        forward["non_learning_reasons"] = non_learning_reasons
        forward["closed"] = len(closed_ids)
        forward["opened"] = len(closed_ids) + len(open_ids)
        forward["last_non_learning_closed_at"] = int(closed.close_time or now_ms())

        metrics = self._metrics_from_forward(forward)
        strategy["forward"] = forward
        strategy["metrics"] = metrics
        if str(closed.close_reason or "") == "PRICE_SOURCE_STALE_RECONCILE" and metrics["trades"] == 0:
            strategy["status"] = "QUARANTINED"
        else:
            strategy["status"] = self._status_from_metrics(metrics)
        saved = strategy_registry.save(strategy)
        return {
            "recorded": False,
            "reason": "non_learning_close_reason",
            "strategy_id": saved["strategy_id"],
            "status": saved.get("status"),
            "open": metrics["open"],
            "trades": metrics["trades"],
        }

    def _provider(self, plan: StrategyPlan) -> str:
        raw = plan.raw if isinstance(plan.raw, dict) else {}
        provider = str(raw.get("provider") or "").strip().lower()
        if provider.startswith("deepseek"):
            return "deepseek"
        if provider.startswith("codex"):
            return "codex_cli"
        if provider.startswith("openai"):
            return "openai"
        return provider

    def _name(self, provider: str, item: RadarItem, plan: StrategyPlan) -> str:
        contract = plan.raw.get("strategy_contract") if isinstance(plan.raw, dict) else {}
        kind = str((contract or {}).get("strategy_kind") or "radar_plan")
        return f"{provider}_{plan.side.lower()}_{kind}_{item.symbol}"

    def _filters_from_item(self, item: RadarItem, plan: StrategyPlan) -> dict[str, Any]:
        side = plan.side if plan.side in {"LONG", "SHORT"} else item.direction
        row = item.asdict()
        return {
            "min_score": round(max(0.0, float(item.score or 0.0) * 0.92), 2),
            "min_fund_confirm": int(item.fund_confirm_count or 0),
            "allowed_fake_risks": ["LOW"],
            "min_direction_confirmations": max(3, direction_confirmations(row, side)),
            "min_volume_spike": round(max(0.0, float(item.volume_spike or 0.0) * 0.8), 3),
            "max_wick_ratio": round(min(0.55, max(float(item.wick_ratio or 0.0), float(settings.paper_probe_max_wick_ratio or 0.55))), 3),
            "require_oi_positive": float(item.oi_change or 0.0) >= 0,
            "require_timeframe_alignment": self._timeframes_aligned(item, side),
            "require_taker_alignment": self._taker_aligned(item, side),
            "require_depth_alignment": self._depth_aligned(item, side),
            "require_sm_delta_alignment": self._sm_aligned(item, side),
            "allowed_sides": [side] if side in {"LONG", "SHORT"} else ["LONG", "SHORT"],
            "cyqnt_reference": {
                "feature_score": feature.get("feature_score"),
                "selection_score": feature.get("selection_score"),
                "estimated_win_rate": feature.get("estimated_win_rate"),
                "role": "reference only; strategy_matches uses structural filters while cyqnt evidence is tracked for attribution",
            } if (feature := candidate_feature_enhancer.evaluate(item).asdict()) else {},
        }

    def _plan_snapshot(self, plan: StrategyPlan) -> dict[str, Any]:
        if plan is None:
            return {}
        return {
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
            "reason": plan.reason,
            "wait_type": plan.wait_type,
            "expire_after_seconds": plan.expire_after_seconds,
        }

    def _signal_snapshot(self, item: RadarItem) -> dict[str, Any]:
        if item is None:
            return {}
        return {
            "symbol": item.symbol,
            "rank": item.rank,
            "side": item.direction,
            "score": item.score,
            "fund_confirm": f"{item.fund_confirm_count}/{item.fund_confirm_total}",
            "fake_breakout_risk": item.fake_breakout_risk,
            "direction_confirmations": direction_confirmations(item.asdict(), item.direction),
            "volume_spike": item.volume_spike,
            "wick_ratio": item.wick_ratio,
            "atr_pct": item.atr_pct,
            "taker_buy_ratio": item.taker_buy_ratio,
            "taker_sell_ratio": item.taker_sell_ratio,
            "depth_imbalance": item.depth_imbalance,
            "sm_delta": item.sm_delta,
            "cyqnt_feature_enhancement": self._feature_snapshot(item),
        }

    def _feature_snapshot(self, item: RadarItem) -> dict[str, Any]:
        if item is None:
            return {}
        return candidate_feature_enhancer.evaluate(item).asdict()

    def _execution_snapshot(self, exec_plan: ExecutionPlan) -> dict[str, Any]:
        return {
            "decision": exec_plan.decision,
            "mode": exec_plan.mode,
            "margin": exec_plan.dynamic_margin,
            "notional": exec_plan.notional,
            "leverage": exec_plan.dynamic_leverage,
            "risk_usdt": exec_plan.risk_usdt,
            "risk_pct": exec_plan.risk_pct,
            "management_mode": exec_plan.management_mode,
            "reason": exec_plan.reason,
        }

    def _closed_sample(self, closed: ClosedPosition) -> dict[str, Any]:
        return {
            "position_id": closed.position_id,
            "symbol": closed.symbol,
            "side": closed.side,
            "pnl": round(float(closed.pnl or 0.0), 4),
            "roi": round(float(closed.roi or 0.0), 4),
            "close_reason": closed.close_reason,
            "entry_price": closed.entry_price,
            "exit_price": closed.exit_price,
            "notional": closed.notional,
            "fee": closed.fee,
            "risk_usdt": closed.risk_usdt,
            "risk_pct": closed.risk_pct,
            "mfe": closed.mfe,
            "mae": closed.mae,
            "mfe_r": closed.mfe_r,
            "mae_r": closed.mae_r,
            "hold_time_ms": closed.hold_time_ms,
            "exit_decision": closed.exit_decision,
            "last_ai_review": closed.last_ai_review,
            "close_time": closed.close_time,
            "cyqnt_feature_enhancement": (
                closed.strategy_contract.get("cyqnt_feature_enhancement")
                if isinstance(closed.strategy_contract, dict)
                else {}
            ),
        }

    def _metrics_from_forward(self, forward: dict[str, Any]) -> dict[str, Any]:
        samples = list(forward.get("samples") or [])
        pnls = [float(sample.get("pnl") or 0.0) for sample in samples]
        wins = [pnl for pnl in pnls if pnl > 0]
        losses = [pnl for pnl in pnls if pnl < 0]
        gross_profit = sum(wins)
        gross_loss = abs(sum(losses))
        trades = len(samples)
        win_rate = len(wins) / max(1, len(wins) + len(losses))
        profit_factor = gross_profit / gross_loss if gross_loss > 0 else (999.0 if gross_profit > 0 else 0.0)
        pnl = sum(pnls)
        min_trades = max(1, int(settings.evolve_min_holdout_trades or 4))
        eligible = (
            trades >= min_trades
            and win_rate >= float(settings.evolve_min_holdout_win_rate or 0.5)
            and profit_factor >= float(settings.evolve_min_profit_factor or 1.15)
            and pnl > float(settings.evolve_min_net_pnl or 0.0)
        )
        return {
            "trades": trades,
            "opened": int(forward.get("opened") or 0),
            "open": len(forward.get("open_position_ids") or []),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": round(win_rate, 4),
            "pnl": round(pnl, 4),
            "avg_pnl": round(pnl / trades, 6) if trades else 0.0,
            "profit_factor": round(profit_factor, 4),
            "eligible": eligible,
            "eligible_reasons": self._eligibility_reasons(trades, win_rate, profit_factor, pnl),
        }

    def _eligibility_reasons(self, trades: int, win_rate: float, profit_factor: float, pnl: float) -> list[str]:
        reasons: list[str] = []
        if trades < max(1, int(settings.evolve_min_holdout_trades or 4)):
            reasons.append("forward_samples_low")
        if win_rate < float(settings.evolve_min_holdout_win_rate or 0.5):
            reasons.append("forward_win_rate_low")
        if profit_factor < float(settings.evolve_min_profit_factor or 1.15):
            reasons.append("forward_profit_factor_low")
        if pnl <= float(settings.evolve_min_net_pnl or 0.0):
            reasons.append("forward_pnl_low")
        return reasons

    def _status_from_metrics(self, metrics: dict[str, Any]) -> str:
        if metrics.get("eligible"):
            return "PASS"
        min_trades = max(1, int(settings.evolve_min_holdout_trades or 4))
        if int(metrics.get("trades") or 0) >= min_trades and float(metrics.get("pnl") or 0.0) <= 0:
            return "REJECTED"
        return "WATCH"

    def _ai_strategies(self, limit: int) -> list[dict[str, Any]]:
        try:
            strategies = strategy_registry.list(limit=max(1, int(limit)))
        except Exception:
            return []
        return [
            strategy
            for strategy in strategies
            if str(strategy.get("source") or "").startswith("ai_generated_")
            and strategy.get("status") != "QUARANTINED"
        ]

    def _ai_forward_samples(self, strategies: list[dict[str, Any]]) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        strategy_by_id = {str(strategy.get("strategy_id") or ""): strategy for strategy in strategies}
        seen_position_ids: set[str] = set()
        for strategy in strategies:
            contract = strategy.get("strategy_contract") if isinstance(strategy.get("strategy_contract"), dict) else {}
            tags = contract.get("learning_tags") if isinstance(contract.get("learning_tags"), dict) else {}
            strategy_kind = str(contract.get("strategy_kind") or "unknown")
            last_signal = strategy.get("last_signal") if isinstance(strategy.get("last_signal"), dict) else {}
            samples = ((strategy.get("forward") or {}).get("samples") or []) if isinstance(strategy.get("forward"), dict) else []
            for sample in samples:
                if not isinstance(sample, dict):
                    continue
                position_id = str(sample.get("position_id") or "")
                if position_id:
                    seen_position_ids.add(position_id)
                symbol = str(sample.get("symbol") or last_signal.get("symbol") or tags.get("symbol") or "").upper()
                side = str(sample.get("side") or tags.get("side") or "").upper()
                if side not in {"LONG", "SHORT"}:
                    continue
                rows.append(
                    {
                        "strategy_id": strategy.get("strategy_id"),
                        "position_id": position_id,
                        "sample_source": "strategy_forward",
                        "status": strategy.get("status"),
                        "provider": strategy.get("provider"),
                        "symbol": symbol,
                        "side": side,
                        "strategy_kind": strategy_kind,
                        "pnl": round(_float(sample.get("pnl")), 4),
                        "roi": round(_float(sample.get("roi")), 4),
                        "close_reason": sample.get("close_reason"),
                        "mfe_r": round(_float(sample.get("mfe_r")), 4),
                        "mae_r": round(_float(sample.get("mae_r")), 4),
                        "positive_features": self._sample_positive_features(tags),
                        "failure_risks": self._sample_failure_risks(tags),
                        "close_time": int(sample.get("close_time") or 0),
                    }
                )
        rows.extend(self._closed_position_samples(strategy_by_id, seen_position_ids))
        return sorted(rows, key=lambda row: int(row.get("close_time") or 0), reverse=True)

    def _closed_position_samples(
        self,
        strategy_by_id: dict[str, dict[str, Any]],
        seen_position_ids: set[str],
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        try:
            closed_rows = position_registry.list_closed(limit=500)
        except Exception:
            return rows
        for row in closed_rows:
            if not isinstance(row, dict):
                continue
            if str(row.get("close_reason") or "") in NON_LEARNING_CLOSE_REASONS:
                continue
            position_id = str(row.get("position_id") or "")
            if position_id and position_id in seen_position_ids:
                continue
            strategy_id = str(row.get("strategy_id") or "")
            strategy = strategy_by_id.get(strategy_id)
            contract = row.get("strategy_contract") if isinstance(row.get("strategy_contract"), dict) else {}
            if not contract:
                contract = strategy.get("strategy_contract") if strategy and isinstance(strategy.get("strategy_contract"), dict) else {}
            if strategy is None and not contract.get("strategy_kind"):
                continue
            tags = contract.get("learning_tags") if isinstance(contract.get("learning_tags"), dict) else {}
            symbol = str(row.get("symbol") or tags.get("symbol") or "").upper()
            side = str(row.get("side") or tags.get("side") or "").upper()
            if side not in {"LONG", "SHORT"}:
                continue
            rows.append(
                {
                    "strategy_id": strategy_id,
                    "position_id": position_id,
                    "sample_source": "closed_position" if strategy else "closed_position_contract",
                    "status": strategy.get("status") if strategy else "CONTRACT_ONLY",
                    "provider": strategy.get("provider") if strategy else "contract_closed_position",
                    "symbol": symbol,
                    "side": side,
                    "strategy_kind": str(contract.get("strategy_kind") or "unknown"),
                    "pnl": round(_float(row.get("pnl")), 4),
                    "roi": round(_float(row.get("roi")), 4),
                    "close_reason": row.get("close_reason"),
                    "mfe_r": round(_float(row.get("mfe_r")), 4),
                    "mae_r": round(_float(row.get("mae_r")), 4),
                    "positive_features": self._sample_positive_features(tags),
                    "failure_risks": self._sample_failure_risks(tags),
                    "close_time": int(row.get("close_time") or 0),
                }
            )
        return rows

    def _sample_positive_features(self, tags: dict[str, Any]) -> list[str]:
        explicit = _string_set(tags.get("main_positive_features") or tags.get("positive_factors") or [])
        if explicit:
            return sorted(explicit)
        out: set[str] = set()
        if _float(tags.get("cyqnt_feature_score")) >= 68:
            out.add("feature_score_strong")
        if _float(tags.get("cyqnt_estimated_win_rate")) >= float(settings.strategy_min_paper_win_rate or 0.56):
            out.add("estimated_win_rate_above_paper_gate")
        if int(_float(tags.get("fund_confirm"))) >= 3:
            out.add("fund_confirm_full")
        elif int(_float(tags.get("fund_confirm"))) >= 2:
            out.add("fund_confirm_partial")
        if tags.get("fake_breakout_risk") == "LOW":
            out.add("fake_breakout_risk_low")
        for key in ("timeframe_aligned", "taker_aligned", "depth_aligned"):
            if tags.get(key) is True:
                out.add(key)
        for reason in _string_set(tags.get("cyqnt_reasons") or []):
            if reason.endswith("_positive") or reason == "feature_score_strong":
                out.add(reason)
        return sorted(out)

    def _sample_failure_risks(self, tags: dict[str, Any]) -> list[str]:
        explicit = _string_set(tags.get("main_failure_risks") or tags.get("failure_risks") or [])
        if explicit:
            return sorted(explicit)
        out: set[str] = set()
        if _float(tags.get("cyqnt_feature_score")) < 48:
            out.add("feature_score_weak")
        if _float(tags.get("cyqnt_estimated_win_rate")) < float(settings.strategy_min_paper_win_rate or 0.56):
            out.add("estimated_win_rate_below_paper_gate")
        if int(_float(tags.get("fund_confirm"))) < 2:
            out.add("fund_confirm_too_low_for_training")
        if tags.get("fake_breakout_risk") == "HIGH":
            out.add("fake_breakout_risk_high")
        elif tags.get("fake_breakout_risk") == "MEDIUM":
            out.add("fake_breakout_risk_medium")
        if tags.get("wick_high") is True:
            out.add("wick_high")
        for key in ("timeframe_aligned", "taker_aligned", "depth_aligned"):
            if tags.get(key) is False:
                out.add(f"{key}_false")
        for reason in _string_set(tags.get("cyqnt_reasons") or []):
            if reason.endswith("_negative") or reason == "feature_score_weak":
                out.add(reason)
        return sorted(out)

    def _sample_metrics(self, samples: list[dict[str, Any]]) -> dict[str, Any]:
        pnls = [_float(sample.get("pnl")) for sample in samples]
        wins = [pnl for pnl in pnls if pnl > 0]
        losses = [pnl for pnl in pnls if pnl < 0]
        gross_profit = sum(wins)
        gross_loss = abs(sum(losses))
        trades = len(pnls)
        profit_factor = gross_profit / gross_loss if gross_loss > 0 else (999.0 if gross_profit > 0 else 0.0)
        return {
            "trades": trades,
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": round(len(wins) / max(1, len(wins) + len(losses)), 4),
            "profit_factor": round(profit_factor, 4),
            "pnl": round(sum(pnls), 4),
            "avg_pnl": round(sum(pnls) / trades, 6) if trades else 0.0,
        }

    def _bucket_summary(
        self,
        samples: list[dict[str, Any]],
        keys: tuple[str, ...],
        *,
        positive: bool,
    ) -> list[dict[str, Any]]:
        buckets: dict[tuple[Any, ...], list[dict[str, Any]]] = {}
        for sample in samples:
            key = tuple(sample.get(part) or "" for part in keys)
            if not all(key):
                continue
            buckets.setdefault(key, []).append(sample)
        rows: list[dict[str, Any]] = []
        for key, bucket in buckets.items():
            metrics = self._sample_metrics(bucket)
            if positive and metrics["pnl"] <= 0:
                continue
            if not positive and metrics["pnl"] >= 0:
                continue
            rows.append(
                {
                    "bucket": dict(zip(keys, key)),
                    "samples": metrics["trades"],
                    "win_rate": metrics["win_rate"],
                    "profit_factor": metrics["profit_factor"],
                    "pnl": metrics["pnl"],
                    "avg_pnl": metrics["avg_pnl"],
                }
            )
        if positive:
            rows.sort(key=lambda row: (row["profit_factor"], row["win_rate"], row["pnl"]), reverse=True)
        else:
            rows.sort(key=lambda row: (row["pnl"], row["profit_factor"], row["win_rate"]))
        return rows

    def _trading_lessons(self, samples: list[dict[str, Any]]) -> dict[str, Any]:
        winners = [sample for sample in samples if _float(sample.get("pnl")) > 0]
        losers = [sample for sample in samples if _float(sample.get("pnl")) < 0]
        winner_features = Counter(
            feature
            for sample in winners
            for feature in _string_set(sample.get("positive_features") or [])
        )
        loser_risks = Counter(
            risk
            for sample in losers
            for risk in _string_set(sample.get("failure_risks") or [])
        )
        close_reasons = Counter(str(sample.get("close_reason") or "") for sample in samples if sample.get("close_reason"))
        sample_count = len(samples)
        return {
            "sample_count": sample_count,
            "data_confidence": "LOW" if sample_count < 20 else ("MEDIUM" if sample_count < 100 else "HIGH"),
            "learning_mode": "paper_forward_training" if sample_count < 20 else "statistical_filtering",
            "winner_features": [{"name": k, "count": v} for k, v in winner_features.most_common(8)],
            "loser_risks": [{"name": k, "count": v} for k, v in loser_risks.most_common(8)],
            "close_reasons": [{"name": k, "count": v} for k, v in close_reasons.most_common(6)],
            "coach_rules": [
                "A paper OPEN is for learning only, not live permission.",
                "Do not repeat exact symbol/side or same strategy_kind buckets marked AVOID.",
                "REVIEW buckets require a material change; they do not automatically block paper validation.",
                "Prefer candidates with feature_score and estimated_win_rate above the paper gate plus explicit invalidation geometry.",
                "Losses with low MFE or high MAE teach entry timing and stop placement, not just side direction.",
            ],
        }

    def _observation_lessons(
        self,
        observations: list[dict[str, Any]],
        *,
        item: RadarItem | None = None,
    ) -> dict[str, Any]:
        reason_counts = Counter()
        decision_counts = Counter()
        same_symbol_side: list[dict[str, Any]] = []
        same_side: list[dict[str, Any]] = []
        feature_overlap: list[dict[str, Any]] = []
        current_risks: set[str] = set()
        current_positives: set[str] = set()
        current_symbol = ""
        current_side = ""
        if item is not None:
            current_symbol = item.symbol
            current_side = item.direction
            feature = self._feature_snapshot(item)
            current_risks = _string_set(feature.get("failure_risks") or [])
            current_positives = _string_set(feature.get("positive_factors") or [])

        for row in observations:
            decision_counts[str(row.get("decision") or "")] += 1
            for token in self._observation_reason_tokens(row):
                reason_counts[token] += 1
            symbol = str(row.get("symbol") or "")
            side = str(row.get("side") or "")
            if item is None:
                continue
            if symbol == current_symbol and side == current_side:
                same_symbol_side.append(row)
            if side == current_side:
                same_side.append(row)
            feature = row.get("cyqnt_feature_enhancement") if isinstance(row.get("cyqnt_feature_enhancement"), dict) else {}
            risks = _string_set(feature.get("failure_risks") or [])
            positives = _string_set(feature.get("positive_factors") or [])
            if risks.intersection(current_risks) or positives.intersection(current_positives):
                feature_overlap.append(row)

        return {
            "sample_type": "decision_observation_not_trade_outcome",
            "observation_count": len(observations),
            "same_symbol_side_count": len(same_symbol_side),
            "same_side_count": len(same_side),
            "feature_overlap_count": len(feature_overlap),
            "top_decisions": [
                {"name": name, "count": count}
                for name, count in decision_counts.most_common(6)
                if name
            ],
            "top_rejection_reasons": [
                {"name": name, "count": count}
                for name, count in reason_counts.most_common(8)
                if name
            ],
            "recent": [self._compact_observation(row) for row in observations[:8]],
            "current_candidate_repeat_risks": [
                self._compact_observation(row) for row in (same_symbol_side or feature_overlap)[:4]
            ],
            "instruction": (
                "Decision observations teach why AI/gates waited or rejected. They reduce repeated bad plan generation, "
                "but they are not wins/losses and must not count as live-readiness proof."
            ),
        }

    def _compact_observation(self, row: dict[str, Any]) -> dict[str, Any]:
        feature = row.get("cyqnt_feature_enhancement") if isinstance(row.get("cyqnt_feature_enhancement"), dict) else {}
        radar = row.get("radar") if isinstance(row.get("radar"), dict) else {}
        return {
            "created_at": row.get("created_at"),
            "symbol": row.get("symbol"),
            "side": row.get("side"),
            "decision": row.get("decision"),
            "reason": row.get("reason"),
            "stage": row.get("stage"),
            "candidate_source": row.get("candidate_source"),
            "plan_action": row.get("plan_action"),
            "wait_type": row.get("wait_type"),
            "radar": {
                "score": radar.get("score"),
                "rank": radar.get("rank"),
                "fund_confirm": radar.get("fund_confirm"),
                "fake_breakout_risk": radar.get("fake_breakout_risk"),
                "direction_confirmations": radar.get("direction_confirmations"),
                "wick_ratio": radar.get("wick_ratio"),
            },
            "cyqnt": {
                "feature_score": feature.get("feature_score"),
                "estimated_win_rate": feature.get("estimated_win_rate"),
                "selection_score": feature.get("selection_score"),
                "positive_factors": list(feature.get("positive_factors") or [])[:4],
                "failure_risks": list(feature.get("failure_risks") or [])[:4],
            },
        }

    def _observation_reason_tokens(self, row: dict[str, Any]) -> list[str]:
        raw = str(row.get("reason") or row.get("wait_type") or row.get("decision") or "")
        for sep in (";", "|"):
            raw = raw.replace(sep, ",")
        tokens = [part.strip() for part in raw.split(",") if part.strip()]
        if not tokens and row.get("decision"):
            tokens = [str(row.get("decision"))]
        return tokens[:6]

    def _candidate_learning_delta(self, feature: dict[str, Any], samples: list[dict[str, Any]], side: str) -> dict[str, Any]:
        current_pos = _string_set(feature.get("positive_factors") or [])
        current_risk = _string_set(feature.get("failure_risks") or [])
        side_losses = [sample for sample in samples if sample.get("side") == side and _float(sample.get("pnl")) < 0]
        loss_risks = Counter(
            risk
            for sample in side_losses
            for risk in _string_set(sample.get("failure_risks") or [])
        )
        loss_positives = Counter(
            pos
            for sample in side_losses
            for pos in _string_set(sample.get("positive_features") or [])
        )
        resolved_risks = [
            {"name": key, "count": loss_risks[key]}
            for key in sorted(set(loss_risks).difference(current_risk), key=lambda k: (-loss_risks[k], k))[:6]
        ]
        novel_positives = [key for key in sorted(current_pos) if key not in loss_positives]
        resolved_improvements = [f"risk_resolved:{row['name']}" for row in resolved_risks]
        return {
            "current_positive_factors": sorted(current_pos)[:8],
            "current_failure_risks": sorted(current_risk)[:8],
            "overlaps_with_losing_risks": [
                {"name": key, "count": loss_risks[key]}
                for key in sorted(current_risk.intersection(loss_risks), key=lambda k: loss_risks[k], reverse=True)[:6]
            ],
            "resolved_losing_risks": resolved_risks,
            "material_improvements_vs_losses": [*novel_positives, *resolved_improvements][:6],
            "instruction": (
                "If material_improvements_vs_losses is non-empty, overlaps_with_losing_risks is empty, and hard avoid_repeating "
                "is empty, Codex may create a paper-only validation OPEN when risk geometry and costs are valid. "
                "If overlaps_with_losing_risks is non-empty, WAIT until those losing risks are resolved."
            ),
        }

    def _generation_gate(
        self,
        *,
        hard_avoid: list[dict[str, Any]],
        review: list[dict[str, Any]],
        feature: dict[str, Any],
    ) -> dict[str, Any]:
        failure_risks = _string_set(
            feature.get("failure_risks")
            or feature.get("negative_factors")
            or feature.get("main_failure_risks")
            or []
        )
        reasons: list[str] = []
        if hard_avoid:
            reasons.append("avoid_repeating")

        estimated_win_rate = _float(feature.get("estimated_win_rate"))
        paper_floor = max(0.50, float(settings.strategy_min_paper_win_rate or 0.53) - 0.03)
        if estimated_win_rate > 0 and estimated_win_rate < paper_floor:
            reasons.append("cyqnt_estimated_win_rate_low")

        feature_score = _float(feature.get("feature_score"))
        if feature_score > 0 and feature_score < 45.0:
            reasons.append("cyqnt_feature_score_low")

        hard_failure_risks = {
            "current_wick_extreme",
            "fake_breakout_high",
            "market_stale",
            "side_conflict",
            "geometry_invalid",
        }
        for risk in sorted(failure_risks.intersection(hard_failure_risks)):
            reasons.append(f"hard_failure_risk:{risk}")

        reasons = list(dict.fromkeys(reasons))
        return {
            "allow_open_plan": not reasons,
            "review_required": bool(review and not reasons),
            "reasons": reasons,
            "review_reasons": [str(match.get("name") or "") for match in review[:4] if match.get("name")],
            "instruction": (
                "If allow_open_plan is false, Codex must return WAIT. "
                "If review_required is true, Codex may generate only a paper-only validation OPEN when the plan states "
                "material evidence improvements and passes local quality/risk gates."
            ),
        }

    def _match_report(self, name: str, samples: list[dict[str, Any]]) -> dict[str, Any]:
        samples = sorted(samples, key=lambda sample: int(sample.get("close_time") or 0), reverse=True)
        metrics = self._sample_metrics(samples)
        min_block = max(1, int(settings.strategy_block_symbol_side_min_trades or 3))
        review_min = max(1, min(2, min_block))
        recent_recovered = self._recent_bucket_recovered(samples, min_block)
        severity = "PASS"
        can_hard_avoid = name in {"exact_symbol_side", "same_strategy_kind_side"}
        if (
            can_hard_avoid
            and not recent_recovered
            and metrics["trades"] >= min_block
            and metrics["pnl"] < 0
            and metrics["win_rate"] <= float(settings.strategy_block_symbol_side_win_rate)
        ):
            severity = "AVOID"
        elif metrics["trades"] >= review_min and metrics["pnl"] < 0:
            severity = "REVIEW"
        return {
            "name": name,
            "samples": metrics["trades"],
            "win_rate": metrics["win_rate"],
            "profit_factor": metrics["profit_factor"],
            "pnl": metrics["pnl"],
            "avg_pnl": metrics["avg_pnl"],
            "severity": severity,
            "recent_recovered": recent_recovered,
            "recent_close_reasons": [sample.get("close_reason") for sample in samples[:4] if sample.get("close_reason")],
        }

    def _recent_bucket_recovered(self, samples: list[dict[str, Any]], min_samples: int) -> bool:
        recent = samples[: max(1, int(min_samples))]
        if len(recent) < max(1, int(min_samples)):
            return False
        metrics = self._sample_metrics(recent)
        return metrics["pnl"] > 0 and metrics["win_rate"] >= max(0.50, float(settings.strategy_block_symbol_side_win_rate or 0.34))

    def _timeframes_aligned(self, item: RadarItem, side: str) -> bool:
        if side == "LONG":
            return item.change_5m > 0 and item.change_15m > 0 and item.change_1h >= 0
        if side == "SHORT":
            return item.change_5m < 0 and item.change_15m < 0 and item.change_1h <= 0
        return False

    def _taker_aligned(self, item: RadarItem, side: str) -> bool:
        return item.taker_buy_ratio >= 0.58 if side == "LONG" else item.taker_sell_ratio >= 0.58

    def _depth_aligned(self, item: RadarItem, side: str) -> bool:
        return item.depth_imbalance >= 0.12 if side == "LONG" else item.depth_imbalance <= -0.12

    def _sm_aligned(self, item: RadarItem, side: str) -> bool:
        return item.sm_delta >= 0 if side == "LONG" else item.sm_delta <= 0


ai_strategy_feedback = AIStrategyFeedback()


def _float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


def _string_set(values: Any) -> set[str]:
    if isinstance(values, str):
        values = [values]
    if not isinstance(values, list):
        return set()
    return {str(value).strip() for value in values if str(value).strip()}
