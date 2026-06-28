from __future__ import annotations

from typing import Any

from backend.ai_strategy.openai_strategy_client import openai_strategy_client
from backend.config import settings
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
        ai_status = openai_strategy_client.status(candidate_count=len(candidates), candidate_source=candidate_source)
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

    def _record_cycle(self, cycle: dict[str, Any]) -> None:
        self.last_cycle = cycle
        self.cycle_log.insert(0, cycle)
        self.cycle_log = self.cycle_log[:50]


ai_trade_director = AITradeDirector()
