from __future__ import annotations
import asyncio
import json
import math
import threading
from typing import Any
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
import uvicorn
from backend.config import settings
from backend.config_env import update_env_values
from backend.ai_strategy.ai_service import ai_service
from backend.ai_strategy.position_policy_client import ai_position_policy_client
from backend.learning.strategy_evolver import strategy_evolver
from backend.learning.strategy_registry import strategy_registry
from backend.learning.ai_strategy_feedback import ai_strategy_feedback
from backend.learning.trade_memory import trade_memory
from backend.learning.replay_memory import replay_memory
from backend.learning.event_calibrator import event_calibrator
from backend.learning.learned_risk_guard import learned_risk_guard
from backend.learning.learning_data_audit import learning_data_audit
from backend.learning.radar_score_auditor import radar_score_auditor
from backend.learning.radar_weight_calibrator import radar_weight_calibrator
from backend.learning.trade_attributor import trade_attributor
from backend.ai_strategy.strategy_qa import strategy_qa
from backend.strategy_alpha.service import run_strategy_alpha_cycle, strategy_alpha_status
from backend.radar.radar_engine import radar_engine
from backend.positions.position_manager import position_manager
from backend.positions.position_registry import position_registry
from backend.trading.ai_trade_director import ai_trade_director
from backend.trading.autotrader import autotrader
from backend.trading.exchange_reconciliation import exchange_reconciliation
from backend.trading.live_executor import live_executor
from backend.trading.live_readiness import live_readiness
from backend.trading.performance_guard import performance_guard
from backend.trading.production_acceptance import production_acceptance_runner
from backend.trading.trade_acceptance import trade_acceptance_runner
from backend.account.account_service import account_service
from backend.market.binance_rest import binance_rest
from backend.market.market_service import market_service
from backend.market.binance_ws_ticker import binance_ticker_stream
from backend.market.dynamic_symbol_stream import dynamic_symbol_stream
from backend.exchange.binance_futures import binance_futures
from backend.system_readiness import system_readiness_report
from backend.radar.universal_anomaly_trainer import universal_anomaly_trainer
from backend.radar.universal_anomaly_training import universal_anomaly_training
from backend.radar.universal_anomaly_auto_trainer import run_auto_train_loop, universal_anomaly_auto_trainer
from backend.radar.universal_anomaly_calibration import universal_anomaly_sample_calibrator
from backend.app.db.session import init_db


class MainnetConfigRequest(BaseModel):
    api_key: str
    api_secret: str


class CodexStrategyConfigRequest(BaseModel):
    pass


class EvolveRequest(BaseModel):
    use_codex: bool = False
    promote: bool = False


class PaperRepairRequest(BaseModel):
    promote: bool = False
    run_once: bool = True


class StrategyAlphaRunRequest(BaseModel):
    generation_size: int = 20
    mutation_size: int = 5


class StrategyAskRequest(BaseModel):
    question: str


class ProductionAcceptanceRequest(BaseModel):
    mode: str = "preflight"
    confirm_real_order: str = ""
    manage_seconds: int = 0


class AutoTradeParamsRequest(BaseModel):
    auto_trading_candidate_mode: str
    auto_trading_candidate_min_score: float
    auto_trading_candidate_limit: int
    auto_trading_use_active_strategy_filter: bool
    auto_trading_use_performance_guard: bool
    paper_account_equity_usdt: float
    max_open_positions: int
    trade_target_margin_pct: float
    trade_max_margin_pct: float
    trade_max_risk_pct: float
    trade_min_net_profit_usdt: float
    trade_min_profit_cost_ratio: float
    trade_min_margin_usdt: float
    trade_min_notional_usdt: float
    trade_reserved_balance_pct: float
    strategy_min_paper_win_rate: float
    strategy_min_paper_confidence: float
    strategy_min_expected_r: float
    strategy_min_tp2_r: float

app = FastAPI(title="猎妖人 AI Radar Full Source")
app.mount("/static", StaticFiles(directory="backend/web/static"), name="static")
templates = Jinja2Templates(directory="backend/web/templates")

RADAR_API_SCAN_TIMEOUT_SECONDS = 75.0
RADAR_DIAGNOSTICS_SCAN_TIMEOUT_SECONDS = 12.0
_universal_anomaly_auto_train_thread: threading.Thread | None = None
API_WRITE_AUTH_EXEMPT_PATHS = {
    "/api/radar/scan-now",
}


@app.middleware("http")
async def api_write_auth(request: Request, call_next):
    if (
        request.url.path.startswith("/api/")
        and request.url.path not in API_WRITE_AUTH_EXEMPT_PATHS
        and request.method.upper() in {"POST", "PUT", "PATCH", "DELETE"}
    ):
        expected = str(settings.api_token or "").strip()
        if not expected:
            return JSONResponse({"detail": "api_token_not_configured"}, status_code=503)
        supplied = str(request.headers.get("X-API-Token") or request.query_params.get("api_token") or "").strip()
        if supplied != expected:
            return JSONResponse({"detail": "invalid_api_token"}, status_code=401)
    return await call_next(request)


def _consume_radar_scan_result(task: asyncio.Task) -> None:
    try:
        task.result()
    except asyncio.CancelledError:
        pass
    except Exception as exc:
        print("radar scan task error", repr(exc))


async def _radar_scan_offloop(*, force_refresh: bool = False):
    return await radar_engine.scan(force_refresh=force_refresh)


def _start_radar_scan_background(*, force_refresh: bool = False) -> bool:
    if radar_engine.scan_in_progress():
        return False
    task = asyncio.create_task(_radar_scan_offloop(force_refresh=force_refresh), name="radar-scan-background")
    task.add_done_callback(_consume_radar_scan_result)
    return True


async def _radar_scan_with_timeout(*, force_refresh: bool = False, timeout_seconds: float = RADAR_API_SCAN_TIMEOUT_SECONDS):
    task = asyncio.create_task(_radar_scan_offloop(force_refresh=force_refresh), name="radar-scan-request")
    try:
        return await asyncio.wait_for(
            asyncio.shield(task),
            timeout=max(1.0, float(timeout_seconds)),
        )
    except asyncio.TimeoutError:
        task.add_done_callback(_consume_radar_scan_result)
        raise


def _compact_active_coins(status: dict) -> dict:
    active = status.get("active_coins") if isinstance(status.get("active_coins"), dict) else {}
    return {
        "active_count": int(active.get("active_count") or 0),
        "active_symbols": list(active.get("active_symbols") or [])[:200],
        "recent_removed": list(active.get("recent_removed") or [])[:20],
    }


def _compact_dynamic_stream(status: dict) -> dict:
    stream = status.get("dynamic_stream") if isinstance(status.get("dynamic_stream"), dict) else {}
    return {
        "active_count": int(stream.get("active_count") or 0),
        "running": bool(stream.get("running")),
        "last_error": str(stream.get("last_error") or ""),
    }


def _compact_scan_status(status: dict) -> dict:
    out = dict(status or {})
    out["active_coins"] = _compact_active_coins(out)
    out["dynamic_stream"] = _compact_dynamic_stream(out)
    return out


def _radar_api_scan_status() -> dict:
    try:
        return _compact_scan_status(radar_engine.scan_status(compact=True))
    except TypeError:
        return _compact_scan_status(radar_engine.scan_status())


def _items_by_symbols(items: list[Any], symbols: list[str]) -> list[Any]:
    by_symbol = {str(getattr(item, "symbol", "") or ""): item for item in items}
    return [by_symbol[symbol] for symbol in symbols if symbol in by_symbol]


def _optional_float(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return int(default)


def _state_market_data(scan_status: dict[str, Any]) -> dict[str, Any]:
    refresh = scan_status.get("market_refresh") if isinstance(scan_status.get("market_refresh"), dict) else {}
    active = scan_status.get("active_coins") if isinstance(scan_status.get("active_coins"), dict) else {}
    dynamic = scan_status.get("dynamic_stream") if isinstance(scan_status.get("dynamic_stream"), dict) else {}
    snapshot_count = _safe_int(refresh.get("snapshot_count"), len(market_service.last_snapshots))
    top50_count = _safe_int(scan_status.get("top50_count"), len(radar_engine.top50))
    return {
        "mode": settings.market_data_mode,
        "public_source": binance_rest.last_public_source,
        "refresh_source": str(refresh.get("source") or ""),
        "degraded": bool(refresh.get("degraded")),
        "error": str(refresh.get("error") or ""),
        "warning": str(refresh.get("warning") or ""),
        "snapshot_count": snapshot_count,
        "symbol_count": _safe_int(refresh.get("symbol_count"), snapshot_count),
        "top50_count": top50_count,
        "active_coin_count": _safe_int(active.get("active_count")),
        "active_symbols": list(active.get("active_symbols") or [])[:50],
        "dynamic_stream_count": _safe_int(dynamic.get("active_count")),
        "dynamic_stream_running": bool(dynamic.get("running")),
        "dynamic_stream_error": str(dynamic.get("last_error") or ""),
    }


def _ws_ticker_rows_by_symbol(symbols: list[str]) -> dict[str, dict[str, Any]]:
    wanted = {str(symbol or "").upper() for symbol in symbols if symbol}
    rows: dict[str, dict[str, Any]] = {}
    try:
        ticker_rows = binance_ticker_stream.snapshot_rows()
    except Exception:
        return rows
    for row in ticker_rows:
        if not isinstance(row, dict):
            continue
        symbol = str(row.get("symbol") or "").upper()
        if symbol in wanted:
            rows[symbol] = row
    return rows


def _ticker_last_price(row: dict[str, Any]) -> float:
    if not isinstance(row, dict):
        return 0.0
    return _optional_float(row.get("lastPrice")) or 0.0


def _light_trade_director_status(
    *,
    candidate_source: str,
    candidate_symbols: list[str],
    performance: dict[str, Any],
    loop_ok: bool,
    loop_reason: str,
    loop_performance: dict[str, Any],
) -> dict[str, Any]:
    readiness = live_readiness.summary()
    return {
        "stage": "status_light",
        "source": "autotrade_diagnostics",
        "candidate_source": candidate_source,
        "candidate_symbols": candidate_symbols,
        "candidate_lock": autotrader.candidate_lock_status(),
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
        "live_readiness": {
            "current_stage": readiness.get("current_stage"),
            "blockers": readiness.get("blockers", [])[:8],
        },
        "safety": {
            "trade_mode": settings.trade_mode,
            "live_trading_enabled": bool(settings.live_trading_enabled),
            "real_order_allowed": bool(settings.trade_mode == "live" and settings.live_trading_enabled),
        },
    }


def start_universal_anomaly_auto_train_thread() -> threading.Thread | None:
    global _universal_anomaly_auto_train_thread
    if not settings.universal_anomaly_auto_train_enabled:
        return None
    if _universal_anomaly_auto_train_thread and _universal_anomaly_auto_train_thread.is_alive():
        return _universal_anomaly_auto_train_thread
    thread = threading.Thread(
        target=run_auto_train_loop,
        name="universal-anomaly-auto-train",
        daemon=True,
    )
    thread.start()
    _universal_anomaly_auto_train_thread = thread
    return thread

@app.on_event("startup")
async def startup():
    init_db()
    try:
        if universal_anomaly_trainer.artifact_path.exists():
            universal_anomaly_trainer.activate_latest()
    except Exception:
        pass
    start_universal_anomaly_auto_train_thread()
    if settings.market_data_mode.lower() == "binance" and settings.binance_ws_enabled:
        binance_ticker_stream.start()
        dynamic_symbol_stream.start()
    asyncio.create_task(background_loop())
    asyncio.create_task(position_manage_loop())

async def background_loop():
    await asyncio.sleep(max(1, settings.scan_interval_seconds))
    while True:
        try:
            await _radar_scan_offloop()
            if autotrader.enabled:
                await ai_trade_director.run_once(source="auto_loop")
        except Exception as e:
            print("background error", repr(e))
        await asyncio.sleep(settings.scan_interval_seconds)

async def position_manage_loop():
    await asyncio.sleep(max(1, settings.position_manage_interval_seconds))
    while True:
        try:
            await position_manager.manage_all()
            await exchange_reconciliation.maybe_refresh(min_interval_seconds=30)
        except Exception as e:
            print("position manager error", repr(e))
        await asyncio.sleep(max(1, settings.position_manage_interval_seconds))

@app.get("/")
async def root(): return RedirectResponse("/radar")
@app.get("/dashboard")
async def dashboard(request: Request): return templates.TemplateResponse(request, "dashboard.html")
@app.get("/radar")
async def radar_page(request: Request): return templates.TemplateResponse(request, "radar.html")
@app.get("/positions")
async def positions_page(request: Request): return templates.TemplateResponse(request, "positions.html")
@app.get("/settings")
async def settings_page(request: Request): return templates.TemplateResponse(request, "settings.html")
@app.get("/strategy-ai")
async def strategy_ai_page(request: Request):
    question = (request.query_params.get("q") or "").strip()
    answer = ""
    status = "等待提问"
    context_json = "{}"
    if question:
        status = "策略 AI 已返回"
        out = await strategy_qa.ask(question)
        if out.get("ok"):
            answer = str(out.get("answer") or "")
            context_json = json.dumps(out.get("context") or {}, ensure_ascii=False, indent=2)
            status = (
                f"provider={out.get('provider', '--')} | model={out.get('model', '--')} | "
                f"reasoning={out.get('reasoning_effort', '--')} | tier={out.get('service_tier', '--')}"
            )
            if out.get("warning"):
                status += f" | warning={out.get('warning')}"
        else:
            answer = f"策略 AI 调用失败：{out.get('error') or out.get('message') or 'unknown_error'}"
            context_json = json.dumps(out.get("context") or {}, ensure_ascii=False, indent=2)
            status = "调用失败"
    return templates.TemplateResponse(
        request,
        "strategy_ai.html",
        {
            "question": question,
            "answer": answer,
            "status": status,
            "context_json": context_json,
        },
    )

async def _state_major_rows() -> list[dict[str, Any]]:
    specs = [
        ("BTCUSDT", "BTC 永续"),
        ("ETHUSDT", "ETH 永续"),
        ("BNBUSDT", "BNB 永续"),
        ("SOLUSDT", "SOL 永续"),
    ]
    ticker_rows = _ws_ticker_rows_by_symbol([sym for sym, _ in specs])
    rows = []
    for sym, label in specs:
        snapshot = market_service.last_snapshots.get(sym)
        ticker_row = ticker_rows.get(sym) or {}
        cached_quote = market_service.cached_price_quote(sym)
        ticker_price = _ticker_last_price(ticker_row)
        cached_price = float(getattr(cached_quote, "price", 0.0) or 0.0)
        snapshot_price = snapshot.price if snapshot and snapshot.price > 0 else 0.0
        change_5m = _optional_float(getattr(snapshot, "change_5m", None)) if snapshot else None
        change_24h = _optional_float(ticker_row.get("priceChangePercent"))
        quote_volume_24h = _optional_float(ticker_row.get("quoteVolume"))
        if ticker_price > 0:
            price = ticker_price
            source = "ws_ticker_last_price"
            stale = False
            error = ""
            price_age_seconds = 0.0
        else:
            price = cached_price if cached_price > 0 else snapshot_price
            source = getattr(cached_quote, "source", "snapshot_cache" if snapshot else "unavailable")
            stale = bool(getattr(cached_quote, "stale", True))
            error = str(getattr(cached_quote, "error", "") or "")
            price_age_seconds = float(getattr(cached_quote, "age_seconds", 999999.0) or 999999.0)
        if change_5m is not None:
            change = change_5m
            change_source = "snapshot_5m"
            change_label = "5m"
        elif change_24h is not None:
            change = change_24h
            change_source = "ws_ticker_24h"
            change_label = "24h"
        else:
            change = 0.0
            change_source = "unavailable"
            change_label = ""
        rows.append({
            "symbol": sym,
            "label": label,
            "price": price,
            "change": change,
            "change_5m": change_5m,
            "change_24h": change_24h,
            "change_source": change_source,
            "change_label": change_label,
            "quote_volume_24h": quote_volume_24h,
            "source": source,
            "stale": stale,
            "error": error,
            "bid": float(getattr(cached_quote, "bid", 0.0) or 0.0),
            "ask": float(getattr(cached_quote, "ask", 0.0) or 0.0),
            "price_age_seconds": price_age_seconds,
        })
    return rows


@app.get("/api/state")
async def state():
    majors = await _state_major_rows()
    scan_status = _radar_api_scan_status()
    market_data = _state_market_data(scan_status)
    return {
        "last_scan_time":radar_engine.last_scan_time,
        "market_heat":radar_engine.market_heat,
        "alert_count":radar_engine.alert_count,
        "major":majors,
        "major_markets":majors,
        "auto_enabled":autotrader.enabled,
        "market_data_source":binance_rest.last_public_source,
        "scan_status":scan_status,
        "market_data":market_data,
        "top50_count":market_data["top50_count"],
    }


@app.get("/api/health")
async def api_health():
    return {"ok": True, "service": "ai-radar-monitor", "version": "legacy"}

@app.get("/api/radar")
async def api_radar():
    scan_error = ""
    if not radar_engine.top50:
        if radar_engine.scan_in_progress():
            scan_error = "radar_scan_running_no_cache"
        else:
            _start_radar_scan_background()
            scan_error = "radar_scan_warming_up"
    top_confirmed = [x.asdict() for x in radar_engine.top4]
    scan_status = _radar_api_scan_status()
    return {
        "ok": not bool(scan_error),
        "error": scan_error,
        "top50":[x.asdict() for x in radar_engine.top50],
        "top4":top_confirmed,
        "top5_confirmed":top_confirmed,
        "trade_top5": top_confirmed,
        "last_scan_id":radar_engine.last_scan_id,
        "scan_status": scan_status,
        "active_coins": scan_status.get("active_coins", {}),
        "dynamic_stream": scan_status.get("dynamic_stream", {}),
    }

@app.get("/api/system/readiness")
async def api_system_readiness():
    scan_error = ""
    warmup_started = False
    if not radar_engine.top50:
        if radar_engine.scan_in_progress():
            scan_error = "radar_scan_running_no_cache"
        else:
            warmup_started = _start_radar_scan_background()
            scan_error = "radar_scan_warming_up"
    return system_readiness_report(warmup_started=warmup_started, scan_error=scan_error)

@app.post("/api/radar/scan-now")
async def api_scan_now():
    if radar_engine.scan_in_progress():
        return {
            "ok": True,
            "started": False,
            "error": "radar_scan_already_running",
            "count": len(radar_engine.top50),
            "last_scan_time": radar_engine.last_scan_time,
            "scan_status": _radar_api_scan_status(),
        }
    try:
        started = _start_radar_scan_background(force_refresh=True)
        return {
            "ok": True,
            "started": started,
            "error": "" if started else "radar_scan_already_running",
            "count": len(radar_engine.top50),
            "last_scan_time": radar_engine.last_scan_time,
            "scan_status": _radar_api_scan_status(),
        }
    except Exception as exc:
        return {"ok":False,"started":False,"error":f"{type(exc).__name__}:{exc}","scan_status":_radar_api_scan_status()}


@app.get("/api/radar/universal-anomaly/training")
async def api_universal_anomaly_training(horizon_minutes: int = 5, limit: int = 50):
    horizon = max(1, min(240, int(horizon_minutes)))
    sample_limit = max(1, min(500, int(limit)))
    return {
        "ok": True,
        "summary": universal_anomaly_training.summary(),
        "recent_samples": universal_anomaly_training.recent_samples(limit=sample_limit, horizon_minutes=horizon),
        "policy": {
            "features_exclude_symbol_id": True,
            "label": f"future_return_after_{horizon}m",
            "purpose": "offline training data for coin-agnostic anomaly direction model",
        },
    }


@app.post("/api/radar/universal-anomaly/training/collect")
async def api_collect_universal_anomaly_training(horizon_minutes: int = 5, limit: int = 500):
    horizon = max(1, min(240, int(horizon_minutes)))
    row_limit = max(1, min(5000, int(limit)))
    return universal_anomaly_training.collect(horizon_minutes=horizon, limit=row_limit)


@app.get("/api/radar/universal-anomaly/calibration")
async def api_universal_anomaly_sample_calibration(
    horizon_minutes: int = 5,
    limit: int = 5000,
    min_symbol_samples: int = 5,
    neutral_rate_warn: float = 0.75,
    dominance_warn: float = 0.85,
):
    return universal_anomaly_sample_calibrator.calibrate(
        horizon_minutes=max(1, min(240, int(horizon_minutes))),
        limit=max(1, min(50000, int(limit))),
        repair=False,
        min_symbol_samples=max(1, min(1000, int(min_symbol_samples))),
        neutral_rate_warn=max(0.0, min(1.0, float(neutral_rate_warn))),
        dominance_warn=max(0.0, min(1.0, float(dominance_warn))),
    )


@app.post("/api/radar/universal-anomaly/calibration/repair")
async def api_repair_universal_anomaly_sample_calibration(
    horizon_minutes: int = 5,
    limit: int = 5000,
    min_symbol_samples: int = 5,
    neutral_rate_warn: float = 0.75,
    dominance_warn: float = 0.85,
):
    return universal_anomaly_sample_calibrator.calibrate(
        horizon_minutes=max(1, min(240, int(horizon_minutes))),
        limit=max(1, min(50000, int(limit))),
        repair=True,
        min_symbol_samples=max(1, min(1000, int(min_symbol_samples))),
        neutral_rate_warn=max(0.0, min(1.0, float(neutral_rate_warn))),
        dominance_warn=max(0.0, min(1.0, float(dominance_warn))),
    )


@app.get("/api/radar/universal-anomaly/model")
async def api_universal_anomaly_model_status():
    return {"ok": True, **universal_anomaly_trainer.status(), "auto_trainer": universal_anomaly_auto_trainer.status()}


@app.post("/api/radar/universal-anomaly/model/train")
async def api_train_universal_anomaly_model(
    horizon_minutes: int = 5,
    model_type: str = "auto",
    min_samples: int = 100,
    limit: int = 5000,
):
    horizon = max(1, min(240, int(horizon_minutes)))
    sample_limit = max(1, min(20000, int(limit)))
    floor = max(2, min(10000, int(min_samples)))
    try:
        report = universal_anomaly_trainer.train(
            horizon_minutes=horizon,
            model_type=model_type,
            min_samples=floor,
            limit=sample_limit,
            activate=True,
        )
        report.pop("artifact", None)
        return report
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}:{exc}"}


@app.get("/api/account")
async def api_account():
    return await account_service.get_account_summary()

@app.get("/api/config/mainnet")
async def api_mainnet_config():
    return {
        "market_data_mode": settings.market_data_mode,
        "binance_testnet": settings.binance_testnet,
        "trade_mode": settings.trade_mode,
        "live_trading_enabled": settings.live_trading_enabled,
        "configured": binance_futures.configured(),
        "api_key_tail": settings.binance_api_key[-6:] if settings.binance_api_key else "",
    }

@app.post("/api/config/mainnet")
async def api_save_mainnet_config(payload: MainnetConfigRequest):
    api_key = payload.api_key.strip()
    api_secret = payload.api_secret.strip()
    if not api_key or not api_secret:
        return {"ok": False, "error": "api_key_and_secret_required"}

    values = {
        "MARKET_DATA_MODE": "binance",
        "BINANCE_TESTNET": "false",
        "BINANCE_API_KEY": api_key,
        "BINANCE_API_SECRET": api_secret,
        "TRADE_MODE": "live",
        "LIVE_TRADING_ENABLED": "false",
        "LIVE_USE_TEST_ORDER": "true",
    }
    update_env_values(".env", values)

    settings.market_data_mode = "binance"
    settings.binance_testnet = False
    settings.binance_api_key = api_key
    settings.binance_api_secret = api_secret
    settings.trade_mode = "live"
    settings.live_trading_enabled = False
    settings.live_use_test_order = True
    binance_futures.reload_from_settings()

    return {
        "ok": True,
        "market_data_mode": settings.market_data_mode,
        "binance_testnet": settings.binance_testnet,
        "trade_mode": settings.trade_mode,
        "live_trading_enabled": settings.live_trading_enabled,
        "configured": binance_futures.configured(),
        "api_key_tail": api_key[-6:],
    }


def _codex_strategy_config_response(*, ok: bool = True, mode: str = "codex_strategy_status") -> dict[str, Any]:
    readiness = system_readiness_report()
    codex_entry = readiness.get("codex") if isinstance(readiness.get("codex"), dict) else {}
    real_order_allowed = bool(settings.trade_mode == "live" and settings.live_trading_enabled and not settings.live_use_test_order)
    return {
        "ok": ok,
        "mode": mode,
        "values": {
            "AI_ENABLED": "true" if settings.ai_enabled else "false",
            "AI_STRATEGY_PROVIDER": settings.ai_strategy_provider,
            "REQUIRE_CODEX_STRATEGY_FOR_ENTRY": "true" if settings.require_codex_strategy_for_entry else "false",
            "LIVE_TRADING_ENABLED": "true" if settings.live_trading_enabled else "false",
            "LIVE_USE_TEST_ORDER": "true" if settings.live_use_test_order else "false",
        },
        "safety": {
            "trade_mode": settings.trade_mode,
            "live_trading_enabled": bool(settings.live_trading_enabled),
            "live_use_test_order": bool(settings.live_use_test_order),
            "real_order_allowed": real_order_allowed,
        },
        "codex_entry": codex_entry,
    }


@app.get("/api/config/codex-strategy")
async def api_codex_strategy_config():
    return _codex_strategy_config_response()


@app.post("/api/config/codex-strategy")
async def api_enable_codex_strategy_config(payload: CodexStrategyConfigRequest | None = None):
    values = {
        "AI_ENABLED": "true",
        "AI_STRATEGY_PROVIDER": "codex_cli",
        "REQUIRE_CODEX_STRATEGY_FOR_ENTRY": "true",
        "LIVE_TRADING_ENABLED": "false",
        "LIVE_USE_TEST_ORDER": "true",
    }
    update_env_values(".env", values)

    settings.ai_enabled = True
    settings.ai_strategy_provider = values["AI_STRATEGY_PROVIDER"]
    settings.require_codex_strategy_for_entry = True
    settings.live_trading_enabled = False
    settings.live_use_test_order = True

    return _codex_strategy_config_response(mode="codex_strategy_enforced")


@app.get("/api/exchange/positions")
async def api_exchange_positions():
    try:
        return {"ok": True, "positions": await account_service.get_exchange_positions()}
    except Exception as exc:
        return {"ok": False, "positions": [], "error": repr(exc)}

@app.get("/api/exchange/open-orders")
async def api_open_orders(symbol: str | None = None):
    try:
        return {"ok": True, "orders": await account_service.get_open_orders(symbol)}
    except Exception as exc:
        return {"ok": False, "orders": [], "error": repr(exc)}

@app.get("/api/exchange/reconciliation")
async def api_exchange_reconciliation(force: bool = False):
    try:
        return await exchange_reconciliation.refresh(force=force)
    except Exception as exc:
        return {"ok": False, "error": repr(exc), "cached": exchange_reconciliation.cached()}

@app.get("/api/positions")
async def api_positions():
    await position_manager.manage_all()
    return {
        "summary": position_manager.summary(),
        "open": [_open_position_table_view(p) for p in position_registry.list_open()],
        "closed": [_closed_position_table_view(row) for row in position_registry.list_closed(limit=100)],
    }

@app.get("/api/performance")
async def api_performance():
    return performance_guard.summary()

@app.get("/api/live-readiness")
async def api_live_readiness():
    return live_readiness.summary()

@app.get("/api/strategy-alpha/status")
async def api_strategy_alpha_status():
    return strategy_alpha_status()

@app.post("/api/strategy-alpha/run-cycle")
async def api_strategy_alpha_run_cycle(payload: StrategyAlphaRunRequest):
    return run_strategy_alpha_cycle(
        generation_size=payload.generation_size,
        mutation_size=payload.mutation_size,
    )

@app.get("/api/learning/memory")
async def api_learning_memory():
    return {
        **trade_memory.summary(),
        "replay": replay_memory.summary(),
        "data_audit": learning_data_audit.summary(),
        "calibration": event_calibrator.summary(),
        "radar_weight_calibration": radar_weight_calibrator.compact_context(),
        "attribution": trade_attributor.summary(),
    }

@app.get("/api/learning/transparency")
async def api_learning_transparency(limit: int = 5, include_raw: bool = False):
    sample_limit = max(1, min(50, int(limit)))
    closed_samples = trade_memory.samples(limit=sample_limit, require_radar=True)
    replay_samples = replay_memory.samples(limit=sample_limit)
    decision_observations = ai_strategy_feedback.decision_observations(limit=sample_limit)
    data_quality = learning_data_audit.summary()
    return {
        "purpose": "Show exactly what the learning layer sees, derives, and is allowed to influence.",
        "data_quality": {
            "trust_level": data_quality.get("trust_level"),
            "production_grade": data_quality.get("production_grade"),
            "can_hard_block_from_learning": data_quality.get("can_hard_block_from_learning"),
            "reasons": data_quality.get("reasons", []),
            "sources": data_quality.get("sources", {}),
        },
        "storage_sources": [
            {
                "table": "radar_snapshots",
                "meaning": "Market observations from scans. These are not closed learning outcomes by themselves.",
                "used_for": ["replay_memory", "trade_memory radar join", "radar weight calibration"],
            },
            {
                "table": "closed_positions",
                "meaning": "Paper/live closed trades. These are the strongest real learning samples when joined to radar snapshots.",
                "used_for": ["trade_memory", "trade_attribution", "event_calibration", "performance_guard"],
            },
            {
                "table": "evolved_strategies",
                "meaning": "Candidate strategy filters and AI forward-test records.",
                "used_for": ["active strategy filter", "AI plan forward metrics"],
            },
            {
                "table": "ai_decision_observations",
                "meaning": "AI/gate WAIT, OBSERVE, stale, invalid-plan, and no-candidate observations. These explain why the system did not trade.",
                "used_for": ["Codex prompt feedback", "candidate rejection visibility", "do-not-repeat coaching"],
            },
            {
                "table": "strategy_evolution_runs",
                "meaning": "Audit trail for strategy evolution attempts.",
                "used_for": ["research review", "promotion audit"],
            },
        ],
        "learning_schema": _learning_schema(),
        "what_is_learned": _learning_content_map(),
        "current_samples": {
            "closed_trade_samples": [_learning_sample_view(sample, "closed_trade", include_raw) for sample in closed_samples],
            "replay_samples": [_learning_sample_view(sample, "replay", include_raw) for sample in replay_samples],
            "ai_decision_observations": [
                _decision_observation_view(row, include_raw) for row in decision_observations
            ],
        },
        "summaries": {
            "trade_memory": trade_memory.summary(),
            "replay_memory": replay_memory.summary(),
            "attribution": trade_attributor.summary(),
            "event_calibration": event_calibrator.summary(),
            "ai_strategy_feedback": ai_strategy_feedback.quality_summary(),
        },
        "guardrail": (
            "Learning evidence is allowed to guide paper/shadow review immediately, "
            "but it cannot become a hard live-trading blocker or approval until data_quality.production_grade is true."
        ),
    }


def _learning_schema() -> dict[str, Any]:
    return {
        "identity": ["sample_id", "sample_source", "symbol", "side", "strategy_id", "source_signal_id"],
        "signal_context": [
            "score",
            "rank",
            "fund_confirm_count",
            "fake_breakout_risk",
            "change_5m",
            "change_15m",
            "change_1h",
            "oi_change",
            "volume_spike",
            "taker_buy_ratio",
            "taker_sell_ratio",
            "depth_imbalance",
            "sm_delta",
            "wick_ratio",
            "atr_pct",
        ],
        "execution_outcome": [
            "entry_price",
            "exit_price",
            "pnl",
            "roi",
            "gross_pnl",
            "fee",
            "risk_usdt",
            "risk_pct",
            "mfe",
            "mae",
            "mfe_r",
            "mae_r",
            "hold_time_ms",
            "close_reason",
        ],
        "strategy_context": [
            "strategy_kind",
            "hypothesis",
            "learning_tags",
            "cyqnt_feature_enhancement",
            "allowed_stages",
            "exit_decision",
        ],
        "derived_learning_features": ["pattern", "factors", "root_causes", "profit_drivers", "lesson"],
    }


def _learning_content_map() -> list[dict[str, Any]]:
    return [
        {
            "module": "trade_memory",
            "learns_from": "closed_positions joined with the radar snapshot at entry",
            "learns": "A closed trade sample with market state, plan context, cost, risk, PnL, MFE/MAE, and close reason.",
        },
        {
            "module": "replay_memory",
            "learns_from": "recent radar_snapshots only",
            "learns": "A simulated R-multiple outcome from later radar prices. This is weaker than closed trades and marked replay.",
        },
        {
            "module": "trade_attributor",
            "learns_from": "closed and replay samples",
            "learns": "Which physical factors repeat in losses or wins: wick risk, weak taker/depth, timeframe alignment, fake-breakout risk, tight stops, close reasons.",
        },
        {
            "module": "event_calibrator",
            "learns_from": "similar current-event buckets",
            "learns": "Win rate, profit factor, and PnL for events similar to the current candidate.",
        },
        {
            "module": "strategy_evolver",
            "learns_from": "enough closed/replay samples",
            "learns": "Candidate filter rules such as min_score, fund confirmation, fake-risk allowlist, max wick, and alignment requirements.",
        },
        {
            "module": "learned_risk_guard",
            "learns_from": "trade_attributor plus data-quality audit",
            "learns": "Whether learned loss structures may block/down-weight a candidate. Low-trust data is review-only.",
        },
        {
            "module": "ai_strategy_feedback",
            "learns_from": "AI-generated strategy opens and closes",
            "learns": "Forward metrics per AI strategy_id: opened, closed, win rate, PF, PnL, MFE/MAE, and eligibility.",
        },
        {
            "module": "ai_decision_observations",
            "learns_from": "Codex WAIT decisions, risk OBSERVE decisions, stale-candidate skips, and candidate-selection empty states",
            "learns": "Why the system did not trade under current market conditions. This is rejection coaching, not PnL proof.",
        },
    ]


def _learning_sample_view(sample: dict[str, Any], source: str, include_raw: bool) -> dict[str, Any]:
    normalized = trade_attributor._normalize_sample(sample)
    explanation = trade_attributor.explain_trade(sample)
    contract = sample.get("strategy_contract") if isinstance(sample.get("strategy_contract"), dict) else {}
    signal = normalized.get("radar") if isinstance(normalized.get("radar"), dict) else normalized
    view = {
        "sample_source": source,
        "identity": _pick(normalized, ["sample_id", "symbol", "side", "strategy_id", "source_signal_id", "open_time", "close_time"]),
        "signal_context": _pick(
            signal,
            [
                "score",
                "rank",
                "fund_confirm_count",
                "fund_confirm_total",
                "fake_breakout_risk",
                "change_5m",
                "change_15m",
                "change_1h",
                "oi_change",
                "volume_spike",
                "funding_rate",
                "taker_buy_ratio",
                "taker_sell_ratio",
                "depth_imbalance",
                "sm_delta",
                "sm_position",
                "wick_ratio",
                "atr_pct",
                "dealer_radar",
            ],
        ),
        "execution_outcome": _pick(
            normalized,
            [
                "entry_price",
                "exit_price",
                "pnl",
                "roi",
                "gross_pnl",
                "fee",
                "risk_usdt",
                "risk_pct",
                "mfe",
                "mae",
                "mfe_r",
                "mae_r",
                "hold_time_ms",
                "close_reason",
            ],
        ),
        "strategy_context": {
            "strategy_kind": contract.get("strategy_kind"),
            "hypothesis": contract.get("hypothesis"),
            "learning_tags": contract.get("learning_tags", {}),
            "allowed_stages": contract.get("allowed_stages", {}),
            "cyqnt_feature_enhancement": contract.get("cyqnt_feature_enhancement", {}),
            "exit_decision": normalized.get("exit_decision"),
        },
        "derived_learning_features": {
            "pattern": normalized.get("pattern"),
            "factors": normalized.get("factors", []),
            "root_causes": explanation.get("root_causes", []),
            "profit_drivers": explanation.get("profit_drivers", []),
            "lesson": explanation.get("lesson"),
        },
        "used_by": [
            "trade_attribution",
            "event_calibration",
            "strategy_evolver",
            "learned_risk_guard",
            "performance_guard" if source == "closed_trade" else "research_replay_only",
        ],
    }
    if include_raw:
        view["raw_sample"] = sample
    return view


def _pick(row: dict[str, Any], keys: list[str]) -> dict[str, Any]:
    return {key: row.get(key) for key in keys if key in row}


def _decision_observation_view(row: dict[str, Any], include_raw: bool) -> dict[str, Any]:
    radar = row.get("radar") if isinstance(row.get("radar"), dict) else {}
    feature = row.get("cyqnt_feature_enhancement") if isinstance(row.get("cyqnt_feature_enhancement"), dict) else {}
    context = row.get("context") if isinstance(row.get("context"), dict) else {}
    out = {
        "sample_source": row.get("sample_type", "decision_observation_not_trade_outcome"),
        "identity": _pick(row, ["observation_id", "created_at", "symbol", "side", "candidate_source", "provider", "model"]),
        "decision": _pick(row, ["stage", "decision", "reason", "plan_action", "wait_type", "paper_validation"]),
        "signal_context": _pick(
            radar,
            [
                "score",
                "rank",
                "fund_confirm",
                "fake_breakout_risk",
                "direction_confirmations",
                "volume_spike",
                "taker_buy_ratio",
                "depth_imbalance",
                "sm_delta",
                "wick_ratio",
            ],
        ),
        "cyqnt_feature_enhancement": _pick(
            feature,
            ["feature_score", "selection_score", "estimated_win_rate", "positive_factors", "failure_risks"],
        ),
        "candidate_gate_context": _pick(context, ["counts", "rejection_counts_top12", "gate"]),
        "used_by": ["Codex prompt feedback", "candidate rejection visibility", "do-not-repeat coaching"],
        "guardrail": "Not a closed trade and not allowed to improve win rate, PnL, or live-readiness by itself.",
    }
    if include_raw:
        out["raw_sample"] = row
    return out


@app.get("/api/learning/calibration")
async def api_learning_calibration():
    return event_calibrator.summary()

@app.get("/api/learning/data-audit")
async def api_learning_data_audit(force: bool = False):
    return learning_data_audit.summary(force=force)

@app.get("/api/learning/attribution")
async def api_learning_attribution():
    return trade_attributor.summary()

@app.get("/api/learning/attribution/deep")
async def api_learning_attribution_deep(limit: int = 20):
    return trade_attributor.deep_analysis(trade_limit=max(1, min(100, int(limit))))

@app.get("/api/learning/radar-score")
async def api_learning_radar_score(limit: int = 5000):
    scan_error = ""
    if not radar_engine.top50:
        if radar_engine.scan_in_progress():
            scan_error = "radar_scan_running_no_cache"
        else:
            _start_radar_scan_background()
            scan_error = "radar_scan_warming_up"
    report = radar_score_auditor.report(current_items=radar_engine.top50, limit=limit)
    return {**report, "ok": not bool(scan_error), "scan_error": scan_error, "scan_status": radar_engine.scan_status()}

@app.get("/api/learning/radar-weights")
async def api_learning_radar_weights(limit: int = 5000, force: bool = False):
    return radar_weight_calibrator.report(limit=limit, force=force)

@app.get("/api/learning/guard")
async def api_learning_guard(limit: int = 20):
    scan_error = ""
    if not radar_engine.top50:
        try:
            await _radar_scan_with_timeout(timeout_seconds=RADAR_DIAGNOSTICS_SCAN_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            scan_error = "radar_scan_timeout"
        except Exception as exc:
            scan_error = f"{type(exc).__name__}:{exc}"
    performance = performance_guard.summary()
    recovery_mode = bool(performance.get("recovery_mode"))
    rows = []
    for item in radar_engine.top50[: max(1, min(100, int(limit)))]:
        report = learned_risk_guard.evaluate(item, None, recovery_mode=recovery_mode)
        reverse = learned_risk_guard.reverse_opportunity(item, recovery_mode=recovery_mode)
        rows.append(
            {
                "symbol": item.symbol,
                "side": item.direction,
                "score": item.score,
                "rank": item.rank,
                "fund_confirm": f"{item.fund_confirm_count}/{item.fund_confirm_total}",
                "fake_breakout_risk": item.fake_breakout_risk,
                "allow_paper": report.allow_paper,
                "allow_live": report.allow_live,
                "severity": report.severity,
                "reasons": report.reasons,
                "advice": report.advice,
                "hard_blocks": report.hard_blocks,
                "matched_samples": report.matched_samples,
                "win_rate": report.win_rate,
                "profit_factor": report.profit_factor,
                "pnl": report.pnl,
                "reverse_opportunity": {
                    "allow_reverse": reverse.get("allow_reverse"),
                    "reason": reverse.get("reason"),
                    "reverse_item": reverse.get("reverse_item"),
                    "reverse_confirmations": reverse.get("reverse_confirmations"),
                    "reverse_fund_confirm": reverse.get("reverse_fund_confirm"),
                    "reverse": reverse.get("reverse"),
                },
            }
        )
    return {
        "ok": not bool(scan_error),
        "scan_error": scan_error,
        "scan_status": radar_engine.scan_status(),
        "enabled": settings.trade_learning_guard_enabled and settings.trade_attribution_enabled,
        "recovery_mode": recovery_mode,
        "items": rows,
        "attribution": trade_attributor.summary(),
    }

@app.get("/api/learning/strategies")
async def api_learning_strategies():
    return {
        "active": strategy_registry.active(),
        "strategies": strategy_registry.list(),
        "runs": strategy_registry.runs(),
    }

@app.get("/api/learning/decision-observations")
async def api_learning_decision_observations(limit: int = 20):
    sample_limit = max(1, min(200, int(limit)))
    rows = ai_strategy_feedback.decision_observations(limit=sample_limit)
    return {
        "count": len(rows),
        "summary": ai_strategy_feedback.quality_summary().get("decision_observations", {}),
        "items": [_decision_observation_view(row, False) for row in rows],
    }

@app.post("/api/learning/evolve")
async def api_learning_evolve(payload: EvolveRequest):
    return strategy_evolver.evolve(use_codex=payload.use_codex, promote=payload.promote)

@app.post("/api/learning/paper-repair")
async def api_learning_paper_repair(payload: PaperRepairRequest):
    before = _autotrade_params()
    evolve = strategy_evolver.evolve(use_codex=False, promote=payload.promote)
    values = {
        "AUTO_TRADING_CANDIDATE_MODE": "paper_top",
        "AUTO_TRADING_CANDIDATE_MIN_SCORE": "55",
        "AUTO_TRADING_CANDIDATE_LIMIT": "1",
        "AUTO_TRADING_USE_ACTIVE_STRATEGY_FILTER": "true",
        "AUTO_TRADING_USE_PERFORMANCE_GUARD": "true",
        "PAPER_PROBE_ENABLED": "true",
        "MAX_OPEN_POSITIONS": "1",
        "LIVE_TRADING_ENABLED": "false",
        "AI_ENABLED": "false",
        "AI_STRATEGY_PROVIDER": "rule",
        "REQUIRE_CODEX_STRATEGY_FOR_ENTRY": "false",
    }
    update_env_values(".env", values)

    settings.auto_trading_candidate_mode = values["AUTO_TRADING_CANDIDATE_MODE"]
    settings.auto_trading_candidate_min_score = float(values["AUTO_TRADING_CANDIDATE_MIN_SCORE"])
    settings.auto_trading_candidate_limit = int(values["AUTO_TRADING_CANDIDATE_LIMIT"])
    settings.auto_trading_use_active_strategy_filter = True
    settings.auto_trading_use_performance_guard = True
    settings.paper_probe_enabled = True
    settings.max_open_positions = 1
    settings.live_trading_enabled = False
    settings.ai_enabled = False
    settings.ai_strategy_provider = values["AI_STRATEGY_PROVIDER"]
    settings.require_codex_strategy_for_entry = False
    autotrader.enabled = False

    verify = None
    if payload.run_once:
        verify = await autotrader.run_once()

    return {
        "ok": True,
        "mode": "paper_repair_verify",
        "before": before,
        "after": _autotrade_params(),
        "evolve": evolve,
        "verify": verify,
        "safety": {
            "live_trading_enabled": settings.live_trading_enabled,
            "auto_loop_enabled": autotrader.enabled,
            "real_order_allowed": False,
        },
    }

@app.post("/api/strategy-ai/ask")
async def api_strategy_ai_ask(payload: StrategyAskRequest):
    return await strategy_qa.ask(payload.question)

@app.get("/api/strategy-ai/realtime")
async def api_strategy_ai_realtime():
    scan_error = ""
    if not radar_engine.top50:
        try:
            await _radar_scan_with_timeout(timeout_seconds=RADAR_DIAGNOSTICS_SCAN_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            scan_error = "radar_scan_timeout"
        except Exception as exc:
            scan_error = f"{type(exc).__name__}:{exc}"
    performance = performance_guard.summary()
    candidates, candidate_source = autotrader._candidate_batch(performance)
    ai_status = ai_service.status(candidate_count=len(candidates), candidate_source=candidate_source)
    provider = ai_status.get("provider")
    provider_status = ai_status.get(provider) if provider in {"deepseek", "codex_cli"} else {}
    return {
        "ok": not bool(scan_error),
        "scan_error": scan_error,
        "scan_status": radar_engine.scan_status(),
        "provider": provider,
        "candidate_source": candidate_source,
        "candidate_symbols": [item.symbol for item in candidates],
        "candidate_lock": autotrader.candidate_lock_status(),
        "ai_strategy": ai_status,
        "ai_strategy_quality": {
            "summary": ai_strategy_feedback.quality_summary(),
            "candidate_feedback": [ai_strategy_feedback.evaluate_candidate(item) for item in candidates[:5]],
        },
        "last_plan": (provider_status or {}).get("last_plan") or {},
        "recent_plans": (provider_status or {}).get("recent_plans") or [],
        "last_result": autotrader.last_result,
        "radar_weight_calibration": radar_weight_calibrator.compact_context(),
        "safety": {
            "live_trading_enabled": settings.live_trading_enabled,
            "auto_loop_enabled": autotrader.enabled,
            "real_order_allowed": bool(settings.trade_mode == "live" and settings.live_trading_enabled),
        },
    }

@app.get("/api/strategy-ai/quality")
async def api_strategy_ai_quality():
    scan_error = ""
    if not radar_engine.top50:
        try:
            await _radar_scan_with_timeout(timeout_seconds=RADAR_DIAGNOSTICS_SCAN_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            scan_error = "radar_scan_timeout"
        except Exception as exc:
            scan_error = f"{type(exc).__name__}:{exc}"
    performance = performance_guard.summary()
    candidates, candidate_source = autotrader._candidate_batch(performance)
    return {
        "ok": not bool(scan_error),
        "scan_error": scan_error,
        "scan_status": radar_engine.scan_status(),
        "candidate_source": candidate_source,
        "summary": ai_strategy_feedback.quality_summary(),
        "candidate_feedback": [ai_strategy_feedback.evaluate_candidate(item) for item in candidates[:8]],
        "performance": performance,
        "safety": {
            "live_trading_enabled": settings.live_trading_enabled,
            "auto_loop_enabled": autotrader.enabled,
            "real_order_allowed": bool(settings.trade_mode == "live" and settings.live_trading_enabled),
        },
    }

@app.post("/api/positions/{position_id}/manual-close")
async def api_manual_close(position_id: str):
    p=position_registry.open.get(position_id)
    if not p: return {"ok":False,"error":"not_found"}
    if _is_real_live_position(p):
        if not (settings.trade_mode == "live" and settings.live_trading_enabled):
            return {
                "ok": False,
                "error": "real_live_position_requires_exchange_close",
                "message": "Refusing local-only close for a real live position while live trading is disabled.",
                "position_id": p.position_id,
                "symbol": p.symbol,
            }
        c = await position_manager.managed_close(p, "MANUAL_CLOSE", p.current_price)
    else:
        c=position_manager.close_position(p,"MANUAL_CLOSE",p.current_price)
    return {"ok":True,"closed":c.asdict()}

def _is_real_live_position(p) -> bool:
    if not str(getattr(p, "position_id", "") or "").startswith("livepos"):
        return False
    open_order = getattr(p, "exchange_open_order", {})
    if isinstance(open_order, dict) and open_order.get("testOrder"):
        return False
    if getattr(p, "lock_status", "") == "LIVE_TEST_ORDER":
        return False
    return True

@app.post("/api/autotrade/run-once")
async def api_run_once():
    return await ai_trade_director.run_once(source="manual")

@app.get("/api/trade-director/status")
async def api_trade_director_status():
    scan_error = ""
    if not radar_engine.top50:
        try:
            await _radar_scan_with_timeout(timeout_seconds=RADAR_DIAGNOSTICS_SCAN_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            scan_error = "radar_scan_timeout"
        except Exception as exc:
            scan_error = f"{type(exc).__name__}:{exc}"
    return {**ai_trade_director.status(), "ok": not bool(scan_error), "scan_error": scan_error}

@app.post("/api/trade-director/codex-paper-probe")
async def api_trade_director_codex_paper_probe():
    return await ai_trade_director.run_codex_paper_probe()

@app.post("/api/trade-director/acceptance/paper-cycle")
async def api_trade_director_acceptance_paper_cycle():
    return await trade_acceptance_runner.run_controlled_paper_cycle()

@app.post("/api/trade-director/acceptance/production")
async def api_trade_director_acceptance_production(payload: ProductionAcceptanceRequest):
    return await production_acceptance_runner.run(
        mode=payload.mode,
        confirm_real_order=payload.confirm_real_order,
        manage_seconds=payload.manage_seconds,
    )

@app.get("/api/trade-director/acceptance/production")
async def api_trade_director_acceptance_production_status():
    return production_acceptance_runner.status()

@app.get("/api/autotrade/params")
async def api_autotrade_params():
    return _autotrade_params()

@app.get("/api/autotrade/diagnostics")
async def api_autotrade_diagnostics():
    scan_error = ""
    if not radar_engine.top50:
        if radar_engine.scan_in_progress():
            scan_error = "radar_scan_running_no_cache"
        else:
            _start_radar_scan_background()
            scan_error = "radar_scan_warming_up"
    performance = performance_guard.summary()
    loop_ok, loop_reason, loop_performance = autotrader.loop_start_guard()
    candidate_filter = autotrader.candidate_diagnostics_light(performance)
    candidate_source = str(candidate_filter.get("candidate_source") or "")
    candidate_symbols = [str(symbol) for symbol in candidate_filter.get("candidate_symbols") or []]
    candidates = _items_by_symbols(radar_engine.top50, candidate_symbols)
    active_strategy = strategy_registry.active()
    strategy_filter = None
    if active_strategy and settings.auto_trading_use_active_strategy_filter:
        strategy_filter = autotrader.strategy_selection_diagnostics(active_strategy, candidates)
    return {
        "ok": not bool(scan_error),
        "scan_error": scan_error,
        "scan_status": _radar_api_scan_status(),
        "candidate_source": candidate_source,
        "candidate_count_before_strategy": len(candidates),
        "candidate_symbols_before_strategy": candidate_symbols,
        "candidate_lock": autotrader.candidate_lock_status(),
        "trade_director": _light_trade_director_status(
            candidate_source=candidate_source,
            candidate_symbols=candidate_symbols,
            performance=performance,
            loop_ok=loop_ok,
            loop_reason=loop_reason,
            loop_performance=loop_performance,
        ),
        "ai_strategy": _compact_ai_strategy_status(
            ai_service.status(candidate_count=len(candidates), candidate_source=candidate_source)
        ),
        "ai_strategy_quality": {
            "summary": ai_strategy_feedback.quality_summary(),
            "candidate_feedback": [],
            "candidate_feedback_skipped": "lightweight_diagnostics",
        },
        "ai_position_policy": ai_position_policy_client.status(),
        "candidate_filter": candidate_filter,
        "active_strategy_filter_enabled": bool(settings.auto_trading_use_active_strategy_filter),
        "active_strategy": {
            "strategy_id": active_strategy.get("strategy_id"),
            "name": active_strategy.get("name"),
            "filters": active_strategy.get("filters"),
            "metrics": active_strategy.get("metrics"),
        } if active_strategy else None,
        "strategy_filter": strategy_filter,
        "performance": performance,
        "loop_start_guard": {
            "ok": loop_ok,
            "reason": loop_reason,
            "performance": loop_performance,
        },
        "last_result": autotrader.last_result,
        "safety": {
            "live_trading_enabled": settings.live_trading_enabled,
            "auto_loop_enabled": autotrader.enabled,
            "real_order_allowed": bool(settings.trade_mode == "live" and settings.live_trading_enabled),
        },
    }


def _closed_position_table_view(row: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "position_id",
        "strategy_id",
        "symbol",
        "side",
        "entry_price",
        "exit_price",
        "quantity",
        "margin",
        "notional",
        "gross_pnl",
        "fee",
        "pnl",
        "roi",
        "risk_usdt",
        "risk_pct",
        "close_reason",
        "open_time",
        "close_time",
        "source_signal_id",
    )
    return {key: row.get(key) for key in keys}


def _open_position_table_view(position) -> dict[str, Any]:
    row = position.asdict()
    strategy = strategy_registry.get(position.strategy_id) or {}
    source = str(strategy.get("source") or "").strip().lower()
    provider = str(strategy.get("provider") or "").strip().lower()
    contract = position.strategy_contract if isinstance(position.strategy_contract, dict) else {}
    contract_provider = str(contract.get("provider") or contract.get("model_provider") or "").strip().lower()
    if provider == "codex_cli" or source == "ai_generated_codex_cli" or contract_provider == "codex_cli":
        row["learning_countability"] = ai_trade_director._codex_learning_countability(position, strategy)
    return row


def _compact_ai_strategy_status(status: dict[str, Any]) -> dict[str, Any]:
    codex = status.get("codex_cli") if isinstance(status.get("codex_cli"), dict) else {}
    deepseek = status.get("deepseek") if isinstance(status.get("deepseek"), dict) else {}
    return {
        "enabled": status.get("enabled"),
        "provider": status.get("provider"),
        "candidate_source": status.get("candidate_source"),
        "candidate_count_before_ai": status.get("candidate_count_before_ai"),
        "will_invoke_for_current_candidates": status.get("will_invoke_for_current_candidates"),
        "not_invoked_reason": status.get("not_invoked_reason"),
        "codex_cli": {
            "command_found": codex.get("command_found"),
            "ready_for_generation": codex.get("ready_for_generation"),
            "availability_reason": codex.get("availability_reason"),
            "auth_required": codex.get("auth_required"),
            "auth_available": codex.get("auth_available"),
            "auth_source": codex.get("auth_source"),
            "codex_home": codex.get("codex_home"),
            "auth_json_exists": codex.get("auth_json_exists"),
            "schema_exists": codex.get("schema_exists"),
            "model": codex.get("model"),
            "timeout_seconds": codex.get("timeout_seconds"),
            "reasoning_effort": codex.get("reasoning_effort"),
            "service_tier": codex.get("service_tier"),
            "invocation_count": codex.get("invocation_count"),
            "last_status": codex.get("last_status"),
            "last_error": codex.get("last_error"),
            "last_model": codex.get("last_model"),
            "last_route": codex.get("last_route"),
            "last_symbol": codex.get("last_symbol"),
            "last_action": codex.get("last_action"),
        },
        "deepseek": {
            "enabled": deepseek.get("enabled"),
            "configured": deepseek.get("configured"),
            "model": deepseek.get("model"),
            "last_status": deepseek.get("last_status"),
            "last_error": deepseek.get("last_error"),
        },
    }


def _compact_trade_director_status(status: dict[str, Any]) -> dict[str, Any]:
    return {
        "cycle_id": status.get("cycle_id"),
        "stage": status.get("stage"),
        "source": status.get("source"),
        "candidate_source": status.get("candidate_source"),
        "candidate_symbols": status.get("candidate_symbols"),
        "candidate_lock": status.get("candidate_lock"),
        "performance": status.get("performance"),
        "loop_start_guard": status.get("loop_start_guard"),
        "positions": status.get("positions"),
        "live_readiness": {
            "current_stage": (status.get("live_readiness") or {}).get("current_stage"),
            "blockers": (status.get("live_readiness") or {}).get("blockers", [])[:8],
        },
        "safety": status.get("safety"),
    }

@app.post("/api/autotrade/params")
async def api_save_autotrade_params(payload: AutoTradeParamsRequest):
    mode = payload.auto_trading_candidate_mode.strip().lower()
    if mode not in {"strict", "paper_top"}:
        return {"ok": False, "error": "invalid_candidate_mode"}
    recovery_mode = bool(performance_guard.summary().get("recovery_mode"))
    values = {
        "AUTO_TRADING_CANDIDATE_MODE": mode,
        "AUTO_TRADING_CANDIDATE_MIN_SCORE": str(max(0.0, min(100.0, payload.auto_trading_candidate_min_score))),
        "AUTO_TRADING_CANDIDATE_LIMIT": str(max(1, min(5, int(payload.auto_trading_candidate_limit)))),
        "AUTO_TRADING_USE_ACTIVE_STRATEGY_FILTER": "true" if payload.auto_trading_use_active_strategy_filter else "false",
        "AUTO_TRADING_USE_PERFORMANCE_GUARD": "true" if payload.auto_trading_use_performance_guard or performance_guard.summary().get("recovery_mode") else "false",
        "PAPER_ACCOUNT_EQUITY_USDT": str(max(1.0, payload.paper_account_equity_usdt)),
        "MAX_OPEN_POSITIONS": str(max(1, min(3, int(payload.max_open_positions)))),
        "TRADE_TARGET_MARGIN_PCT": str(max(0.001, min(1.0, payload.trade_target_margin_pct))),
        "TRADE_MAX_MARGIN_PCT": str(max(0.001, min(1.0, payload.trade_max_margin_pct))),
        "TRADE_MAX_RISK_PCT": str(max(0.0001, min(0.03, payload.trade_max_risk_pct))),
        "TRADE_MIN_NET_PROFIT_USDT": str(max(0.0, min(1000.0, payload.trade_min_net_profit_usdt))),
        "TRADE_MIN_PROFIT_COST_RATIO": str(max(0.0, min(100.0, payload.trade_min_profit_cost_ratio))),
        "TRADE_MIN_MARGIN_USDT": str(max(0.0, payload.trade_min_margin_usdt)),
        "TRADE_MIN_NOTIONAL_USDT": str(max(0.0, payload.trade_min_notional_usdt)),
        "TRADE_RESERVED_BALANCE_PCT": str(max(0.0, min(0.95, payload.trade_reserved_balance_pct))),
        "STRATEGY_MIN_PAPER_WIN_RATE": str(max(0.60 if recovery_mode else 0.0, min(1.0, payload.strategy_min_paper_win_rate))),
        "STRATEGY_MIN_PAPER_CONFIDENCE": str(max(60.0 if recovery_mode else 0.0, min(100.0, payload.strategy_min_paper_confidence))),
        "STRATEGY_MIN_EXPECTED_R": str(max(0.10 if recovery_mode else -5.0, min(5.0, payload.strategy_min_expected_r))),
        "STRATEGY_MIN_TP2_R": str(max(0.1, min(20.0, payload.strategy_min_tp2_r))),
        "LIVE_TRADING_ENABLED": "false",
    }
    update_env_values(".env", values)

    settings.auto_trading_candidate_mode = values["AUTO_TRADING_CANDIDATE_MODE"]
    settings.auto_trading_candidate_min_score = float(values["AUTO_TRADING_CANDIDATE_MIN_SCORE"])
    settings.auto_trading_candidate_limit = int(values["AUTO_TRADING_CANDIDATE_LIMIT"])
    settings.auto_trading_use_active_strategy_filter = values["AUTO_TRADING_USE_ACTIVE_STRATEGY_FILTER"] == "true"
    settings.auto_trading_use_performance_guard = values["AUTO_TRADING_USE_PERFORMANCE_GUARD"] == "true"
    settings.paper_account_equity_usdt = float(values["PAPER_ACCOUNT_EQUITY_USDT"])
    settings.max_open_positions = int(values["MAX_OPEN_POSITIONS"])
    settings.trade_target_margin_pct = float(values["TRADE_TARGET_MARGIN_PCT"])
    settings.trade_max_margin_pct = float(values["TRADE_MAX_MARGIN_PCT"])
    settings.trade_max_risk_pct = float(values["TRADE_MAX_RISK_PCT"])
    settings.trade_min_net_profit_usdt = float(values["TRADE_MIN_NET_PROFIT_USDT"])
    settings.trade_min_profit_cost_ratio = float(values["TRADE_MIN_PROFIT_COST_RATIO"])
    settings.trade_min_margin_usdt = float(values["TRADE_MIN_MARGIN_USDT"])
    settings.trade_min_notional_usdt = float(values["TRADE_MIN_NOTIONAL_USDT"])
    settings.trade_reserved_balance_pct = float(values["TRADE_RESERVED_BALANCE_PCT"])
    settings.strategy_min_paper_win_rate = float(values["STRATEGY_MIN_PAPER_WIN_RATE"])
    settings.strategy_min_paper_confidence = float(values["STRATEGY_MIN_PAPER_CONFIDENCE"])
    settings.strategy_min_expected_r = float(values["STRATEGY_MIN_EXPECTED_R"])
    settings.strategy_min_tp2_r = float(values["STRATEGY_MIN_TP2_R"])
    settings.live_trading_enabled = False
    return {"ok": True, "params": _autotrade_params()}

@app.post("/api/autotrade/start")
async def api_start():
    ok, reason, performance = autotrader.loop_start_guard()
    if not ok:
        autotrader.enabled=False
        return {"ok":False,"enabled":False,"error":reason,"performance":performance}
    autotrader.enabled=True
    return {"ok":True,"enabled":True}
@app.post("/api/autotrade/stop")
async def api_stop():
    autotrader.enabled=False
    return {"ok":True,"enabled":False}

def run():
    uvicorn.run("backend.main:app", host=settings.app_host, port=settings.app_port, reload=False)


def _autotrade_params():
    return {
        "auto_trading_candidate_mode": settings.auto_trading_candidate_mode,
        "auto_trading_candidate_min_score": settings.auto_trading_candidate_min_score,
        "auto_trading_candidate_limit": settings.auto_trading_candidate_limit,
        "auto_trading_use_active_strategy_filter": settings.auto_trading_use_active_strategy_filter,
        "auto_trading_use_performance_guard": settings.auto_trading_use_performance_guard,
        "paper_probe_enabled": settings.paper_probe_enabled,
        "paper_probe_min_score_floor": settings.paper_probe_min_score_floor,
        "paper_probe_min_fund_confirm": settings.paper_probe_min_fund_confirm,
        "paper_probe_min_direction_confirmations": settings.paper_probe_min_direction_confirmations,
        "paper_probe_max_wick_ratio": settings.paper_probe_max_wick_ratio,
        "paper_loop_allow_recovery": settings.paper_loop_allow_recovery,
        "paper_account_equity_usdt": settings.paper_account_equity_usdt,
        "max_open_positions": settings.max_open_positions,
        "trade_target_margin_pct": settings.trade_target_margin_pct,
        "trade_max_margin_pct": settings.trade_max_margin_pct,
        "trade_max_risk_pct": settings.trade_max_risk_pct,
        "trade_min_net_profit_usdt": settings.trade_min_net_profit_usdt,
        "trade_min_profit_cost_ratio": settings.trade_min_profit_cost_ratio,
        "trade_min_margin_usdt": settings.trade_min_margin_usdt,
        "trade_min_notional_usdt": settings.trade_min_notional_usdt,
        "trade_reserved_balance_pct": settings.trade_reserved_balance_pct,
        "strategy_min_paper_win_rate": settings.strategy_min_paper_win_rate,
        "strategy_min_paper_confidence": settings.strategy_min_paper_confidence,
        "strategy_min_expected_r": settings.strategy_min_expected_r,
        "strategy_min_tp2_r": settings.strategy_min_tp2_r,
        "trade_attribution_enabled": settings.trade_attribution_enabled,
        "trade_learning_guard_enabled": settings.trade_learning_guard_enabled,
        "trade_learning_guard_min_rule_samples": settings.trade_learning_guard_min_rule_samples,
        "trade_learning_guard_recovery_strict": settings.trade_learning_guard_recovery_strict,
        "trade_learning_reverse_enabled": settings.trade_learning_reverse_enabled,
        "trade_learning_reverse_min_confirmations": settings.trade_learning_reverse_min_confirmations,
        "trade_learning_reverse_min_win_rate": settings.trade_learning_reverse_min_win_rate,
        "trade_learning_reverse_min_profit_factor": settings.trade_learning_reverse_min_profit_factor,
        "trade_mode": settings.trade_mode,
        "live_trading_enabled": settings.live_trading_enabled,
        "live_use_test_order": settings.live_use_test_order,
    }
