from __future__ import annotations

from dataclasses import asdict
from typing import Any

from backend.ai_strategy.dynamic_trade_model import auto_trading_risk_model
from backend.ai_strategy.openai_strategy_client import openai_strategy_client
from backend.config import settings
from backend.learning.ai_strategy_feedback import ai_strategy_feedback
from backend.learning.strategy_registry import strategy_registry
from backend.models import MarketSnapshot, RadarItem, now_ms
from backend.positions.position_manager import position_manager
from backend.positions.position_registry import position_registry
from backend.radar.candidate_feature_enhancer import candidate_feature_enhancer
from backend.radar.fake_breakout import fake_breakout
from backend.radar.fund_confirm import fund_confirm
from backend.radar.radar_engine import radar_engine
from backend.radar.score_engine import score_engine
from backend.radar.smart_money import smart_money
from backend.trading.ai_trade_director import ai_trade_director
from backend.trading.paper_executor import paper_executor


class TradeAcceptanceRunner:
    async def run_controlled_paper_cycle(self) -> dict[str, Any]:
        before_open_ids = {p.position_id for p in position_registry.list_open()}
        before_closed_ids = {row.get("position_id") for row in position_registry.list_closed(limit=200)}
        item = self._acceptance_candidate()
        cyqnt = candidate_feature_enhancer.evaluate(item).asdict()
        plan = await openai_strategy_client.generate(
            item,
            {
                "open_positions": 0,
                "performance_guard": {"recovery_mode": False},
                "candidate_selection": {
                    "source": "acceptance_controlled_paper_cycle",
                    "paper_validation": True,
                    "paper_probe": True,
                    "acceptance_mode": True,
                },
            },
        )
        plan.raw = {**plan.raw, "acceptance_mode": True, "cyqnt_feature_enhancement": cyqnt}
        exec_plan = auto_trading_risk_model.decide(
            item,
            plan,
            {
                "equity": max(5000.0, float(settings.paper_account_equity_usdt or 1000.0)),
                "available_balance": max(5000.0, float(settings.paper_account_equity_usdt or 1000.0)),
                "loss_streak": 0,
                "open_positions": 0,
                "max_open_positions": 1,
                "trade_mode": "paper",
                "execution_context": "paper_closed_loop",
            },
            {"market_heat": radar_engine.market_heat, "volatility_regime": "normal"},
            paper_probe=True,
        )
        execution_allowed = exec_plan.decision in {"OPEN", "PAPER_ONLY"}
        stages: list[dict[str, Any]] = [
            self._stage("scan_candidate", True, {"symbol": item.symbol, "side": item.direction, "score": item.score}),
            self._stage("cyqnt_evidence", bool(cyqnt.get("cyqnt_available")), cyqnt),
            self._stage("strategy_plan", plan.action in {"OPEN_LONG", "OPEN_SHORT"}, self._plan_snapshot(plan)),
            self._stage("risk_model", execution_allowed, asdict(exec_plan)),
        ]

        if not execution_allowed:
            report = self._report(stages, before_open_ids, before_closed_ids, result={"blocked": exec_plan.reason})
            ai_trade_director.last_cycle = {**ai_trade_director.last_cycle, "acceptance": report}
            return report

        position = await paper_executor.open_position("acceptance_cycle", plan.strategy_id, item.score, exec_plan)
        stages.append(self._stage("paper_open", position.position_id in {p.position_id for p in position_registry.list_open()}, position.asdict()))
        feedback_open = ai_strategy_feedback.record_open(
            plan=plan,
            item=item,
            exec_plan=exec_plan,
            position=position,
            candidate_source="acceptance_controlled_paper_cycle",
            paper_validation=True,
        )
        stages.append(self._stage("learning_open_recorded", bool(feedback_open.get("recorded")), feedback_open))

        exit_price = self._profitable_exit_price(position)
        position_manager.update_position(position, exit_price)
        stages.append(
            self._stage(
                "position_manager_active",
                position.mfe != 0 or position.mae != 0 or position.unrealized_pnl != 0,
                {
                    "position_id": position.position_id,
                    "unrealized_pnl": position.unrealized_pnl,
                    "mfe": position.mfe,
                    "mae": position.mae,
                    "lifecycle_state": position.lifecycle_state,
                },
            )
        )

        closed = position_manager.close_position(position, "ACCEPTANCE_TP2", exit_price=exit_price)
        stages.append(self._stage("paper_close", closed.position_id not in {p.position_id for p in position_registry.list_open()}, closed.asdict()))
        stored_strategy = strategy_registry.get(plan.strategy_id) or {}
        forward = stored_strategy.get("forward") or {}
        samples = list(forward.get("samples") or [])
        learning_closed = bool(samples and any(sample.get("position_id") == closed.position_id for sample in samples))
        stages.append(
            self._stage(
                "learning_close_recorded",
                learning_closed,
                {
                    "strategy_id": plan.strategy_id,
                    "strategy_status": stored_strategy.get("status"),
                    "metrics": stored_strategy.get("metrics"),
                    "sample_count": len(samples),
                },
            )
        )

        report = self._report(
            stages,
            before_open_ids,
            before_closed_ids,
            result={
                "position_id": position.position_id,
                "strategy_id": plan.strategy_id,
                "symbol": item.symbol,
                "pnl": closed.pnl,
                "close_reason": closed.close_reason,
            },
        )
        ai_trade_director.last_cycle = {**ai_trade_director.last_cycle, "acceptance": report}
        return report

    def _acceptance_candidate(self) -> RadarItem:
        snapshot = MarketSnapshot(
            symbol="ACCEPTUSDT",
            price=100.0,
            change_5m=1.8,
            change_15m=2.6,
            change_1h=1.4,
            volume_spike=3.0,
            oi_change=2.0,
            funding_rate=0.0001,
            taker_buy_ratio=0.72,
            taker_sell_ratio=0.28,
            depth_imbalance=0.24,
            atr_pct=1.2,
            wick_ratio=0.24,
        )
        sm_position, sm_delta = smart_money.estimate(snapshot)
        fake, fake_score = fake_breakout(snapshot, "LONG")
        features = score_engine.feature_scores(snapshot, sm_position, 70, fake_score)
        score = score_engine.total(features)
        fund_count, fund_total = fund_confirm(snapshot, "LONG")
        return RadarItem(
            rank=1,
            symbol=snapshot.symbol,
            base_asset="ACCEPT",
            price=snapshot.price,
            direction="LONG",
            stage="acceptance",
            trigger_mode="controlled_paper_cycle",
            score=max(78.0, score),
            score_history=[55.0, 68.0, max(78.0, score)],
            rank_history=[5, 2, 1],
            heat_slope=12.0,
            slope_score=88.0,
            fake_breakout_risk=fake,
            change_5m=snapshot.change_5m,
            change_15m=snapshot.change_15m,
            change_1h=snapshot.change_1h,
            oi_change=snapshot.oi_change,
            fund_confirm_count=max(3, fund_count),
            fund_confirm_total=fund_total,
            dealer_radar="acceptance_long_followthrough",
            sm_position=sm_position,
            sm_delta=max(0.8, sm_delta),
            volume_spike=snapshot.volume_spike,
            funding_rate=snapshot.funding_rate,
            taker_buy_ratio=snapshot.taker_buy_ratio,
            taker_sell_ratio=snapshot.taker_sell_ratio,
            depth_imbalance=snapshot.depth_imbalance,
            atr_pct=snapshot.atr_pct,
            wick_ratio=snapshot.wick_ratio,
        )

    def _profitable_exit_price(self, position) -> float:
        return float(position.tp2 or position.entry_price * 1.03)

    def _stage(self, name: str, ok: bool, evidence: dict[str, Any]) -> dict[str, Any]:
        return {"name": name, "ok": bool(ok), "evidence": evidence}

    def _plan_snapshot(self, plan) -> dict[str, Any]:
        return {
            "strategy_id": plan.strategy_id,
            "action": plan.action,
            "side": plan.side,
            "entry": plan.ideal_entry_price,
            "stop_loss": plan.stop_loss,
            "tp1": plan.tp1,
            "tp2": plan.tp2,
            "provider": plan.raw.get("provider"),
            "cyqnt_feature_enhancement": plan.raw.get("cyqnt_feature_enhancement"),
        }

    def _report(
        self,
        stages: list[dict[str, Any]],
        before_open_ids: set[str],
        before_closed_ids: set[str],
        *,
        result: dict[str, Any],
    ) -> dict[str, Any]:
        open_ids = {p.position_id for p in position_registry.list_open()}
        closed_ids = {row.get("position_id") for row in position_registry.list_closed(limit=200)}
        return {
            "ok": all(stage["ok"] for stage in stages),
            "mode": "controlled_paper_acceptance",
            "real_order_allowed": False,
            "stages": stages,
            "result": result,
            "position_delta": {
                "opened_during_test": list((open_ids | closed_ids) - (before_open_ids | before_closed_ids)),
                "open_positions_after": len(open_ids),
                "closed_positions_after": len(closed_ids),
            },
            "acceptance_standard": {
                "requires_scan_candidate": True,
                "requires_cyqnt_evidence": True,
                "requires_strategy_plan": True,
                "requires_risk_model": True,
                "requires_paper_open": True,
                "requires_position_manager": True,
                "requires_paper_close": True,
                "requires_learning_close_record": True,
            },
        }


trade_acceptance_runner = TradeAcceptanceRunner()
