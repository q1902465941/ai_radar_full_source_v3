from __future__ import annotations

import argparse
import asyncio
import csv
import json
import math
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.config import settings
from backend.learning.learning_data_audit import learning_data_audit
from backend.learning.market_side_guard import (
    market_side_block_active,
    market_side_block_reason,
    market_side_report_fresh,
    side_blocks_from_market_metrics,
)
from backend.learning.radar_weight_calibrator import radar_weight_calibrator
from backend.models import MarketSnapshot, RadarItem, now_ms
from backend.radar.candidate_feature_enhancer import candidate_feature_enhancer
from backend.radar.dealer_radar import dealer_label
from backend.radar.fake_breakout import fake_breakout
from backend.radar.fund_confirm import fund_confirm, fund_confirm_components
from backend.radar.radar_engine import RadarEngine
from backend.radar.score_engine import clamp, direction, score_engine


BASE_URL = "https://fapi.binance.com"
PRIORITY_SYMBOLS = ("BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT")
INTERVAL_MS = {
    "1m": 60_000,
    "3m": 180_000,
    "5m": 300_000,
    "15m": 900_000,
    "30m": 1_800_000,
    "1h": 3_600_000,
}
_REQUEST_LOCK: asyncio.Lock | None = None
_NEXT_REQUEST_AT = 0.0
REQUEST_DELAY_SECONDS = 0.12
MAX_HTTP_ATTEMPTS = 5
_BACKTEST_RADAR_ENGINE = RadarEngine()


@dataclass
class Candle:
    open_time: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    close_time: int
    quote_volume: float
    trades: int
    taker_buy_base: float
    taker_buy_quote: float


@dataclass
class SignalRow:
    entry_time: int
    signal_index: int
    item: RadarItem
    feature_score: float
    estimated_win_rate: float
    selection_score: float
    candidate_reasons: list[str]


async def main_async() -> int:
    parser = argparse.ArgumentParser(description="Run a candle-level Binance Futures market backtest for the radar candidate rules.")
    parser.add_argument("--days", type=float, default=14.0, help="Recent calendar days to fetch from Binance mainnet.")
    parser.add_argument("--interval", default="5m", choices=sorted(INTERVAL_MS), help="Kline interval.")
    parser.add_argument("--symbol-limit", type=int, default=16, help="Number of USDT-M symbols to test.")
    parser.add_argument("--symbols", default="", help="Comma-separated symbols. Overrides symbol selection when provided.")
    parser.add_argument("--skip-oi", action="store_true", help="Do not fetch open-interest history.")
    parser.add_argument("--use-cache", action="store_true", help="Read previously downloaded mainnet candle cache instead of fetching Binance.")
    parser.add_argument("--horizon-bars", type=int, default=int(settings.replay_horizon_steps), help="Max bars to hold a simulated trade.")
    parser.add_argument("--output-tag", default="latest", help="Report tag suffix. 'latest' also writes stable latest files.")
    args = parser.parse_args()
    args.market_data_source = "local_mainnet_candle_cache" if args.use_cache else "mainnet_public"

    started_ms = now_ms()
    interval_ms = INTERVAL_MS[args.interval]
    end_ms = started_ms - interval_ms
    start_ms = int(end_ms - max(1.0, float(args.days)) * 86_400_000)
    timeout = httpx.Timeout(max(10.0, float(settings.binance_http_timeout) * 4.0))
    limits = httpx.Limits(max_connections=8, max_keepalive_connections=4)

    try:
        if args.use_cache:
            symbols = parse_symbols(args.symbols) or cached_symbols(args.interval, max(1, int(args.symbol_limit)))
            fetched = load_cached_market_data(symbols, args.interval, start_ms, end_ms)
        else:
            async with httpx.AsyncClient(base_url=BASE_URL, timeout=timeout, limits=limits) as client:
                symbols = parse_symbols(args.symbols) or await select_symbols(client, max(1, int(args.symbol_limit)))
                fetched = await fetch_market_data(
                    client=client,
                    symbols=symbols,
                    interval=args.interval,
                    interval_ms=interval_ms,
                    start_ms=start_ms,
                    end_ms=end_ms,
                    fetch_oi=not args.skip_oi,
                )
    except Exception as exc:
        report = failure_report(args, started_ms, start_ms, end_ms, f"{type(exc).__name__}: {exc}")
        write_reports(report, [], args.output_tag)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 2

    signals, limitations = build_signals(fetched, args.interval, interval_ms)
    trades = run_trades(fetched, signals, max(1, int(args.horizon_bars)))
    report = build_report(
        args=args,
        started_ms=started_ms,
        start_ms=start_ms,
        end_ms=end_ms,
        fetched=fetched,
        signals=signals,
        trades=trades,
        limitations=limitations,
    )
    write_candles(fetched, args.interval)
    write_reports(report, trades, args.output_tag)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["validation"]["passed"] else 1


async def select_symbols(client: httpx.AsyncClient, limit: int) -> list[str]:
    rows = await get_json(client, "/fapi/v1/ticker/24hr", {})
    exchange_info = await get_json(client, "/fapi/v1/exchangeInfo", {})
    if not isinstance(rows, list):
        raise RuntimeError("ticker_24hr_unavailable")
    exchange_meta = exchange_symbol_meta(exchange_info)
    if settings.binance_crypto_perpetual_only and not exchange_meta:
        raise RuntimeError("exchange_info_unavailable")
    volume_ranked: list[tuple[str, float]] = []
    mover_ranked: list[tuple[str, float]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        symbol = str(row.get("symbol") or "")
        if not symbol_allowed(symbol, exchange_meta):
            continue
        if _f(row.get("lastPrice")) <= 0:
            continue
        volume_ranked.append((symbol, _f(row.get("quoteVolume"))))
        mover_ranked.append((symbol, abs(_f(row.get("priceChangePercent")))))
    volume_ranked.sort(key=lambda item: item[1], reverse=True)
    mover_ranked.sort(key=lambda item: item[1], reverse=True)
    valid = {symbol for symbol, _ in volume_ranked}
    selected: list[str] = []

    def add(symbol: str) -> None:
        if symbol in valid and symbol not in selected and len(selected) < limit:
            selected.append(symbol)

    for symbol in PRIORITY_SYMBOLS:
        add(symbol)
    remaining = max(0, limit - len(selected))
    mover_slots = max(0, min(remaining, round(remaining * max(0.0, min(float(settings.binance_mover_share), 0.8)))))
    volume_slots = max(0, remaining - mover_slots)
    for symbol, _ in volume_ranked[:volume_slots]:
        add(symbol)
    for symbol, _ in mover_ranked[:mover_slots]:
        add(symbol)
    for symbol, _ in volume_ranked:
        add(symbol)
        if len(selected) >= limit:
            break
    return selected


async def fetch_market_data(
    *,
    client: httpx.AsyncClient,
    symbols: list[str],
    interval: str,
    interval_ms: int,
    start_ms: int,
    end_ms: int,
    fetch_oi: bool,
) -> dict[str, dict[str, Any]]:
    semaphore = asyncio.Semaphore(max(1, min(4, int(settings.binance_factor_concurrency or 4))))

    async def load(symbol: str) -> tuple[str, dict[str, Any]]:
        async with semaphore:
            errors: list[str] = []
            candles = await fetch_klines(client, symbol, interval, interval_ms, start_ms, end_ms)
            quality_errors = candle_quality_errors(candles)
            if quality_errors:
                return symbol, {"candles": [], "open_interest": {}, "errors": quality_errors}
            oi = {}
            if fetch_oi:
                try:
                    oi = await fetch_open_interest_hist(client, symbol, interval, interval_ms, start_ms, end_ms)
                except Exception as exc:
                    errors.append(f"open_interest:{type(exc).__name__}: {exc}")
            return symbol, {"candles": candles, "open_interest": oi, "errors": errors}

    pairs = await asyncio.gather(*(load(symbol) for symbol in symbols), return_exceptions=True)
    out: dict[str, dict[str, Any]] = {}
    for idx, result in enumerate(pairs):
        symbol = symbols[idx]
        if isinstance(result, Exception):
            out[symbol] = {"candles": [], "open_interest": {}, "errors": [f"{type(result).__name__}: {result}"]}
            continue
        out[result[0]] = result[1]
    return out


def cached_symbols(interval: str, limit: int) -> list[str]:
    root = Path(settings.jesse_data_path) / "candles" / "binance_futures" / interval
    if not root.exists():
        raise RuntimeError(f"candle_cache_missing:{root}")
    available = sorted(path.stem.upper() for path in root.glob("*.jsonl") if path.stat().st_size > 0)
    if not available:
        raise RuntimeError(f"candle_cache_empty:{root}")
    valid = set(available)
    selected: list[str] = []

    def add(symbol: str) -> None:
        if symbol in valid and symbol not in selected and len(selected) < limit:
            selected.append(symbol)

    for symbol in PRIORITY_SYMBOLS:
        add(symbol)
    for symbol in available:
        add(symbol)
        if len(selected) >= limit:
            break
    return selected


def load_cached_market_data(symbols: list[str], interval: str, start_ms: int, end_ms: int) -> dict[str, dict[str, Any]]:
    root = Path(settings.jesse_data_path) / "candles" / "binance_futures" / interval
    out: dict[str, dict[str, Any]] = {}
    for symbol in symbols:
        path = root / f"{symbol.upper()}.jsonl"
        errors: list[str] = []
        candles: list[Candle] = []
        if not path.exists():
            out[symbol] = {"candles": [], "open_interest": {}, "errors": [f"candle_cache_missing:{path}"]}
            continue
        with path.open("r", encoding="utf-8") as fh:
            for line_no, line in enumerate(fh, start=1):
                text = line.strip()
                if not text:
                    continue
                try:
                    candle = parse_candle(json.loads(text))
                except Exception as exc:
                    errors.append(f"cache_parse:{line_no}:{type(exc).__name__}: {exc}")
                    continue
                if candle and start_ms <= candle.open_time <= end_ms and candle.close_time <= now_ms():
                    candles.append(candle)
        if not candles:
            errors.append("candle_cache_no_rows_in_requested_window")
        quality_errors = candle_quality_errors(candles)
        if quality_errors:
            errors.extend(quality_errors)
            candles = []
        out[symbol] = {"candles": candles, "open_interest": {}, "errors": errors}
    return out


async def fetch_klines(
    client: httpx.AsyncClient,
    symbol: str,
    interval: str,
    interval_ms: int,
    start_ms: int,
    end_ms: int,
) -> list[Candle]:
    rows: dict[int, Candle] = {}
    cursor = start_ms
    while cursor < end_ms:
        data = await get_json(
            client,
            "/fapi/v1/klines",
            {"symbol": symbol, "interval": interval, "startTime": cursor, "endTime": end_ms, "limit": 1500},
        )
        if not isinstance(data, list) or not data:
            break
        for row in data:
            candle = parse_candle(row)
            if candle and start_ms <= candle.open_time <= end_ms and candle.close_time <= now_ms():
                rows[candle.open_time] = candle
        last_open = int(_f(data[-1][0]))
        next_cursor = last_open + interval_ms
        if next_cursor <= cursor:
            break
        cursor = next_cursor
        await asyncio.sleep(0.03)
    return [rows[key] for key in sorted(rows)]


async def fetch_open_interest_hist(
    client: httpx.AsyncClient,
    symbol: str,
    period: str,
    interval_ms: int,
    start_ms: int,
    end_ms: int,
) -> dict[int, float]:
    rows: dict[int, float] = {}
    cursor = start_ms
    while cursor < end_ms:
        data = await get_json(
            client,
            "/futures/data/openInterestHist",
            {"symbol": symbol, "period": period, "startTime": cursor, "endTime": end_ms, "limit": 500},
            allow_empty=True,
        )
        if not isinstance(data, list) or not data:
            break
        for row in data:
            if not isinstance(row, dict):
                continue
            ts = int(_f(row.get("timestamp")))
            value = _f(row.get("sumOpenInterest"))
            if ts > 0 and value > 0:
                rows[ts] = value
        last_ts = int(_f(data[-1].get("timestamp") if isinstance(data[-1], dict) else 0))
        next_cursor = last_ts + interval_ms
        if next_cursor <= cursor:
            break
        cursor = next_cursor
        await asyncio.sleep(0.03)
    return rows


async def get_json(client: httpx.AsyncClient, path: str, params: dict[str, Any], *, allow_empty: bool = False) -> Any:
    for attempt in range(MAX_HTTP_ATTEMPTS):
        await throttle_binance_request()
        try:
            response = await client.get(path, params=params)
        except (httpx.ConnectError, httpx.ReadTimeout, httpx.ConnectTimeout, httpx.RemoteProtocolError):
            if attempt >= MAX_HTTP_ATTEMPTS - 1:
                raise
            await asyncio.sleep(min(30.0, 2.0 * (attempt + 1)))
            continue
        if response.status_code in {418, 429} and attempt < MAX_HTTP_ATTEMPTS - 1:
            await asyncio.sleep(retry_after_seconds(response, attempt))
            continue
        if response.status_code >= 400:
            if allow_empty and response.status_code in {400, 404}:
                return []
            raise RuntimeError(f"binance_http_{response.status_code}:{response.text[:160]}")
        if not response.text.strip():
            return [] if allow_empty else None
        return response.json()
    raise RuntimeError("binance_http_retry_exhausted")


async def throttle_binance_request() -> None:
    global _REQUEST_LOCK, _NEXT_REQUEST_AT
    if _REQUEST_LOCK is None:
        _REQUEST_LOCK = asyncio.Lock()
    async with _REQUEST_LOCK:
        now = time.monotonic()
        wait_for = _NEXT_REQUEST_AT - now
        if wait_for > 0:
            await asyncio.sleep(wait_for)
        _NEXT_REQUEST_AT = time.monotonic() + REQUEST_DELAY_SECONDS


def retry_after_seconds(response: httpx.Response, attempt: int) -> float:
    header = response.headers.get("retry-after")
    try:
        if header:
            return min(120.0, max(5.0, float(header)))
    except ValueError:
        pass
    return min(120.0, 15.0 * (attempt + 1))


def build_signals(fetched: dict[str, dict[str, Any]], interval: str, interval_ms: int) -> tuple[dict[int, list[SignalRow]], list[str]]:
    rows_by_entry: dict[int, list[SignalRow]] = {}
    limitations: set[str] = {
        "historical_depth_unavailable_depth_imbalance_set_to_zero",
        "historical_funding_not_replayed_funding_rate_set_to_zero",
        "entry_uses_next_candle_open_no_lookahead",
    }
    weights = radar_weight_calibrator.weights()
    for symbol, payload in fetched.items():
        candles: list[Candle] = payload.get("candles") or []
        oi = payload.get("open_interest") or {}
        if len(candles) < 80:
            limitations.add(f"{symbol}:candles_low")
            continue
        if not oi:
            limitations.add(f"{symbol}:open_interest_history_missing")
        score_history: list[float] = []
        prev_sm = 0.0
        oi_values = oi_values_for_candles(candles, oi)
        for idx in range(30, len(candles) - 2):
            snapshot = snapshot_at(symbol, candles, oi_values, idx)
            side = direction(snapshot)
            fake, fake_score = fake_breakout(snapshot, side)
            sm_position, sm_delta = smart_money_estimate(snapshot, prev_sm)
            prev_sm = sm_position
            heat_score = 0.0 if len(score_history) < 2 else max(0.0, min(100.0, (score_history[-1] - score_history[0]) * 2.0))
            features = score_engine.feature_scores(snapshot, sm_position, heat_score, fake_score)
            heat_slope = 0.0 if len(score_history) < 2 else round(score_history[-1] - score_history[-2], 4)
            slope_score = clamp(50.0 + heat_slope * 4.0)
            fund_count, fund_total = fund_confirm(snapshot, side)
            fund_components = fund_confirm_components(snapshot, side)
            dealer = dealer_label(snapshot, side, sm_delta, fund_count, fake)
            anomaly_score = score_engine.total(features, weights=weights)
            item = RadarItem(
                rank=999,
                symbol=symbol,
                base_asset=symbol.replace("USDT", ""),
                price=snapshot.price,
                direction=side,
                stage="market_backtest",
                trigger_mode="candle_replay",
                score=0.0,
                score_history=list(score_history),
                rank_history=[],
                heat_slope=heat_slope,
                slope_score=slope_score,
                fake_breakout_risk=fake,
                change_5m=snapshot.change_5m,
                change_15m=snapshot.change_15m,
                change_1h=snapshot.change_1h,
                oi_change=snapshot.oi_change,
                fund_confirm_count=fund_count,
                fund_confirm_total=fund_total,
                dealer_radar=dealer,
                sm_position=sm_position,
                sm_delta=sm_delta,
                volume_spike=snapshot.volume_spike,
                funding_rate=snapshot.funding_rate,
                taker_buy_ratio=snapshot.taker_buy_ratio,
                taker_sell_ratio=snapshot.taker_sell_ratio,
                depth_imbalance=snapshot.depth_imbalance,
                atr_pct=snapshot.atr_pct,
                wick_ratio=snapshot.wick_ratio,
                score_features=features,
                score_explain={},
                ts_ms=candles[idx].close_time,
            )
            quality_score, quality_explain = backtest_trade_quality_score(
                item,
                anomaly_score,
                fund_components,
                weights,
                {},
            )
            item.score = quality_score
            item.score_features = {
                **features,
                "anomaly_score": anomaly_score,
                "trade_quality_score": quality_score,
                "fund_confirm_components": fund_components,
                "rank_model": "production_trade_quality_v2",
            }
            item.score_explain = quality_explain
            score_history.append(quality_score)
            score_history = score_history[-12:]
            feature = market_feature_report(item)
            ok, reasons = production_candidate_check(item, feature)
            if not ok:
                continue
            entry_time = candles[idx + 1].open_time
            rows_by_entry.setdefault(entry_time, []).append(
                SignalRow(
                    entry_time=entry_time,
                    signal_index=idx,
                    item=item,
                    feature_score=feature["feature_score"],
                    estimated_win_rate=feature["estimated_win_rate"],
                    selection_score=feature["selection_score"],
                    candidate_reasons=reasons,
                )
            )

    for entry_time, rows in rows_by_entry.items():
        rows.sort(key=lambda row: row.item.score, reverse=True)
        for rank, row in enumerate(rows, start=1):
            row.item.rank = rank
    return rows_by_entry, sorted(limitations)


def backtest_trade_quality_score(
    item: RadarItem,
    anomaly_score: float,
    fund_components: dict[str, bool],
    score_weights: dict,
    score_calibration: dict,
) -> tuple[float, dict]:
    return _BACKTEST_RADAR_ENGINE._scan_quality_score(
        item,
        anomaly_score,
        fund_components,
        score_weights,
        score_calibration,
    )


def snapshot_at(symbol: str, candles: list[Candle], oi_values: list[float], idx: int) -> MarketSnapshot:
    close = candles[idx].close
    recent = candles[idx - 2 : idx + 1]
    baseline = candles[max(0, idx - 14) : idx - 2] or candles[max(0, idx - 20) : idx + 1]
    recent_avg_quote = sum(row.quote_volume for row in recent) / max(1, len(recent))
    baseline_avg_quote = sum(row.quote_volume for row in baseline) / max(1, len(baseline))
    volume_spike = recent_avg_quote / baseline_avg_quote if baseline_avg_quote > 0 else 1.0

    ranges = []
    wick_ratios = []
    for row in candles[max(0, idx - 13) : idx + 1]:
        if row.close > 0:
            ranges.append((row.high - row.low) / row.close * 100.0)
        candle_range = max(0.0, row.high - row.low)
        if candle_range > 0:
            upper = max(0.0, row.high - max(row.open, row.close))
            lower = max(0.0, min(row.open, row.close) - row.low)
            wick_ratios.append(max(upper, lower) / candle_range)

    quote_volume = sum(row.quote_volume for row in recent)
    taker_buy_quote = sum(row.taker_buy_quote for row in recent)
    taker_buy_ratio = taker_buy_quote / quote_volume if quote_volume > 0 else 0.5
    taker_buy_ratio = max(0.0, min(1.0, taker_buy_ratio))

    oi_current = oi_values[idx] if idx < len(oi_values) else 0.0
    oi_previous = oi_values[idx - 30] if idx >= 30 and idx - 30 < len(oi_values) else 0.0
    oi_change = _pct(oi_current, oi_previous) if oi_current > 0 and oi_previous > 0 else 0.0

    return MarketSnapshot(
        symbol=symbol,
        price=close,
        change_5m=_pct(close, candles[idx - 1].close),
        change_15m=_pct(close, candles[idx - 3].close),
        change_1h=_pct(close, candles[idx - 12].close),
        volume_spike=max(0.0, volume_spike),
        oi_change=oi_change,
        funding_rate=0.0,
        taker_buy_ratio=taker_buy_ratio,
        taker_sell_ratio=1.0 - taker_buy_ratio,
        depth_imbalance=0.0,
        atr_pct=sum(ranges) / max(1, len(ranges)),
        wick_ratio=max(wick_ratios) if wick_ratios else 0.0,
        ts_ms=candles[idx].close_time,
    )


def oi_values_for_candles(candles: list[Candle], oi: dict[int, float]) -> list[float]:
    if not oi:
        return [0.0 for _ in candles]
    keys = sorted(oi)
    out: list[float] = []
    pos = 0
    last = 0.0
    for candle in candles:
        while pos < len(keys) and keys[pos] <= candle.close_time:
            last = oi[keys[pos]]
            pos += 1
        out.append(last)
    return out


def smart_money_estimate(snapshot: MarketSnapshot, previous: float) -> tuple[float, float]:
    oi_abnormal = clamp(abs(snapshot.oi_change) / 2.5 * 100.0)
    depth_score = clamp(abs(snapshot.depth_imbalance) * 100.0)
    vol_score = clamp((snapshot.volume_spike - 0.5) / 3.0 * 100.0)
    taker_score = clamp(abs(snapshot.taker_buy_ratio - 0.5) / 0.2 * 100.0)
    funding_div = clamp(abs(snapshot.funding_rate) / 0.0008 * 100.0)
    position = round(clamp(oi_abnormal * 0.25 + depth_score * 0.15 + vol_score * 0.2 + taker_score * 0.25 + funding_div * 0.15), 2)
    return position, round(position - previous, 2)


def market_feature_report(item: RadarItem) -> dict[str, float]:
    contributions = candidate_feature_enhancer._contributions(item)
    feature_score = candidate_feature_enhancer._feature_score(contributions)
    estimated = candidate_feature_enhancer._feature_win_rate(item, feature_score)
    floor = candidate_feature_enhancer._current_signal_floor(item, feature_score, contributions)
    if floor > 0 and estimated < floor:
        estimated = floor
    estimated = max(0.05, min(0.82, estimated))
    selection = candidate_feature_enhancer._selection_score(item, feature_score, estimated, 0, 0.0, 0, 0.0)
    return {
        "feature_score": round(feature_score, 4),
        "estimated_win_rate": round(estimated, 4),
        "selection_score": round(selection, 4),
    }


def production_candidate_check(item: RadarItem, feature: dict[str, float]) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    if item.direction not in {"LONG", "SHORT"}:
        reasons.append("direction_neutral")
    if item.fake_breakout_risk == "HIGH":
        reasons.append("fake_breakout_high")
    elif item.fake_breakout_risk != "LOW":
        reasons.append("fake_breakout_not_low")
    if item.fund_confirm_count < min(3, item.fund_confirm_total):
        reasons.append("fund_confirm_below_3")
    confirms = direction_confirmations(item)
    if confirms < 4:
        reasons.append("direction_confirmations_low")
    if not timeframe_fully_aligned(item):
        reasons.append("timeframe_not_fully_aligned")
    if "trap" in str(item.dealer_radar or "").lower():
        reasons.append("dealer_trap")
    if risk_fraction(item) < 0.007:
        reasons.append("stop_structure_too_tight_for_recent_market")
    if item.direction == "LONG" and float(item.change_5m or 0.0) > 3.0:
        reasons.append("long_chase_displacement_high")
    if item.direction == "SHORT" and float(item.change_5m or 0.0) < -3.0:
        reasons.append("short_chase_displacement_high")

    min_win = max(0.54, min(float(settings.strategy_min_live_win_rate or 0.58), float(settings.strategy_min_paper_win_rate or 0.60)) - 0.02)
    if float(feature["estimated_win_rate"]) < min_win:
        reasons.append("cyqnt_win_rate_low")
    if float(feature["feature_score"]) < 46.0:
        reasons.append("cyqnt_feature_score_low")

    wick = float(item.wick_ratio or 0.0)
    if item.fake_breakout_risk == "LOW":
        if wick > min(0.55, max(0.0, float(settings.paper_probe_max_wick_ratio or 0.55))):
            reasons.append("wick_too_high")
    elif wick > 0.68:
        reasons.append("wick_too_high_for_medium_fake")

    raw_score = float(item.score or 0.0)
    selection_score = float(feature["selection_score"])
    strong_enhanced = selection_score >= 66.0 and float(feature["feature_score"]) >= 54.0
    strong_raw = raw_score >= 58.0
    exceptional = (
        item.fake_breakout_risk == "LOW"
        and confirms >= 5
        and selection_score >= 72.0
        and float(feature["estimated_win_rate"]) >= max(min_win, 0.56)
    )
    if not (strong_raw or strong_enhanced or exceptional):
        reasons.append("production_score_low")
    return not reasons, reasons


def recent_market_side_block(item: RadarItem) -> bool:
    if item.direction not in {"LONG", "SHORT"}:
        return False
    try:
        market = learning_data_audit.summary().get("market_backtest") or {}
        current_ms = now_ms()
        for block in market.get("side_blocks") or []:
            if str(block.get("side") or "").upper() == item.direction and market_side_block_active(block, current_ms):
                return True
        if not market_side_report_fresh(market, current_ms):
            return False
        metrics = (market.get("by_side_metrics") or {}).get(item.direction) or {}
    except Exception:
        return False
    trades = int(_f(metrics.get("trades")))
    return bool(trades and market_side_block_reason(metrics))


def current_market_guard(by_side_metrics: dict[str, Any] | None = None) -> dict[str, Any]:
    try:
        market = learning_data_audit.summary().get("market_backtest") or {}
    except Exception:
        market = {}
    metrics = by_side_metrics if by_side_metrics is not None else (market.get("by_side_metrics") or {})
    side_blocks = side_blocks_from_market_metrics(metrics, market.get("side_blocks") or [])
    return {
        "side_blocks": side_blocks,
        "candidate_filters": {
            "min_replay_risk_pct": 0.7,
            "max_low_fake_wick_ratio": 0.55,
            "max_abs_5m_chase_pct": 3.0,
        },
        "source": "latest_market_backtest_report",
    }


def direction_confirmations(item: RadarItem) -> int:
    if item.direction == "LONG":
        checks = [
            item.change_5m > 0,
            item.change_15m > 0,
            item.change_1h >= 0,
            item.taker_buy_ratio >= 0.55,
            item.depth_imbalance >= 0.08,
            item.sm_delta >= 0,
            item.volume_spike >= 1.3,
            item.wick_ratio <= 0.55,
        ]
    elif item.direction == "SHORT":
        checks = [
            item.change_5m < 0,
            item.change_15m < 0,
            item.change_1h <= 0,
            item.taker_sell_ratio >= 0.55,
            item.depth_imbalance <= -0.08,
            item.sm_delta <= 0,
            item.volume_spike >= 1.3,
            item.wick_ratio <= 0.55,
        ]
    else:
        return 0
    return sum(1 for ok in checks if ok)


def timeframe_fully_aligned(item: RadarItem) -> bool:
    if item.direction == "LONG":
        return item.change_5m > 0 and item.change_15m > 0 and item.change_1h >= 0
    if item.direction == "SHORT":
        return item.change_5m < 0 and item.change_15m < 0 and item.change_1h <= 0
    return False


def run_trades(fetched: dict[str, dict[str, Any]], rows_by_entry: dict[int, list[SignalRow]], horizon_bars: int) -> list[dict[str, Any]]:
    trades: list[dict[str, Any]] = []
    last_exit_time = 0
    for entry_time in sorted(rows_by_entry):
        if entry_time < last_exit_time:
            continue
        rows = sorted(rows_by_entry[entry_time], key=signal_rank_key, reverse=True)
        if not rows:
            continue
        row = rows[0]
        candles: list[Candle] = fetched[row.item.symbol]["candles"]
        trade = simulate_trade(row, candles, horizon_bars)
        if not trade:
            continue
        trades.append(trade)
        last_exit_time = int(trade["exit_time_ms"])
    return trades


def signal_rank_key(row: SignalRow) -> tuple[float, float, int, float, float, int]:
    item = row.item
    fake_bonus = 8.0 if item.fake_breakout_risk == "LOW" else 0.0
    wick_penalty = max(0.0, float(item.wick_ratio or 0.0) - 0.55) * 20.0
    return (
        float(row.estimated_win_rate),
        float(row.selection_score) + fake_bonus - wick_penalty,
        direction_confirmations(item),
        float(row.feature_score),
        float(item.score or 0.0),
        -int(item.rank or 999),
    )


def simulate_trade(row: SignalRow, candles: list[Candle], horizon_bars: int) -> dict[str, Any] | None:
    item = row.item
    entry_index = row.signal_index + 1
    if entry_index >= len(candles):
        return None
    entry_candle = candles[entry_index]
    entry_price = entry_candle.open
    risk_pct = risk_fraction(item)
    if entry_price <= 0 or risk_pct <= 0:
        return None
    tp_r = max(1.0, float(settings.replay_tp_r or 2.0))
    if item.direction == "LONG":
        stop_loss = entry_price * (1.0 - risk_pct)
        tp1 = entry_price * (1.0 + risk_pct)
        take_profit = entry_price * (1.0 + risk_pct * tp_r)
    else:
        stop_loss = entry_price * (1.0 + risk_pct)
        tp1 = entry_price * (1.0 - risk_pct)
        take_profit = entry_price * (1.0 - risk_pct * tp_r)

    exit_price = candles[min(len(candles) - 1, entry_index + horizon_bars - 1)].close
    exit_time = candles[min(len(candles) - 1, entry_index + horizon_bars - 1)].close_time
    close_reason = "TIMEOUT"
    mfe_r = 0.0
    mae_r = 0.0
    stage = "Stage 1"
    realized_net_r = 0.0
    realized_gross_r = 0.0
    realized_cost_r = 0.0
    remaining_ratio = 1.0
    tp1_close_ratio = 0.5
    locked_stop = stop_loss
    atr_trail_pct = min(max(float(item.atr_pct or 0.0) / 100.0 * 0.9, 0.004), 0.025)
    best_price = entry_price
    for future in candles[entry_index : min(len(candles), entry_index + horizon_bars)]:
        if item.direction == "LONG":
            best_price = max(best_price, future.high)
            mfe_r = max(mfe_r, (future.high / entry_price - 1.0) / risk_pct)
            mae_r = min(mae_r, (future.low / entry_price - 1.0) / risk_pct)
            if stage == "Stage 2":
                locked_stop = max(locked_stop, best_price * (1.0 - atr_trail_pct), entry_price * (1.0 + risk_pct * 0.2))
            hit_stop = future.low <= locked_stop
            hit_tp1 = future.high >= tp1
            hit_target = future.high >= take_profit
        else:
            best_price = min(best_price, future.low)
            mfe_r = max(mfe_r, (1.0 - future.low / entry_price) / risk_pct)
            mae_r = min(mae_r, (1.0 - future.high / entry_price) / risk_pct)
            if stage == "Stage 2":
                locked_stop = min(locked_stop, best_price * (1.0 + atr_trail_pct), entry_price * (1.0 - risk_pct * 0.2))
            hit_stop = future.high >= locked_stop
            hit_tp1 = future.low <= tp1
            hit_target = future.low <= take_profit
        if hit_stop and hit_target:
            exit_price = locked_stop
            exit_time = future.close_time
            close_reason = "AMBIGUOUS_STOP_FIRST"
            break
        if hit_stop:
            exit_price = locked_stop
            exit_time = future.close_time
            close_reason = "SL" if stage == "Stage 1" else "LOCKED_STOP"
            break
        if hit_target:
            if stage == "Stage 1":
                leg_net, leg_gross, leg_cost = net_r_multiple(item.direction, entry_price, tp1, risk_pct)
                realized_net_r += tp1_close_ratio * leg_net
                realized_gross_r += tp1_close_ratio * leg_gross
                realized_cost_r += tp1_close_ratio * leg_cost
                remaining_ratio = 1.0 - tp1_close_ratio
            exit_price = take_profit
            exit_time = future.close_time
            close_reason = "TP"
            break
        if stage == "Stage 1" and hit_tp1:
            leg_net, leg_gross, leg_cost = net_r_multiple(item.direction, entry_price, tp1, risk_pct)
            realized_net_r += tp1_close_ratio * leg_net
            realized_gross_r += tp1_close_ratio * leg_gross
            realized_cost_r += tp1_close_ratio * leg_cost
            remaining_ratio = 1.0 - tp1_close_ratio
            stage = "Stage 2"
            if item.direction == "LONG":
                locked_stop = max(entry_price * (1.0 + risk_pct * 0.2), best_price * (1.0 - atr_trail_pct))
            else:
                locked_stop = min(entry_price * (1.0 - risk_pct * 0.2), best_price * (1.0 + atr_trail_pct))

    final_net_r, final_gross_r, final_cost_r = net_r_multiple(item.direction, entry_price, exit_price, risk_pct)
    net_r = realized_net_r + remaining_ratio * final_net_r
    gross_r = realized_gross_r + remaining_ratio * final_gross_r
    cost_r = realized_cost_r + remaining_ratio * final_cost_r
    return {
        "symbol": item.symbol,
        "side": item.direction,
        "entry_time_ms": entry_candle.open_time,
        "entry_time": iso_ms(entry_candle.open_time),
        "signal_time_ms": item.ts_ms,
        "exit_time_ms": exit_time,
        "exit_time": iso_ms(exit_time),
        "rank": item.rank,
        "score": item.score,
        "feature_score": row.feature_score,
        "estimated_win_rate": row.estimated_win_rate,
        "selection_score": row.selection_score,
        "entry_price": round(entry_price, 10),
        "exit_price": round(exit_price, 10),
        "stop_loss": round(stop_loss, 10),
        "tp1": round(tp1, 10),
        "take_profit": round(take_profit, 10),
        "locked_stop": round(locked_stop, 10),
        "remaining_ratio": round(remaining_ratio, 4),
        "risk_pct": round(risk_pct * 100.0, 6),
        "net_r": round(net_r, 6),
        "gross_r": round(gross_r, 6),
        "cost_r": round(cost_r, 6),
        "mfe_r": round(mfe_r, 6),
        "mae_r": round(mae_r, 6),
        "win": net_r > 0,
        "close_reason": close_reason,
        "hold_bars": max(1, round((exit_time - entry_candle.open_time) / max(1, INTERVAL_MS.get(settings.binance_kline_interval, 300_000)))),
        "candidate": {
            "fund_confirm": f"{item.fund_confirm_count}/{item.fund_confirm_total}",
            "fake_breakout_risk": item.fake_breakout_risk,
            "direction_confirmations": direction_confirmations(item),
            "change_5m": item.change_5m,
            "change_15m": item.change_15m,
            "change_1h": item.change_1h,
            "volume_spike": item.volume_spike,
            "oi_change": item.oi_change,
            "taker_buy_ratio": item.taker_buy_ratio,
            "taker_sell_ratio": item.taker_sell_ratio,
            "wick_ratio": item.wick_ratio,
        },
    }


def risk_fraction(item: RadarItem) -> float:
    atr = max(0.0, float(item.atr_pct or 0.0)) / 100.0
    raw = atr * max(0.1, float(settings.replay_atr_risk_mult or 0.9))
    return min(max(raw, float(settings.replay_min_risk_pct or 0.006)), float(settings.replay_max_risk_pct or 0.025))


def net_r_multiple(side: str, entry: float, exit_price: float, risk_pct: float) -> tuple[float, float, float]:
    fee = max(0.0, float(settings.paper_taker_fee_rate or 0.0))
    slip = max(0.0, float(settings.paper_slippage_pct or 0.0))
    if side == "LONG":
        entry_exec = entry * (1.0 + slip)
        exit_exec = exit_price * (1.0 - slip)
        gross_pct = exit_exec / entry_exec - 1.0
    else:
        entry_exec = entry * (1.0 - slip)
        exit_exec = exit_price * (1.0 + slip)
        gross_pct = 1.0 - exit_exec / entry_exec
    gross_r = gross_pct / max(risk_pct, 0.0001)
    cost_r = (2.0 * fee) / max(risk_pct, 0.0001)
    return gross_r - cost_r, gross_r, cost_r


def build_report(
    *,
    args: argparse.Namespace,
    started_ms: int,
    start_ms: int,
    end_ms: int,
    fetched: dict[str, dict[str, Any]],
    signals: dict[int, list[SignalRow]],
    trades: list[dict[str, Any]],
    limitations: list[str],
) -> dict[str, Any]:
    symbols_loaded = [symbol for symbol, payload in fetched.items() if payload.get("candles")]
    candle_counts = {symbol: len(payload.get("candles") or []) for symbol, payload in fetched.items()}
    data_errors = {symbol: payload.get("errors") or [] for symbol, payload in fetched.items() if payload.get("errors")}
    all_signal_rows = [row for rows in signals.values() for row in rows]
    sorted_trades = sorted(trades, key=lambda row: int(row["entry_time_ms"]))
    split = max(0, min(len(sorted_trades), int(math.floor(len(sorted_trades) * float(settings.evolve_train_split or 0.7)))))
    train = sorted_trades[:split]
    holdout = sorted_trades[split:]
    metrics = {
        "overall": summarize_trades(sorted_trades),
        "train": summarize_trades(train),
        "holdout": summarize_trades(holdout),
        "by_side": {
            "LONG": summarize_trades([row for row in sorted_trades if row["side"] == "LONG"]),
            "SHORT": summarize_trades([row for row in sorted_trades if row["side"] == "SHORT"]),
        },
        "by_close_reason": summarize_by_reason(sorted_trades),
    }
    blockers = validation_blockers(metrics)
    return {
        "report_type": "market_backtest",
        "engine": "project_market_backtester",
        "version": 1,
        "generated_at_ms": now_ms(),
        "generated_at": iso_ms(now_ms()),
        "started_at": iso_ms(started_ms),
        "source": {
            "exchange": "binance_usdt_m_futures",
            "base_url": BASE_URL,
            "market_data": getattr(args, "market_data_source", "mainnet_public"),
            "interval": args.interval,
            "days_requested": float(args.days),
            "symbol_limit": int(args.symbol_limit),
            "symbols_requested": list(fetched.keys()),
            "symbols_loaded": symbols_loaded,
            "limitations": limitations,
            "data_errors": data_errors,
        },
        "policy": {
            "candidate_rules": "radar_strict_market_only",
            "no_lookahead": True,
            "signal_candle": "closed",
            "entry": "next_candle_open",
            "stop_model": "ATR percent * replay_atr_risk_mult with replay min/max risk bounds",
            "position_lifecycle_model": "stage1_tp1_partial_then_stage2_locked_stop_and_atr_trailing",
            "tp1_r": 1.0,
            "tp1_close_ratio": 0.5,
            "take_profit_r": float(settings.replay_tp_r),
            "stage2_locked_stop_r": 0.2,
            "same_candle_ambiguity": "stop_first",
            "horizon_bars": int(args.horizon_bars),
            "fee_rate_per_side": float(settings.paper_taker_fee_rate),
            "slippage_per_side_pct": float(settings.paper_slippage_pct),
            "train_split": float(settings.evolve_train_split),
        },
        "data": {
            "from_ms": start_ms,
            "from": iso_ms(start_ms),
            "to_ms": end_ms,
            "to": iso_ms(end_ms),
            "span_days": round(max(0, end_ms - start_ms) / 86_400_000.0, 4),
            "candle_counts": candle_counts,
            "data_errors": data_errors,
            "signal_count": len(all_signal_rows),
            "trade_count_after_non_overlap": len(sorted_trades),
        },
        "metrics": metrics,
        "market_guard": current_market_guard(metrics["by_side"]),
        "validation": {
            "passed": not blockers,
            "blockers": blockers,
            "minimums": {
                "trades": int(settings.evolve_min_backtest_trades),
                "holdout_trades": int(settings.evolve_min_holdout_trades),
                "win_rate": float(settings.evolve_min_win_rate),
                "holdout_win_rate": float(settings.evolve_min_holdout_win_rate),
                "profit_factor": float(settings.evolve_min_profit_factor),
                "net_pnl_r": float(settings.evolve_min_net_pnl),
            },
        },
        "sample_trades": sorted_trades[-20:],
    }


def validation_blockers(metrics: dict[str, Any]) -> list[str]:
    overall = metrics["overall"]
    holdout = metrics["holdout"]
    blockers: list[str] = []
    if int(overall["trades"]) < int(settings.evolve_min_backtest_trades):
        blockers.append("market_backtest_trades_low")
    if int(holdout["trades"]) < int(settings.evolve_min_holdout_trades):
        blockers.append("market_backtest_holdout_trades_low")
    if float(overall["win_rate"]) < float(settings.evolve_min_win_rate):
        blockers.append("market_backtest_win_rate_low")
    if float(holdout["win_rate"]) < float(settings.evolve_min_holdout_win_rate):
        blockers.append("market_backtest_holdout_win_rate_low")
    if float(overall["profit_factor"]) < float(settings.evolve_min_profit_factor):
        blockers.append("market_backtest_profit_factor_low")
    if float(overall["net_pnl_r"]) <= float(settings.evolve_min_net_pnl):
        blockers.append("market_backtest_net_pnl_not_positive")
    if int(holdout["trades"]) > 0 and float(holdout["net_pnl_r"]) <= 0:
        blockers.append("market_backtest_holdout_pnl_not_positive")
    return blockers


def summarize_trades(trades: list[dict[str, Any]]) -> dict[str, Any]:
    values = [float(row.get("net_r") or 0.0) for row in trades]
    wins = [value for value in values if value > 0]
    losses = [value for value in values if value < 0]
    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    equity = 0.0
    peak = 0.0
    max_dd = 0.0
    for value in values:
        equity += value
        peak = max(peak, equity)
        max_dd = min(max_dd, equity - peak)
    return {
        "trades": len(values),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(len(wins) / max(1, len(wins) + len(losses)), 4),
        "profit_factor": round(gross_profit / gross_loss, 4) if gross_loss > 0 else (999.0 if gross_profit > 0 else 0.0),
        "net_pnl_r": round(sum(values), 6),
        "avg_r": round(sum(values) / max(1, len(values)), 6),
        "max_drawdown_r": round(abs(max_dd), 6),
    }


def summarize_by_reason(trades: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for reason in sorted({str(row.get("close_reason") or "") for row in trades}):
        out[reason] = summarize_trades([row for row in trades if row.get("close_reason") == reason])
    return out


def failure_report(args: argparse.Namespace, started_ms: int, start_ms: int, end_ms: int, error: str) -> dict[str, Any]:
    return {
        "report_type": "market_backtest",
        "engine": "project_market_backtester",
        "version": 1,
        "generated_at_ms": now_ms(),
        "generated_at": iso_ms(now_ms()),
        "started_at": iso_ms(started_ms),
        "source": {
            "exchange": "binance_usdt_m_futures",
            "base_url": BASE_URL,
            "market_data": getattr(args, "market_data_source", "mainnet_public"),
            "interval": args.interval,
            "days_requested": float(args.days),
            "limitations": ["data_fetch_failed"],
        },
        "data": {"from_ms": start_ms, "from": iso_ms(start_ms), "to_ms": end_ms, "to": iso_ms(end_ms), "span_days": 0.0},
        "metrics": {"overall": summarize_trades([]), "train": summarize_trades([]), "holdout": summarize_trades([]), "by_side": {}},
        "validation": {"passed": False, "blockers": ["market_backtest_data_fetch_failed", error]},
        "sample_trades": [],
    }


def write_candles(fetched: dict[str, dict[str, Any]], interval: str) -> None:
    root = Path(settings.jesse_data_path) / "candles" / "binance_futures" / interval
    root.mkdir(parents=True, exist_ok=True)
    for symbol, payload in fetched.items():
        candles: list[Candle] = payload.get("candles") or []
        if not candles:
            continue
        path = root / f"{symbol}.jsonl"
        with path.open("w", encoding="utf-8", newline="\n") as fh:
            for candle in candles:
                fh.write(json.dumps(asdict(candle), ensure_ascii=False, separators=(",", ":")) + "\n")


def write_reports(report: dict[str, Any], trades: list[dict[str, Any]], tag: str) -> None:
    reports_dir = ROOT / "trading_lab" / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    suffix = "latest" if tag == "latest" else f"{tag}_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    json_path = reports_dir / f"market_backtest_{suffix}.json"
    latest_json = reports_dir / "market_backtest_latest.json"
    trades_csv = reports_dir / f"market_backtest_{suffix}_trades.csv"
    latest_trades_csv = reports_dir / "market_backtest_latest_trades.csv"
    md_path = reports_dir / f"market_backtest_{suffix}.md"
    latest_md = reports_dir / "market_backtest_latest.md"

    text = json.dumps(report, ensure_ascii=False, indent=2)
    json_path.write_text(text, encoding="utf-8")
    latest_json.write_text(text, encoding="utf-8")
    write_trades_csv(trades_csv, trades)
    write_trades_csv(latest_trades_csv, trades)
    markdown = report_markdown(report)
    md_path.write_text(markdown, encoding="utf-8")
    latest_md.write_text(markdown, encoding="utf-8")


def write_trades_csv(path: Path, trades: list[dict[str, Any]]) -> None:
    fields = [
        "symbol",
        "side",
        "entry_time",
        "exit_time",
        "rank",
        "score",
        "feature_score",
        "estimated_win_rate",
        "selection_score",
        "candidate_fund_confirm",
        "candidate_fake_breakout_risk",
        "candidate_direction_confirmations",
        "candidate_change_5m",
        "candidate_change_15m",
        "candidate_change_1h",
        "candidate_volume_spike",
        "candidate_oi_change",
        "candidate_taker_buy_ratio",
        "candidate_taker_sell_ratio",
        "candidate_wick_ratio",
        "entry_price",
        "exit_price",
        "stop_loss",
        "take_profit",
        "risk_pct",
        "net_r",
        "gross_r",
        "cost_r",
        "mfe_r",
        "mae_r",
        "win",
        "close_reason",
    ]
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        for row in trades:
            candidate = row.get("candidate") if isinstance(row.get("candidate"), dict) else {}
            flat = {key: row.get(key, "") for key in fields}
            flat.update(
                {
                    "candidate_fund_confirm": candidate.get("fund_confirm", ""),
                    "candidate_fake_breakout_risk": candidate.get("fake_breakout_risk", ""),
                    "candidate_direction_confirmations": candidate.get("direction_confirmations", ""),
                    "candidate_change_5m": candidate.get("change_5m", ""),
                    "candidate_change_15m": candidate.get("change_15m", ""),
                    "candidate_change_1h": candidate.get("change_1h", ""),
                    "candidate_volume_spike": candidate.get("volume_spike", ""),
                    "candidate_oi_change": candidate.get("oi_change", ""),
                    "candidate_taker_buy_ratio": candidate.get("taker_buy_ratio", ""),
                    "candidate_taker_sell_ratio": candidate.get("taker_sell_ratio", ""),
                    "candidate_wick_ratio": candidate.get("wick_ratio", ""),
                }
            )
            writer.writerow(flat)


def report_markdown(report: dict[str, Any]) -> str:
    overall = report["metrics"]["overall"]
    holdout = report["metrics"]["holdout"]
    blockers = report["validation"]["blockers"]
    return "\n".join(
        [
            "# Market Backtest",
            "",
            f"- generated_at: {report['generated_at']}",
            f"- passed: {report['validation']['passed']}",
            f"- blockers: {', '.join(blockers) if blockers else 'none'}",
            f"- symbols_loaded: {', '.join(report['source'].get('symbols_loaded') or [])}",
            f"- span_days: {report['data'].get('span_days')}",
            f"- signals: {report['data'].get('signal_count')}",
            f"- trades: {overall['trades']}",
            f"- win_rate: {overall['win_rate']}",
            f"- profit_factor: {overall['profit_factor']}",
            f"- net_pnl_r: {overall['net_pnl_r']}",
            f"- holdout_trades: {holdout['trades']}",
            f"- holdout_win_rate: {holdout['win_rate']}",
            f"- holdout_net_pnl_r: {holdout['net_pnl_r']}",
            "",
            "Policy: closed signal candle, next candle open entry, fees and slippage included, TP1 closes half, Stage 2 uses locked stop and ATR trailing, ambiguous SL/TP candles count as stop first.",
        ]
    )


def parse_candle(row: Any) -> Candle | None:
    if isinstance(row, dict):
        return Candle(
            open_time=int(_f(row.get("open_time"))),
            open=_f(row.get("open")),
            high=_f(row.get("high")),
            low=_f(row.get("low")),
            close=_f(row.get("close")),
            volume=_f(row.get("volume")),
            close_time=int(_f(row.get("close_time"))),
            quote_volume=_f(row.get("quote_volume")),
            trades=int(_f(row.get("trades"))),
            taker_buy_base=_f(row.get("taker_buy_base")),
            taker_buy_quote=_f(row.get("taker_buy_quote")),
        )
    if not isinstance(row, list) or len(row) < 11:
        return None
    return Candle(
        open_time=int(_f(row[0])),
        open=_f(row[1]),
        high=_f(row[2]),
        low=_f(row[3]),
        close=_f(row[4]),
        volume=_f(row[5]),
        close_time=int(_f(row[6])),
        quote_volume=_f(row[7]),
        trades=int(_f(row[8])),
        taker_buy_base=_f(row[9]),
        taker_buy_quote=_f(row[10]),
    )


def parse_symbols(value: str) -> list[str]:
    return [part.strip().upper() for part in value.split(",") if part.strip()]


def exchange_symbol_meta(exchange_info: Any) -> dict[str, dict[str, Any]]:
    rows = exchange_info.get("symbols") if isinstance(exchange_info, dict) else []
    return {
        str(row.get("symbol") or "").upper(): row
        for row in rows
        if isinstance(row, dict) and row.get("symbol")
    }


def symbol_allowed(symbol: str, exchange_meta: dict[str, dict[str, Any]]) -> bool:
    symbol = str(symbol or "").upper()
    if not symbol.endswith("USDT"):
        return False
    if not settings.binance_crypto_perpetual_only:
        return True
    row = exchange_meta.get(symbol)
    if not row:
        return False
    if str(row.get("status") or "").upper() != "TRADING":
        return False
    if str(row.get("contractType") or "").upper() != "PERPETUAL":
        return False
    if str(row.get("quoteAsset") or "").upper() != "USDT":
        return False
    if str(row.get("marginAsset") or "").upper() != "USDT":
        return False
    underlying_type = str(row.get("underlyingType") or "").upper()
    if underlying_type and underlying_type != "COIN":
        return False
    subtypes = row.get("underlyingSubType")
    if isinstance(subtypes, list) and any(str(item).upper() == "TRADFI" for item in subtypes):
        return False
    min_age_days = max(0.0, float(settings.binance_min_symbol_age_days or 0.0))
    onboard_ms = int(_f(row.get("onboardDate")))
    if min_age_days > 0 and onboard_ms > 0:
        age_ms = now_ms() - onboard_ms
        if age_ms < min_age_days * 86_400_000:
            return False
    return True


def candle_quality_errors(candles: list[Candle]) -> list[str]:
    if not candles:
        return []
    closes = [candle.close for candle in candles if candle.close > 0]
    if len(closes) != len(candles):
        return ["kline_nonpositive_close"]
    moves = [abs(_pct(closes[idx], closes[idx - 1])) for idx in range(1, len(closes))]
    if moves and max(moves) > float(settings.binance_kline_max_bar_move_pct or 35.0):
        return ["kline_close_discontinuity"]
    return []


def now_ms() -> int:
    return int(time.time() * 1000)


def iso_ms(value: int) -> str:
    return datetime.fromtimestamp(value / 1000.0, tz=timezone.utc).isoformat()


def _pct(current: float, previous: float) -> float:
    if previous <= 0:
        return 0.0
    return (current - previous) / previous * 100.0


def _f(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


def main() -> int:
    return asyncio.run(main_async())


if __name__ == "__main__":
    raise SystemExit(main())
