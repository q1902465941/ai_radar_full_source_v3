from __future__ import annotations

from typing import Any

from backend.ai_strategy.ai_service import ai_service
from backend.config import settings
from backend.exchange.binance_futures import binance_futures
from backend.learning.ai_strategy_feedback import ai_strategy_feedback
from backend.learning.learning_data_audit import learning_data_audit
from backend.learning.trade_attributor import trade_attributor
from backend.market.binance_factor_source import binance_factor_source
from backend.market.binance_rest import binance_rest
from backend.market.binance_ws_ticker import binance_ticker_stream
from backend.market.dynamic_symbol_stream import dynamic_symbol_stream
from backend.market.market_service import market_service
from backend.models import now_ms
from backend.radar.radar_engine import radar_engine
from backend.storage.db import DB, db
from backend.trading.autotrader import autotrader
from backend.trading.live_readiness import live_readiness
from backend.trading.performance_guard import performance_guard


DB_TABLES = (
    "radar_snapshots",
    "positions",
    "closed_positions",
    "kv",
    "evolved_strategies",
    "strategy_evolution_runs",
    "ai_decision_observations",
    "universal_anomaly_samples",
)


def system_readiness_report(*, warmup_started: bool = False, scan_error: str = "") -> dict[str, Any]:
    performance = _safe_dict(performance_guard.summary)
    loop_ok, loop_reason, loop_performance = _loop_guard()
    candidate_filter = _safe_dict(lambda: autotrader.candidate_diagnostics_light(performance))
    candidate_source = str(candidate_filter.get("candidate_source") or "")
    candidate_symbols = [str(symbol) for symbol in candidate_filter.get("candidate_symbols") or []]
    ai_status = _safe_dict(
        lambda: ai_service.status(
            candidate_count=len(candidate_symbols),
            candidate_source=candidate_source,
        )
    )
    live = _safe_dict(live_readiness.summary)
    data_quality = _safe_dict(learning_data_audit.summary)
    attribution = _safe_dict(trade_attributor.summary)
    feedback = _safe_dict(ai_strategy_feedback.quality_summary)
    market = market_data_status(warmup_started=warmup_started, scan_error=scan_error)
    websocket = websocket_status()
    database = database_health(db)
    codex = codex_status(ai_status)

    wait_blockers = wait_blockers_for(
        market=market,
        candidate_filter=candidate_filter,
        candidate_symbols=candidate_symbols,
        loop_ok=loop_ok,
        loop_reason=loop_reason,
        ai_status=ai_status,
        codex=codex,
    )
    paper = paper_learning_status(
        performance=performance,
        loop_ok=loop_ok,
        loop_reason=loop_reason,
        loop_performance=loop_performance,
        candidate_filter=candidate_filter,
        candidate_symbols=candidate_symbols,
        ai_status=ai_status,
        codex=codex,
        data_quality=data_quality,
        attribution=attribution,
        feedback=feedback,
    )
    live_enablement = live_enablement_status(live)

    blockers = _dedupe_blockers(
        [
            *market.get("blockers", []),
            *wait_blockers,
            *paper.get("blockers", []),
            *live_enablement.get("blockers", []),
            *websocket.get("blockers", []),
            *database.get("blockers", []),
        ]
    )
    status = _overall_status(blockers)
    return {
        "ok": status not in {"BLOCKED"},
        "status": status,
        "ts_ms": now_ms(),
        "market_data": market,
        "wait": {
            "status": "WAIT" if wait_blockers else "CLEAR",
            "candidate_source": candidate_source,
            "candidate_symbols": candidate_symbols,
            "candidate_counts": candidate_filter.get("counts") or {},
            "candidate_gate": candidate_filter.get("gate") or {},
            "ai_wait_cooldown_count": len(candidate_filter.get("ai_wait_cooldowns") or {}),
            "blockers": wait_blockers,
        },
        "live_enablement": live_enablement,
        "paper_learning": paper,
        "codex": codex,
        "websocket": websocket,
        "database": database,
        "blockers": blockers,
        "next_actions": next_actions(blockers, live_enablement, paper, market),
    }


def market_data_status(*, warmup_started: bool = False, scan_error: str = "") -> dict[str, Any]:
    scan = _scan_status()
    refresh = scan.get("market_refresh") if isinstance(scan.get("market_refresh"), dict) else {}
    snapshot_count = _safe_int(refresh.get("snapshot_count"), int(binance_factor_source.last_snapshot_count or 0))
    effective_snapshot_count = max(snapshot_count, len(market_service.last_snapshots), len(radar_engine.top50))
    active = scan.get("active_coins") if isinstance(scan.get("active_coins"), dict) else {}
    dynamic = scan.get("dynamic_stream") if isinstance(scan.get("dynamic_stream"), dict) else {}
    blockers: list[dict[str, Any]] = []
    if warmup_started:
        blockers.append(_block("radar_scan_warming_up", "WAIT", "market_data", "Radar scan warmup has started.", "Wait for the background scan to finish."))
    if scan_error:
        blockers.append(_block(scan_error, "WAIT", "market_data", _scan_error_message(scan_error), "Wait for the background scan or click refresh again after it finishes."))
    if bool(refresh.get("degraded")):
        blockers.append(
            _block(
                "market_refresh_degraded",
                "WARN",
                "market_data",
                f"Market refresh is degraded: {refresh.get('error') or binance_factor_source.last_refresh_error or 'unknown'}",
                "Keep using cache for paper only; stabilize REST/WS before live validation.",
            )
        )
    if effective_snapshot_count <= 0:
        blockers.append(
            _block(
                "market_snapshot_empty",
                "BLOCK_PAPER_ENTRY",
                "market_data",
                "No usable radar snapshots are available yet.",
                "Run a scan and confirm Binance ticker/premium data returns rows.",
            )
        )
    if scan.get("in_progress"):
        blockers.append(_block("radar_scan_running", "WAIT", "market_data", "Radar scan is still running.", "Do not start another blocking scan."))
    return {
        "mode": settings.market_data_mode,
        "public_source": binance_rest.last_public_source,
        "refresh_source": refresh.get("source") or binance_factor_source.last_refresh_source,
        "degraded": bool(refresh.get("degraded")),
        "error": refresh.get("error") or "",
        "warning": refresh.get("warning") or getattr(binance_factor_source, "last_refresh_warning", ""),
        "scan": {
            "in_progress": bool(scan.get("in_progress")),
            "running_seconds": scan.get("running_seconds"),
            "last_duration_seconds": scan.get("last_duration_seconds"),
            "last_error": scan.get("last_error"),
            "last_scan_time": scan.get("last_scan_time"),
            "top50_count": scan.get("top50_count"),
        },
        "warmup_started": bool(warmup_started),
        "snapshot_count": snapshot_count,
        "effective_snapshot_count": effective_snapshot_count,
        "service_snapshot_count": len(market_service.last_snapshots),
        "active_coins": {
            "active_count": _safe_int(active.get("active_count")),
            "active_symbols": list(active.get("active_symbols") or [])[:50],
            "recent_removed": list(active.get("recent_removed") or [])[:10],
        },
        "dynamic_stream": {
            "active_count": _safe_int(dynamic.get("active_count")),
            "running": bool(dynamic.get("running")),
            "last_error": str(dynamic.get("last_error") or ""),
        },
        "blockers": blockers,
    }


def wait_blockers_for(
    *,
    market: dict[str, Any],
    candidate_filter: dict[str, Any],
    candidate_symbols: list[str],
    loop_ok: bool,
    loop_reason: str,
    ai_status: dict[str, Any],
    codex: dict[str, Any],
) -> list[dict[str, Any]]:
    blockers: list[dict[str, Any]] = []
    counts = candidate_filter.get("counts") if isinstance(candidate_filter.get("counts"), dict) else {}
    if market.get("effective_snapshot_count", 0) <= 0:
        blockers.append(_block("wait_no_market_snapshot", "WAIT", "wait", "WAIT because market snapshots are empty.", "Let the radar warm up first."))
    if not candidate_symbols:
        blockers.append(
            _block(
                "candidate_filter_empty",
                "WAIT",
                "wait",
                "No symbol passed the current candidate gate.",
                "Check score/fund/wick rejection counts before changing thresholds.",
                {"counts": counts},
            )
        )
    if not loop_ok:
        blockers.append(
            _block(
                "loop_start_blocked",
                "BLOCK_PAPER_ENTRY",
                "wait",
                f"Auto loop start guard is blocking: {loop_reason or 'unknown'}",
                "Fix the guard reason before expecting automatic paper/live entries.",
            )
        )
    not_invoked = str(ai_status.get("not_invoked_reason") or "")
    if not_invoked:
        blockers.append(
            _block(
                "ai_not_invoked",
                "WAIT",
                "wait",
                f"AI strategy was not invoked: {not_invoked}",
                "This is normal only when there is no candidate or capacity is full.",
            )
        )
    if _codex_required() and not _codex_generation_ready(codex):
        reason = _codex_unavailable_reason(codex)
        blockers.append(
            _block(
                reason,
                "BLOCK_PAPER_ENTRY",
                "codex",
                f"Codex is required for entry but is not generation-ready: {reason}.",
                "Install/fix Codex CLI, mount Codex auth, provide OPENAI_API_KEY, or disable the Codex-required gate only for controlled paper tests.",
            )
        )
    cooldowns = candidate_filter.get("ai_wait_cooldowns") if isinstance(candidate_filter.get("ai_wait_cooldowns"), dict) else {}
    if cooldowns:
        blockers.append(
            _block(
                "ai_wait_cooldowns_active",
                "WAIT",
                "wait",
                f"{len(cooldowns)} symbols are cooling down after AI WAIT/stale decisions.",
                "Let cooldowns expire or inspect the latest AI WAIT reason.",
            )
        )
    return _dedupe_blockers(blockers)


def paper_learning_status(
    *,
    performance: dict[str, Any],
    loop_ok: bool,
    loop_reason: str,
    loop_performance: dict[str, Any],
    candidate_filter: dict[str, Any],
    candidate_symbols: list[str],
    ai_status: dict[str, Any],
    codex: dict[str, Any],
    data_quality: dict[str, Any],
    attribution: dict[str, Any],
    feedback: dict[str, Any],
) -> dict[str, Any]:
    counts = candidate_filter.get("counts") if isinstance(candidate_filter.get("counts"), dict) else {}
    closed_loop_enabled = bool(not (settings.trade_mode == "live" and settings.live_trading_enabled))
    blockers: list[dict[str, Any]] = []
    if not settings.paper_probe_enabled:
        blockers.append(_block("paper_probe_disabled", "BLOCK_PAPER_ENTRY", "paper_learning", "Paper probe is disabled.", "Enable paper probe before expecting a learning loop."))
    if not closed_loop_enabled:
        blockers.append(_block("paper_closed_loop_disabled", "BLOCK_PAPER_ENTRY", "paper_learning", "Real live mode is enabled, so paper closed loop is not active.", "Disable real live trading before paper repair/sampling."))
    if not loop_ok:
        blockers.append(_block("paper_loop_guard_blocked", "BLOCK_PAPER_ENTRY", "paper_learning", f"Paper loop guard is blocking: {loop_reason or 'unknown'}", "Fix loop guard settings first."))
    if not candidate_symbols and _safe_int(counts.get("paper_probe_candidates")) <= 0:
        blockers.append(_block("paper_candidate_empty", "WAIT", "paper_learning", "Paper loop has no candidate to sample.", "Keep scanning; do not open random symbols just to create samples."))
    if _codex_required() and not _codex_generation_ready(codex):
        reason = _codex_unavailable_reason(codex)
        blockers.append(_block("codex_required_for_paper_entry", "BLOCK_PAPER_ENTRY", "paper_learning", f"Codex-required entry cannot open paper samples until Codex is generation-ready: {reason}.", "Fix Codex CLI/auth or run a deliberate paper-only rule fallback test."))
    if bool(performance.get("recovery_mode")):
        blockers.append(_block("performance_recovery_mode", "WARN", "paper_learning", "Performance guard is in recovery mode.", "Paper can still sample, but live graduation stays blocked."))
    if not bool(data_quality.get("production_grade")):
        blockers.append(_block("learning_data_not_production_grade", "WARN", "paper_learning", "Learning data is not production-grade yet.", "Use it as feedback, not as live approval."))
    graduation_progress = paper_graduation_progress(data_quality)
    return {
        "closed_loop_enabled": closed_loop_enabled,
        "auto_loop_enabled": bool(autotrader.enabled),
        "paper_probe_enabled": bool(settings.paper_probe_enabled),
        "candidate_mode": settings.auto_trading_candidate_mode,
        "candidate_symbols": candidate_symbols,
        "candidate_counts": counts,
        "loop_start_guard": {
            "ok": bool(loop_ok),
            "reason": loop_reason,
            "performance": loop_performance,
        },
        "ai": {
            "enabled": bool(ai_status.get("enabled")),
            "provider": ai_status.get("provider"),
            "will_invoke_for_current_candidates": bool(ai_status.get("will_invoke_for_current_candidates")),
            "not_invoked_reason": ai_status.get("not_invoked_reason"),
        },
        "performance": {
            "trades": performance.get("trades"),
            "win_rate": performance.get("win_rate"),
            "recent_win_rate": performance.get("recent_win_rate"),
            "pnl": performance.get("pnl"),
            "loss_streak": performance.get("loss_streak"),
            "recovery_mode": performance.get("recovery_mode"),
        },
        "attribution": {
            "sample_count": attribution.get("sample_count"),
            "global_win_rate": attribution.get("global_win_rate"),
            "global_profit_factor": attribution.get("global_profit_factor"),
            "global_pnl": attribution.get("global_pnl"),
        },
        "learning_data_quality": {
            "trust_level": data_quality.get("trust_level"),
            "production_grade": bool(data_quality.get("production_grade")),
            "can_hard_block_from_learning": bool(data_quality.get("can_hard_block_from_learning")),
            "reasons": list(data_quality.get("reasons") or [])[:8],
        },
        "graduation_progress": graduation_progress,
        "strategy_feedback": {
            "tracked_strategies": feedback.get("tracked_strategies"),
            "closed_samples": feedback.get("closed_samples"),
            "pnl": feedback.get("pnl"),
            "win_rate": feedback.get("win_rate"),
        },
        "blockers": _dedupe_blockers(blockers),
    }


def paper_graduation_progress(data_quality: dict[str, Any]) -> dict[str, Any]:
    minimums = data_quality.get("minimums") if isinstance(data_quality.get("minimums"), dict) else {}
    sources = data_quality.get("sources") if isinstance(data_quality.get("sources"), dict) else {}
    market_backtest = data_quality.get("market_backtest") if isinstance(data_quality.get("market_backtest"), dict) else {}
    radar = data_quality.get("radar_snapshots") if isinstance(data_quality.get("radar_snapshots"), dict) else {}
    real_closed = _safe_int(sources.get("real_closed_samples_with_radar"))
    min_real_closed = max(1, _safe_int(minimums.get("real_closed_samples"), 30))
    missing_real_closed = max(0, min_real_closed - real_closed)
    market_available = bool(market_backtest.get("available"))
    market_passed = bool(market_backtest.get("quality_passed"))
    radar_days = _safe_float(radar.get("span_days"))
    min_radar_days = _safe_float(minimums.get("radar_history_days"), 14.0)
    production_grade = bool(data_quality.get("production_grade"))
    if production_grade:
        next_requirement = "Production-grade learning evidence is available."
    elif market_available and not market_passed:
        next_requirement = "Repair or regenerate the market backtest until quality gates pass."
    elif missing_real_closed > 0:
        next_requirement = (
            f"Collect {missing_real_closed} more real closed paper/shadow samples with radar context "
            "or provide a passing market backtest."
        )
    else:
        next_requirement = "Provide a passing market backtest or extend radar history until the production gate clears."
    return {
        "production_grade": production_grade,
        "trust_level": data_quality.get("trust_level"),
        "real_closed_samples_with_radar": real_closed,
        "minimum_real_closed_samples": min_real_closed,
        "missing_real_closed_samples": missing_real_closed,
        "combined_samples": _safe_int(sources.get("combined_samples")),
        "replay_samples": _safe_int(sources.get("replay_samples")),
        "replay_ratio": _safe_float(sources.get("replay_ratio")),
        "market_backtest_available": market_available,
        "market_backtest_quality_passed": market_passed,
        "radar_history_days": radar_days,
        "minimum_radar_history_days": min_radar_days,
        "reasons": list(data_quality.get("reasons") or [])[:8],
        "next_requirement": next_requirement,
    }


def live_enablement_status(live: dict[str, Any]) -> dict[str, Any]:
    blockers = [
        _block(
            str(block.get("code") or "live_blocker"),
            "BLOCK_LIVE",
            "live_enablement",
            str(block.get("message") or ""),
            _live_action(str(block.get("code") or "")),
            {"stage": block.get("stage")},
        )
        for block in (live.get("blockers") or [])
        if isinstance(block, dict)
    ]
    phases = []
    for phase in live.get("phases") or []:
        if not isinstance(phase, dict):
            continue
        phases.append(
            {
                "name": phase.get("name"),
                "allowed": bool(phase.get("allowed")),
                "requires_manual_approval": bool(phase.get("requires_manual_approval")),
                "blocker_count": len(phase.get("blockers") or []),
                "purpose": phase.get("purpose"),
            }
        )
    return {
        "current_stage": live.get("current_stage"),
        "paper_is_terminal": bool(live.get("paper_is_terminal")),
        "phases": phases,
        "switches": {
            "trade_mode": settings.trade_mode,
            "live_trading_enabled": bool(settings.live_trading_enabled),
            "live_use_test_order": bool(settings.live_use_test_order),
            "attach_protection_orders": bool(settings.attach_protection_orders),
            "binance_configured": bool(binance_futures.configured()),
            "max_open_positions": settings.max_open_positions,
            "performance_guard_enabled": bool(settings.auto_trading_use_performance_guard),
        },
        "metrics": live.get("metrics") or {},
        "blockers": blockers,
        "next_actions": list(live.get("next_actions") or [])[:8],
    }


def codex_status(ai_status: dict[str, Any]) -> dict[str, Any]:
    codex = ai_status.get("codex_cli") if isinstance(ai_status.get("codex_cli"), dict) else {}
    provider = str(ai_status.get("provider") or "").strip().lower()
    ready_for_generation = _codex_generation_ready(codex)
    codex_will_invoke = bool(
        provider == "codex_cli"
        and ai_status.get("will_invoke_for_current_candidates")
        and ready_for_generation
    )
    if provider != "codex_cli":
        not_invoked_reason = f"provider_{provider or 'unknown'}_not_codex"
    elif not ready_for_generation:
        not_invoked_reason = _codex_unavailable_reason(codex)
    else:
        not_invoked_reason = ai_status.get("not_invoked_reason")
    return {
        "required_for_entry": _codex_required(),
        "ai_enabled": bool(ai_status.get("enabled")),
        "provider": ai_status.get("provider"),
        "command_found": bool(codex.get("command_found")),
        "ready_for_generation": ready_for_generation,
        "availability_reason": codex.get("availability_reason"),
        "auth_required": codex.get("auth_required"),
        "auth_available": codex.get("auth_available"),
        "auth_source": codex.get("auth_source"),
        "codex_home": codex.get("codex_home"),
        "auth_json_exists": codex.get("auth_json_exists"),
        "model": codex.get("model"),
        "timeout_seconds": codex.get("timeout_seconds"),
        "reasoning_effort": codex.get("reasoning_effort"),
        "service_tier": codex.get("service_tier"),
        "schema_exists": codex.get("schema_exists"),
        "invocation_count": codex.get("invocation_count"),
        "last_status": codex.get("last_status"),
        "last_error": codex.get("last_error"),
        "last_symbol": codex.get("last_symbol"),
        "last_action": codex.get("last_action"),
        "will_invoke_for_current_candidates": codex_will_invoke,
        "not_invoked_reason": not_invoked_reason,
    }


def _codex_generation_ready(codex: dict[str, Any]) -> bool:
    if "ready_for_generation" in codex:
        return bool(codex.get("ready_for_generation"))
    return bool(codex.get("command_found"))


def _codex_unavailable_reason(codex: dict[str, Any]) -> str:
    reason = str(codex.get("availability_reason") or "").strip()
    if reason:
        return reason
    if not codex.get("command_found"):
        return "codex_command_missing"
    return "codex_unavailable"


def websocket_status() -> dict[str, Any]:
    ticker = _safe_dict(binance_ticker_stream.diagnostics) if hasattr(binance_ticker_stream, "diagnostics") else _ticker_fallback()
    dynamic = _safe_dict(dynamic_symbol_stream.diagnostics)
    compact_dynamic = {
        "running": bool(dynamic.get("running")),
        "active_count": _safe_int(dynamic.get("active_count")),
        "active_symbols": list(dynamic.get("active_symbols") or [])[:50],
        "streams": list(dynamic.get("streams") or []),
        "last_message_age_seconds": dynamic.get("last_message_age_seconds"),
        "last_error": str(dynamic.get("last_error") or ""),
        "version": dynamic.get("version"),
    }
    blockers: list[dict[str, Any]] = []
    if settings.market_data_mode.lower() == "binance" and settings.binance_ws_enabled:
        if not ticker.get("running"):
            blockers.append(_block("ticker_ws_not_running", "WARN", "websocket", "All-market ticker WebSocket is not running.", "Restart the app or check Binance WS connectivity."))
        if compact_dynamic["active_count"] > 0 and not compact_dynamic["running"]:
            blockers.append(_block("dynamic_ws_not_running", "WARN", "websocket", "Dynamic symbol WebSocket has active symbols but is not running.", "Restart the stream or run a fresh scan."))
        if ticker.get("stale"):
            blockers.append(_block("ticker_ws_stale", "WARN", "websocket", "Ticker WebSocket data is stale.", "REST cache may be used until WS recovers."))
        if compact_dynamic.get("last_error"):
            blockers.append(_block("dynamic_ws_error", "WARN", "websocket", f"Dynamic WS error: {compact_dynamic.get('last_error')}", "Check network/proxy and Binance WS URL."))
    return {
        "enabled": bool(settings.binance_ws_enabled),
        "ticker": ticker,
        "dynamic": compact_dynamic,
        "blockers": blockers,
    }


def database_health(database: DB = db) -> dict[str, Any]:
    path = database.path
    tables: dict[str, int] = {}
    try:
        with database.conn() as conn:
            conn.execute("SELECT 1").fetchone()
            for table in DB_TABLES:
                try:
                    row = conn.execute(f"SELECT COUNT(*) AS count FROM {table}").fetchone()
                    tables[table] = int(row["count"] if isinstance(row, dict) else row[0])
                except Exception:
                    tables[table] = -1
            radar_last = conn.execute("SELECT MAX(ts_ms) FROM radar_snapshots").fetchone()[0]
            closed_last = conn.execute("SELECT MAX(close_time) FROM closed_positions").fetchone()[0]
    except Exception as exc:
        return {
            "ok": False,
            "path": str(path),
            "exists": path.exists(),
            "error": f"{type(exc).__name__}:{exc}",
            "tables": tables,
            "blockers": [_block("database_unavailable", "BLOCK_SYSTEM", "database", f"Database health check failed: {type(exc).__name__}", "Fix SQLite path/permissions before trusting learning or positions.")],
        }
    return {
        "ok": True,
        "path": str(path),
        "exists": path.exists(),
        "size_bytes": path.stat().st_size if path.exists() else 0,
        "tables": tables,
        "last_radar_ts_ms": _safe_int(radar_last),
        "last_closed_position_ts_ms": _safe_int(closed_last),
        "blockers": [],
    }


def next_actions(
    blockers: list[dict[str, Any]],
    live_enablement: dict[str, Any],
    paper: dict[str, Any],
    market: dict[str, Any],
) -> list[str]:
    codes = {str(blocker.get("code") or "") for blocker in blockers}
    actions: list[str] = []
    if {"market_snapshot_empty", "radar_scan_warming_up", "ticker_ws_not_running", "market_refresh_degraded"} & codes:
        actions.append("First stabilize the market feed: run scan, check REST/WS errors, and confirm snapshot_count is above 0.")
    if {"candidate_filter_empty", "paper_candidate_empty"} & codes:
        actions.append("Then inspect candidate rejection counts; do not reduce the radar universe just to hide weak signals.")
    if {"codex_command_missing", "codex_auth_missing", "codex_unavailable", "codex_required_for_paper_entry"} & codes:
        actions.append("Fix Codex CLI/auth availability before expecting AI-generated paper entries.")
    if paper.get("closed_loop_enabled") and not paper.get("auto_loop_enabled"):
        actions.append("Paper learning loop is configured but auto loop is off; start it only after the readiness blockers make sense.")
    live_actions = live_enablement.get("next_actions") or []
    actions.extend(str(action) for action in live_actions[:3])
    if not actions:
        actions.append("Readiness is clear enough for paper observation; live still requires explicit staged approval.")
    return actions[:8]


def _scan_status() -> dict[str, Any]:
    try:
        return radar_engine.scan_status(compact=True)
    except TypeError:
        return radar_engine.scan_status()
    except Exception as exc:
        return {"last_error": f"{type(exc).__name__}:{exc}", "top50_count": len(radar_engine.top50)}


def _loop_guard() -> tuple[bool, str, dict[str, Any]]:
    try:
        ok, reason, performance = autotrader.loop_start_guard()
        return bool(ok), str(reason or ""), dict(performance or {})
    except Exception as exc:
        return False, f"{type(exc).__name__}:{exc}", {}


def _safe_dict(fn) -> dict[str, Any]:
    try:
        value = fn()
        return dict(value or {}) if isinstance(value, dict) else {}
    except Exception as exc:
        return {"error": f"{type(exc).__name__}:{exc}"}


def _ticker_fallback() -> dict[str, Any]:
    task = getattr(binance_ticker_stream, "_task", None)
    tickers = getattr(binance_ticker_stream, "_tickers", {}) or {}
    return {
        "running": bool(task and not task.done()),
        "ticker_count": len(tickers),
        "stale": True,
        "last_message_age_seconds": None,
        "last_error": "",
        "custom_url_configured": bool(settings.binance_ws_url),
    }


def _block(
    code: str,
    severity: str,
    source: str,
    message: str,
    action: str = "",
    meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    out = {
        "code": code,
        "severity": severity,
        "source": source,
        "message": message,
        "action": action,
    }
    if meta:
        out["meta"] = meta
    return out


def _dedupe_blockers(blockers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[str, str]] = set()
    out: list[dict[str, Any]] = []
    for blocker in blockers:
        if not isinstance(blocker, dict):
            continue
        key = (str(blocker.get("source") or ""), str(blocker.get("code") or ""))
        if key in seen:
            continue
        seen.add(key)
        out.append(blocker)
    return out


def _overall_status(blockers: list[dict[str, Any]]) -> str:
    severities = {str(blocker.get("severity") or "") for blocker in blockers}
    if {"BLOCK_SYSTEM", "BLOCK_PAPER_ENTRY"} & severities:
        return "BLOCKED"
    if severities:
        return "DEGRADED"
    return "OK"


def _codex_required() -> bool:
    return bool(settings.require_codex_strategy_for_entry or settings.ai_strategy_provider == "codex_cli")


def _live_action(code: str) -> str:
    if code in {"market_refresh_degraded", "market_refresh_missing", "market_source_not_mainnet"}:
        return "Stabilize mainnet market data before live validation."
    if code in {"learning_samples_low", "learning_data_not_production_grade"}:
        return "Collect real closed paper/shadow samples before live validation."
    if code in {"trade_mode_not_live", "binance_keys_missing"}:
        return "Configure exchange validation deliberately; do not auto-enable real orders."
    if code == "open_position_exists":
        return "Wait until current position is closed and reconciled."
    return "Clear this blocker before moving to the next live stage."


def _scan_error_message(scan_error: str) -> str:
    if scan_error == "radar_scan_warming_up":
        return "Radar cache is empty, background scan has been started."
    if scan_error == "radar_scan_running_no_cache":
        return "Radar scan is already running and there is no cache yet."
    return scan_error


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except Exception:
        return default


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default
