from __future__ import annotations

from typing import Any

from backend.config import settings
from backend.exchange.binance_futures import binance_futures
from backend.learning.learning_data_audit import learning_data_audit
from backend.learning.trade_attributor import trade_attributor
from backend.market.binance_factor_source import binance_factor_source
from backend.market.market_service import market_service
from backend.market.binance_rest import binance_rest
from backend.positions.position_manager import position_manager
from backend.positions.position_registry import position_registry
from backend.trading.exchange_reconciliation import exchange_reconciliation
from backend.trading.performance_guard import performance_guard


EXCHANGE_RECONCILIATION_MAX_AGE_SECONDS = 90.0


class LiveReadiness:
    """Graduation gate from paper sampling to live validation.

    This module is deliberately read-only. It does not start trading and never
    enables live_trading_enabled. It only explains what stage is currently safe.
    """

    def summary(self) -> dict[str, Any]:
        performance = performance_guard.summary()
        attribution = trade_attributor.summary()
        data_quality = learning_data_audit.summary()
        positions = position_manager.summary()
        open_positions = position_registry.list_open()
        exchange_status = exchange_reconciliation.cached()
        blockers = self._blockers(performance, attribution, positions, open_positions, data_quality, exchange_status)
        phases = self._phases(blockers)
        current_stage = self._current_stage(phases)
        return {
            "current_stage": current_stage,
            "paper_is_terminal": False,
            "instruction": (
                "Paper is only the sample-collection layer. Graduation path is "
                "paper_probe -> shadow_live -> live_test_order -> micro_live -> scale_live."
            ),
            "phases": phases,
            "blockers": blockers,
            "metrics": {
                "performance": performance,
                "attribution": {
                    "sample_count": attribution.get("sample_count"),
                    "global_win_rate": attribution.get("global_win_rate"),
                    "global_profit_factor": attribution.get("global_profit_factor"),
                    "global_pnl": attribution.get("global_pnl"),
                    "data_quality": data_quality.get("trust_level"),
                    "data_quality_reasons": data_quality.get("reasons"),
                },
                "learning_data_quality": data_quality,
                "positions": {
                    "open_count": positions.get("open_count"),
                    "floating_pnl": positions.get("floating_pnl"),
                    "total_pnl": positions.get("total_pnl"),
                    "used_margin": positions.get("used_margin"),
                },
                "execution": {
                    "market_data_source": binance_rest.last_public_source,
                    "market_refresh_degraded": binance_factor_source.last_refresh_degraded,
                    "market_refresh_error": binance_factor_source.last_refresh_error,
                    "market_refresh_source": binance_factor_source.last_refresh_source,
                    "market_snapshot_count": binance_factor_source.last_snapshot_count,
                    "effective_market_snapshot_count": _effective_market_snapshot_count(),
                    "market_service_snapshot_count": len(market_service.last_snapshots),
                    "exchange_reconciliation": _compact_exchange_reconciliation(exchange_status),
                    "trade_mode": settings.trade_mode,
                    "live_trading_enabled": settings.live_trading_enabled,
                    "live_use_test_order": settings.live_use_test_order,
                    "binance_configured": binance_futures.configured(),
                    "attach_protection_orders": settings.attach_protection_orders,
                    "performance_guard_enabled": settings.auto_trading_use_performance_guard,
                    "max_open_positions": settings.max_open_positions,
                },
            },
            "next_actions": self._next_actions(blockers, phases),
        }

    def _blockers(
        self,
        performance: dict[str, Any],
        attribution: dict[str, Any],
        positions: dict[str, Any],
        open_positions: list,
        data_quality: dict[str, Any],
        exchange_status: dict[str, Any],
    ) -> list[dict[str, Any]]:
        blockers: list[dict[str, Any]] = []
        sample_count = int(attribution.get("sample_count") or 0)
        min_samples = max(30, int(settings.trade_attribution_min_samples) * 3)
        perf_win = float(performance.get("win_rate") or 0.0)
        recent_win = float(performance.get("recent_win_rate") or 0.0)
        perf_pnl = float(performance.get("pnl") or 0.0)
        attr_win = float(attribution.get("global_win_rate") or 0.0)
        attr_pf = float(attribution.get("global_profit_factor") or 0.0)
        attr_pnl = float(attribution.get("global_pnl") or 0.0)
        open_count = len(open_positions)
        learning_production_grade = bool(data_quality.get("production_grade"))

        if bool(performance.get("recovery_mode")):
            blockers.append(_block("performance_recovery_mode", "live_test_order", "performance is still in recovery mode"))
        if sample_count < min_samples:
            blockers.append(_block("learning_samples_low", "live_test_order", f"learning samples {sample_count} < {min_samples}"))
        if not learning_production_grade:
            reasons = ",".join((data_quality.get("reasons") or [])[:4])
            blockers.append(_block("learning_data_not_production_grade", "live_test_order", f"learning data is not production-grade: {reasons or 'unknown'}"))
        if perf_win < 0.52:
            blockers.append(_block("paper_win_rate_low", "live_test_order", f"paper win rate {perf_win:.2%} < 52%"))
        if recent_win < 0.50:
            blockers.append(_block("recent_win_rate_low", "live_test_order", f"recent win rate {recent_win:.2%} < 50%"))
        if perf_pnl <= 0:
            blockers.append(_block("paper_pnl_not_positive", "live_test_order", f"paper pnl {perf_pnl:.4f} <= 0"))
        if int(performance.get("loss_streak") or 0) > 2:
            blockers.append(_block("loss_streak_high", "live_test_order", f"loss streak {performance.get('loss_streak')} > 2"))
        if learning_production_grade and attr_win < 0.50:
            blockers.append(_block("attribution_win_rate_low", "live_test_order", f"attribution win rate {attr_win:.2%} < 50%"))
        if learning_production_grade and attr_pf < 1.05:
            blockers.append(_block("attribution_profit_factor_low", "live_test_order", f"attribution PF {attr_pf:.2f} < 1.05"))
        if learning_production_grade and attr_pnl <= 0:
            blockers.append(_block("attribution_pnl_not_positive", "live_test_order", f"attribution pnl {attr_pnl:.4f} <= 0"))
        if open_count > 0:
            blockers.append(_block("open_position_exists", "live_test_order", f"open positions {open_count} > 0"))
        stale_positions = [
            getattr(position, "symbol", "")
            for position in open_positions
            if bool(getattr(position, "price_stale", False))
        ]
        if stale_positions:
            blockers.append(
                _block(
                    "position_price_stale",
                    "live_test_order",
                    f"open position price is stale/unsafe: {','.join(stale_positions[:5])}",
                )
            )
        if binance_factor_source.last_refresh_degraded:
            blockers.append(
                _block(
                    "market_refresh_degraded",
                    "live_test_order",
                    f"market refresh degraded: {binance_factor_source.last_refresh_error or 'unknown'}",
                )
            )
        if binance_factor_source.last_refresh_source in {"", "none"} and _effective_market_snapshot_count() == 0:
            blockers.append(_block("market_refresh_missing", "live_test_order", "market refresh has no verified snapshots yet"))
        if not settings.auto_trading_use_performance_guard:
            blockers.append(_block("performance_guard_disabled", "live_test_order", "performance guard must stay enabled"))
        if int(settings.max_open_positions or 0) != 1:
            blockers.append(_block("max_open_positions_not_one", "live_test_order", "live validation requires max_open_positions=1"))
        if str(settings.trade_mode).lower() != "live":
            blockers.append(_block("trade_mode_not_live", "live_test_order", "trade_mode must be live for exchange validation"))
        if not binance_futures.configured():
            blockers.append(_block("binance_keys_missing", "live_test_order", "Binance keys are not configured"))
        if _exchange_reconciliation_required():
            exchange_age = exchange_status.get("age_seconds")
            if not _safe_int(exchange_status.get("ts_ms"), 0):
                blockers.append(
                    _block(
                        "exchange_reconciliation_missing",
                        "live_test_order",
                        "exchange reconciliation has not refreshed yet",
                    )
                )
            elif not isinstance(exchange_age, (int, float)) or exchange_age > EXCHANGE_RECONCILIATION_MAX_AGE_SECONDS:
                blockers.append(
                    _block(
                        "exchange_reconciliation_stale",
                        "live_test_order",
                        f"exchange reconciliation is stale: age={exchange_age}s > {EXCHANGE_RECONCILIATION_MAX_AGE_SECONDS:.0f}s",
                    )
                )
            elif bool(exchange_status.get("skipped")):
                blockers.append(
                    _block(
                        "exchange_reconciliation_skipped",
                        "live_test_order",
                        f"exchange reconciliation skipped: {exchange_status.get('reason') or 'unknown'}",
                    )
                )
            elif not bool(exchange_status.get("ok")):
                issue_codes = [
                    str(issue.get("code") or "unknown")
                    for issue in (exchange_status.get("issues") or [])[:5]
                    if isinstance(issue, dict)
                ]
                message = ",".join(issue_codes) or str(exchange_status.get("reason") or "unknown")
                blockers.append(
                    _block(
                        "exchange_reconciliation_failed",
                        "live_test_order",
                        f"exchange/local state mismatch: {message}",
                    )
                )
        if binance_rest.last_public_source != "mainnet":
            blockers.append(_block("market_source_not_mainnet", "live_test_order", "market data source must be mainnet"))
        if not settings.attach_protection_orders:
            blockers.append(_block("protection_orders_disabled", "micro_live", "protection orders must be enabled before real orders"))
        if settings.live_trading_enabled:
            blockers.append(_block("live_trading_already_enabled", "all", "live trading is already enabled; this should be explicit and supervised"))
        return blockers

    def _phases(self, blockers: list[dict[str, Any]]) -> list[dict[str, Any]]:
        live_test_blockers = [b for b in blockers if b["stage"] in {"live_test_order", "all"}]
        micro_blockers = [b for b in blockers if b["stage"] in {"live_test_order", "micro_live", "all"}]
        return [
            {
                "name": "paper_probe",
                "allowed": bool(settings.paper_probe_enabled and not settings.live_trading_enabled),
                "purpose": "collect controlled paper samples and attribution",
                "requires_manual_approval": False,
                "blockers": [] if settings.paper_probe_enabled else [_block("paper_probe_disabled", "paper_probe", "paper probe is disabled")],
            },
            {
                "name": "shadow_live",
                "allowed": bool(
                    binance_rest.last_public_source == "mainnet"
                    and not binance_factor_source.last_refresh_degraded
                    and not (binance_factor_source.last_refresh_source in {"", "none"} and _effective_market_snapshot_count() == 0)
                    and not settings.live_trading_enabled
                ),
                "purpose": "compare decisions against mainnet data without sending orders",
                "requires_manual_approval": False,
                "blockers": self._shadow_live_blockers(),
            },
            {
                "name": "live_test_order",
                "allowed": not live_test_blockers,
                "purpose": "Binance test order only; validates exchange filters without real fill",
                "requires_manual_approval": True,
                "blockers": live_test_blockers,
            },
            {
                "name": "micro_live",
                "allowed": not micro_blockers and not settings.live_use_test_order,
                "purpose": "smallest real notional with protection orders attached",
                "requires_manual_approval": True,
                "blockers": micro_blockers + ([] if not settings.live_use_test_order else [_block("still_in_test_order_mode", "micro_live", "live_use_test_order is still true")]),
            },
            {
                "name": "scale_live",
                "allowed": False,
                "purpose": "increase sizing only after multiple positive micro-live exits",
                "requires_manual_approval": True,
                "blockers": [_block("micro_live_track_record_missing", "scale_live", "micro-live track record is not available yet")],
            },
        ]

    def _current_stage(self, phases: list[dict[str, Any]]) -> str:
        for phase in reversed(phases):
            if phase.get("allowed"):
                return str(phase.get("name"))
        return "paper_probe"

    def _next_actions(self, blockers: list[dict[str, Any]], phases: list[dict[str, Any]]) -> list[str]:
        live_test = next((phase for phase in phases if phase["name"] == "live_test_order"), {})
        if live_test.get("allowed"):
            return [
                "Do not enable real orders automatically.",
                "If the user explicitly approves, use live test order mode first.",
                "Keep max_open_positions=1 and protection-order checks enabled.",
            ]
        codes = {block["code"] for block in blockers}
        actions: list[str] = []
        if "open_position_exists" in codes:
            actions.append("Wait for the current paper probe to close before validating the next stage.")
        if {"market_refresh_degraded", "market_refresh_missing", "position_price_stale"} & codes:
            actions.append("Stabilize Binance market data and clear stale-price positions before any live validation.")
        if {"performance_recovery_mode", "paper_pnl_not_positive", "paper_win_rate_low"} & codes:
            actions.append("Improve paper strategy quality until recovery mode clears, win rate rises, and PnL turns positive.")
        if {"attribution_profit_factor_low", "attribution_pnl_not_positive", "attribution_win_rate_low"} & codes:
            actions.append("Use attribution to avoid repeated loss structures and sample only cleaner paper probes.")
        if "learning_data_not_production_grade" in codes:
            actions.append("Do not treat replay-only learning as production proof; collect real closed samples or candle-level backtests.")
        if "binance_keys_missing" in codes:
            actions.append("Configure Binance keys locally before exchange test-order validation.")
        if not actions:
            actions.append("Continue controlled paper/shadow-live observation until the live_test_order gate clears.")
        return actions

    def _shadow_live_blockers(self) -> list[dict[str, str]]:
        blockers: list[dict[str, str]] = []
        if binance_rest.last_public_source != "mainnet":
            blockers.append(_block("market_source_not_mainnet", "shadow_live", "shadow live needs mainnet data"))
        if binance_factor_source.last_refresh_degraded:
            blockers.append(
                _block(
                    "market_refresh_degraded",
                    "shadow_live",
                    f"market refresh degraded: {binance_factor_source.last_refresh_error or 'unknown'}",
                )
            )
        if binance_factor_source.last_refresh_source in {"", "none"} and _effective_market_snapshot_count() == 0:
            blockers.append(_block("market_refresh_missing", "shadow_live", "market refresh has no verified snapshots yet"))
        return blockers


def _block(code: str, stage: str, message: str) -> dict[str, str]:
    return {"code": code, "stage": stage, "message": message}


def _effective_market_snapshot_count() -> int:
    return max(int(binance_factor_source.last_snapshot_count or 0), len(market_service.last_snapshots))


def _exchange_reconciliation_required() -> bool:
    return str(settings.trade_mode).lower() == "live" and binance_futures.configured()


def _compact_exchange_reconciliation(status: dict[str, Any]) -> dict[str, Any]:
    issues = status.get("issues") if isinstance(status, dict) else []
    return {
        "ok": bool(status.get("ok")) if isinstance(status, dict) else False,
        "age_seconds": status.get("age_seconds") if isinstance(status, dict) else None,
        "skipped": bool(status.get("skipped")) if isinstance(status, dict) else False,
        "reason": status.get("reason") if isinstance(status, dict) else "missing",
        "local_live_count": len(status.get("local_live_positions") or []) if isinstance(status, dict) else 0,
        "exchange_position_count": len(status.get("exchange_positions") or []) if isinstance(status, dict) else 0,
        "open_order_count": int(status.get("open_order_count") or 0) if isinstance(status, dict) else 0,
        "issue_count": len(issues or []),
        "issue_codes": [
            str(issue.get("code") or "unknown")
            for issue in (issues or [])[:8]
            if isinstance(issue, dict)
        ],
    }


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except Exception:
        return default


live_readiness = LiveReadiness()
