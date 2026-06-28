from __future__ import annotations

import asyncio
import math
import time
from typing import Any

from backend.config import settings
from backend.market.binance_rest import binance_rest
from backend.market.binance_ws_ticker import binance_ticker_stream
from backend.market.dynamic_symbol_stream import dynamic_symbol_stream
from backend.models import MarketSnapshot
from backend.radar.active_coins import active_coin_registry


PRIORITY_SYMBOLS = ("BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT")


class BinanceFactorSource:
    def __init__(self, client: Any | None = None) -> None:
        self.client = client or binance_rest
        self._cache: list[MarketSnapshot] = []
        self._cache_ts = 0.0
        self._previous_open_interest: dict[str, float] = {}
        self.last_refresh_degraded = False
        self.last_refresh_error = ""
        self.last_refresh_source = ""
        self.last_symbol_count = 0
        self.last_snapshot_count = 0
        self.last_failed_symbols: list[str] = []
        self.last_effective_concurrency = 0
        self.last_refresh_timings: dict[str, float] = {}
        self.refresh_in_progress = False
        self.current_refresh_degraded = False
        self.current_refresh_error = ""
        self.current_refresh_source = ""
        self.current_symbol_count = 0
        self.current_snapshot_count = 0
        self.current_effective_concurrency = 0
        self.current_refresh_timings: dict[str, float] = {}
        self._exchange_symbol_meta: dict[str, dict[str, Any]] = {}
        self._exchange_meta_ts = 0.0
        self._ticker_last_prices: dict[str, tuple[float, float]] = {}

    async def get_snapshots(self, force_refresh: bool = False) -> list[MarketSnapshot]:
        now = time.monotonic()
        if not force_refresh and self._cache and now - self._cache_ts < settings.binance_factor_ttl_seconds:
            return list(self._cache)

        started = time.monotonic()
        self._begin_refresh_diagnostics()
        self.last_failed_symbols = []

        try:
            premium_rows = await self._safe_fetch_rows("premium_index", self.client.premium_index)
            try:
                ticker_rows = _as_list(binance_ticker_stream.snapshot_rows())
            except Exception as exc:
                self._mark_degraded(f"ws_ticker:{type(exc).__name__}")
                ticker_rows = []
            if not ticker_rows:
                ticker_rows = await self._safe_fetch_rows("ticker_24hr", self.client.ticker_24hr)
                self.current_refresh_source = "rest_ticker" if ticker_rows else "none"
            else:
                self.current_refresh_source = "ws_ticker"
            self.current_refresh_timings["market_rows_seconds"] = round(time.monotonic() - started, 3)

            premium_rows, ticker_rows = self._complete_market_rows(premium_rows, ticker_rows)
            if not premium_rows or not ticker_rows:
                return self._cached_or_empty("market_rows_unavailable")

            premiums = {str(row.get("symbol", "")): row for row in premium_rows if isinstance(row, dict)}
            tickers = {str(row.get("symbol", "")): row for row in ticker_rows if isinstance(row, dict)}
            await self._refresh_exchange_symbol_meta()
            if settings.binance_crypto_perpetual_only and not self._exchange_symbol_meta:
                return self._cached_or_empty("exchange_info_unavailable")
            active_symbols = self._discover_active_candidates(premiums, tickers)
            symbols = self._select_symbols(premiums, tickers)
            symbols = self._prioritize_active_symbols(symbols, active_symbols)
            self.current_symbol_count = len(symbols)
            if not symbols:
                return self._cached_or_empty("no_symbols_selected")

            effective_concurrency = self._effective_concurrency(len(symbols))
            self.current_effective_concurrency = effective_concurrency
            semaphore = asyncio.Semaphore(effective_concurrency)

            async def load(symbol: str) -> MarketSnapshot | None:
                async with semaphore:
                    try:
                        return await self._snapshot(symbol, premiums.get(symbol, {}), tickers.get(symbol, {}))
                    except Exception as exc:
                        if len(self.last_failed_symbols) < 20:
                            self.last_failed_symbols.append(f"{symbol}:{type(exc).__name__}")
                        return None

            snapshot_started = time.monotonic()
            results = await asyncio.gather(*(load(symbol) for symbol in symbols))
            self.current_refresh_timings["snapshot_build_seconds"] = round(time.monotonic() - snapshot_started, 3)
            self.current_refresh_timings["total_seconds"] = round(time.monotonic() - started, 3)
            snapshots = [row for row in results if row is not None]
            self.current_snapshot_count = len(snapshots)
            if snapshots:
                self._cache = snapshots
                self._cache_ts = now
                self._finish_refresh_diagnostics(len(snapshots))
                return list(snapshots)
            return self._cached_or_empty("snapshot_build_empty")
        except Exception:
            self.refresh_in_progress = False
            raise

    async def _safe_fetch_rows(self, label: str, fetch) -> list[Any]:
        last_exc: Exception | None = None
        for attempt in range(2):
            try:
                return _as_list(await fetch())
            except Exception as exc:
                last_exc = exc
                if attempt == 0:
                    await asyncio.sleep(0.25)
        if last_exc is not None:
            self._mark_degraded(f"{label}:{type(last_exc).__name__}:{_compact_error(last_exc)}")
        return []

    async def _refresh_exchange_symbol_meta(self) -> None:
        if not settings.binance_crypto_perpetual_only:
            return
        now = time.monotonic()
        if self._exchange_symbol_meta and now - self._exchange_meta_ts < 3600:
            return
        try:
            info = await self.client.exchange_info()
        except AttributeError:
            self._mark_degraded("exchange_info_missing")
            return
        except Exception as exc:
            self._mark_degraded(f"exchange_info:{type(exc).__name__}:{_compact_error(exc)}")
            return
        rows = info.get("symbols") if isinstance(info, dict) else []
        meta = {
            str(row.get("symbol", "")): row
            for row in _as_list(rows)
            if isinstance(row, dict) and row.get("symbol")
        }
        if meta:
            self._exchange_symbol_meta = meta
            self._exchange_meta_ts = now
        else:
            self._mark_degraded("exchange_info_empty")

    def _complete_market_rows(self, premium_rows: list[Any], ticker_rows: list[Any]) -> tuple[list[Any], list[Any]]:
        premiums = [row for row in premium_rows if isinstance(row, dict) and row.get("symbol")]
        tickers = [row for row in ticker_rows if isinstance(row, dict) and row.get("symbol")]
        if not premiums and tickers:
            self._mark_degraded("premium_index_missing_using_ticker_prices")
            premiums = [
                {
                    "symbol": row.get("symbol"),
                    "markPrice": row.get("lastPrice") or row.get("markPrice") or row.get("price"),
                    "lastFundingRate": "0",
                }
                for row in tickers
            ]
        if not tickers and premiums:
            self._mark_degraded("ticker_missing_using_premium_prices")
            tickers = [
                {
                    "symbol": row.get("symbol"),
                    "lastPrice": row.get("markPrice") or row.get("lastPrice") or row.get("price"),
                    "quoteVolume": "0",
                    "priceChangePercent": "0",
                }
                for row in premiums
            ]
        return premiums, tickers

    def _cached_or_empty(self, reason: str) -> list[MarketSnapshot]:
        suffix = "using_cache" if self._cache else "no_cache"
        self._mark_degraded(f"{reason}_{suffix}")
        self.current_snapshot_count = len(self._cache)
        self._finish_refresh_diagnostics(len(self._cache))
        return list(self._cache)

    def _mark_degraded(self, reason: str) -> None:
        if self.refresh_in_progress:
            self.current_refresh_degraded = True
            parts = [part for part in self.current_refresh_error.split(";") if part]
        else:
            self.last_refresh_degraded = True
            parts = [part for part in self.last_refresh_error.split(";") if part]
        if reason not in parts:
            parts.append(reason)
        if self.refresh_in_progress:
            self.current_refresh_error = ";".join(parts[:8])
        else:
            self.last_refresh_error = ";".join(parts[:8])

    def _begin_refresh_diagnostics(self) -> None:
        self.refresh_in_progress = True
        self.current_refresh_degraded = False
        self.current_refresh_error = ""
        self.current_refresh_source = ""
        self.current_symbol_count = 0
        self.current_snapshot_count = 0
        self.current_effective_concurrency = 0
        self.current_refresh_timings = {}

    def _finish_refresh_diagnostics(self, snapshot_count: int) -> None:
        self.last_refresh_degraded = self.current_refresh_degraded
        self.last_refresh_error = self.current_refresh_error
        self.last_refresh_source = self.current_refresh_source
        self.last_symbol_count = self.current_symbol_count
        self.last_snapshot_count = int(snapshot_count)
        self.last_effective_concurrency = self.current_effective_concurrency
        self.last_refresh_timings = dict(self.current_refresh_timings or {})
        self.refresh_in_progress = False

    def _discover_active_candidates(
        self,
        premiums: dict[str, dict[str, Any]],
        tickers: dict[str, dict[str, Any]],
    ) -> list[str]:
        now = time.monotonic()
        min_quote = max(0.0, float(settings.radar_active_min_quote_volume or 0.0))
        min_24h = max(0.0, float(settings.radar_active_min_change_24h or 0.0))
        min_short = max(0.0, float(settings.radar_active_min_short_change_pct or 0.0))
        excluded_major_symbols = self._radar_excluded_major_symbols()
        candidates: list[str] = []
        reason_by_symbol: dict[str, str] = {}
        score_by_symbol: dict[str, float] = {}
        for symbol, ticker in tickers.items():
            if symbol in excluded_major_symbols:
                continue
            if not self._symbol_allowed(symbol):
                continue
            price = _first_positive(ticker.get("lastPrice"), ticker.get("markPrice"), ticker.get("price"), (premiums.get(symbol) or {}).get("markPrice"))
            if price <= 0:
                continue
            quote_volume = _float(ticker.get("quoteVolume"))
            change_24h = _float(ticker.get("priceChangePercent"))
            previous = self._ticker_last_prices.get(symbol)
            short_change = _pct(price, previous[0]) if previous and previous[0] > 0 else 0.0
            self._ticker_last_prices[symbol] = (price, now)
            if quote_volume < min_quote:
                continue
            reasons = []
            if abs(change_24h) >= min_24h:
                reasons.append("ticker_24h_move")
            if abs(short_change) >= min_short:
                reasons.append("ticker_short_move")
            if not reasons:
                continue
            candidates.append(symbol)
            reason_by_symbol[symbol] = "+".join(reasons)
            score_by_symbol[symbol] = max(abs(change_24h), abs(short_change))
        active_coin_registry.update_candidates(candidates, now=now, reason_by_symbol=reason_by_symbol, score_by_symbol=score_by_symbol)
        active_coin_registry.expire_idle(now=now)
        active_symbols = active_coin_registry.active_symbols()
        dynamic_symbol_stream.sync(active_symbols, now=now)
        return active_symbols

    def _prioritize_active_symbols(self, symbols: list[str], active_symbols: list[str]) -> list[str]:
        if not active_symbols:
            return symbols
        base_limit = max(1, int(settings.binance_symbol_limit or 1))
        active_limit = max(1, int(settings.radar_active_coin_max_symbols or len(active_symbols)))
        limit = max(base_limit, min(len(active_symbols), active_limit))
        out = []
        for symbol in [*active_symbols, *symbols]:
            if symbol not in out:
                out.append(symbol)
            if len(out) >= limit:
                break
        return out

    def _effective_concurrency(self, symbol_count: int) -> int:
        base = max(1, int(settings.binance_factor_concurrency or 1))
        count = max(0, int(symbol_count or 0))
        if count >= 60:
            scaled = min(24, max(12, math.ceil(count / 5)))
            return max(base, scaled)
        if count >= 24:
            scaled = min(12, max(8, math.ceil(count / 4)))
            return max(base, scaled)
        return base

    def _select_symbols(self, premiums: dict[str, dict[str, Any]], tickers: dict[str, dict[str, Any]]) -> list[str]:
        volume_ranked: list[tuple[str, float]] = []
        mover_ranked: list[tuple[str, float]] = []
        activity_ranked: list[tuple[str, float]] = []
        excluded_major_symbols = self._radar_excluded_major_symbols()
        for symbol, premium in premiums.items():
            if not self._symbol_allowed(symbol):
                continue
            if _float(premium.get("markPrice")) <= 0:
                continue
            ticker = tickers.get(symbol) or {}
            quote_volume = _float(ticker.get("quoteVolume"))
            change_pct = abs(_float(ticker.get("priceChangePercent")))
            volume_ranked.append((symbol, quote_volume))
            mover_ranked.append((symbol, change_pct))
            liquidity_weight = max(1.0, math.log10(max(quote_volume, 10.0)))
            activity_ranked.append((symbol, change_pct * liquidity_weight))
        volume_ranked.sort(key=lambda item: item[1], reverse=True)
        mover_ranked.sort(key=lambda item: item[1], reverse=True)
        activity_ranked.sort(key=lambda item: item[1], reverse=True)
        if excluded_major_symbols:
            filtered_volume_ranked = [item for item in volume_ranked if item[0] not in excluded_major_symbols]
            if filtered_volume_ranked:
                volume_ranked = filtered_volume_ranked
                mover_ranked = [item for item in mover_ranked if item[0] not in excluded_major_symbols]
                activity_ranked = [item for item in activity_ranked if item[0] not in excluded_major_symbols]

        selected: list[str] = []
        valid_symbols = {symbol for symbol, _ in volume_ranked}

        def add_symbol(symbol: str) -> None:
            if symbol in valid_symbols and symbol not in selected and len(selected) < settings.binance_symbol_limit:
                selected.append(symbol)

        for symbol in PRIORITY_SYMBOLS:
            add_symbol(symbol)

        limit = max(1, settings.binance_symbol_limit)
        remaining_slots = max(0, limit - len(selected))
        mover_slots = max(0, min(remaining_slots, round(remaining_slots * max(0.0, min(settings.binance_mover_share, 0.8)))))
        activity_slots = max(0, min(remaining_slots - mover_slots, round(remaining_slots * 0.25)))
        volume_slots = max(0, remaining_slots - mover_slots - activity_slots)
        for symbol, _ in volume_ranked[:volume_slots]:
            add_symbol(symbol)
        for symbol, _ in mover_ranked[:mover_slots]:
            add_symbol(symbol)
        for symbol, _ in activity_ranked[:activity_slots]:
            add_symbol(symbol)
        for snapshot in self._cache:
            add_symbol(snapshot.symbol)
        for symbol, _ in volume_ranked:
            add_symbol(symbol)
            if len(selected) >= limit:
                break
        return selected

    def _radar_excluded_major_symbols(self) -> set[str]:
        if not settings.radar_exclude_major_symbols_from_anomaly:
            return set()
        return {
            symbol.strip().upper()
            for symbol in str(settings.radar_major_symbols or "").split(",")
            if symbol.strip()
        }

    def _symbol_allowed(self, symbol: str) -> bool:
        symbol = str(symbol or "").upper()
        if not symbol.endswith("USDT"):
            return False
        if not settings.binance_crypto_perpetual_only:
            return True
        meta = self._exchange_symbol_meta
        if not meta:
            return True
        row = meta.get(symbol)
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
        onboard_ms = int(_float(row.get("onboardDate")))
        if min_age_days > 0 and onboard_ms > 0:
            age_ms = int(time.time() * 1000) - onboard_ms
            if age_ms < min_age_days * 86_400_000:
                return False
        return True

    async def _snapshot(self, symbol: str, premium: dict[str, Any], ticker: dict[str, Any]) -> MarketSnapshot:
        oi_hist_task = (
            self.client.open_interest_hist(symbol, settings.binance_oi_period, 30)
            if settings.binance_use_open_interest_hist
            else _empty_list()
        )
        taker_task = (
            self.client.taker_long_short_ratio(symbol, settings.binance_oi_period, 5)
            if settings.binance_use_taker_ratio_endpoint
            else _empty_list()
        )
        klines, depth, oi_now, oi_hist, taker_rows = await asyncio.gather(
            self.client.klines(symbol, settings.binance_kline_interval, settings.binance_kline_limit),
            self.client.depth(symbol, settings.binance_depth_limit),
            self.client.open_interest(symbol),
            oi_hist_task,
            taker_task,
            return_exceptions=True,
        )
        klines = [] if isinstance(klines, Exception) else klines
        depth = {} if isinstance(depth, Exception) else depth
        oi_now = {} if isinstance(oi_now, Exception) else oi_now
        oi_hist = [] if isinstance(oi_hist, Exception) else oi_hist
        taker_rows = [] if isinstance(taker_rows, Exception) else taker_rows

        kline_features = _kline_features(_as_list(klines))
        blockers = [str(item) for item in (kline_features.get("quality_blockers") or []) if item]
        fatal_blockers = [item for item in blockers if item != "kline_missing"]
        if fatal_blockers:
            raise ValueError(f"kline_quality:{','.join(str(item) for item in fatal_blockers[:3])}")
        structure_metrics = dict(kline_features.get("structure_metrics") or {})
        if blockers:
            structure_metrics["quality_blockers"] = blockers[:5]
            self._mark_degraded("snapshot_quality:kline_missing")
        taker_buy_ratio, taker_sell_ratio = _taker_ratio(_as_list(taker_rows), kline_features)
        oi_change = self._open_interest_change(symbol, oi_now, _as_list(oi_hist))
        price = _first_positive(
            premium.get("markPrice"),
            ticker.get("lastPrice"),
            kline_features.get("price"),
        )

        return MarketSnapshot(
            symbol=symbol,
            price=price,
            change_5m=kline_features["change_5m"],
            change_15m=kline_features["change_15m"],
            change_1h=kline_features["change_1h"],
            volume_spike=kline_features["volume_spike"],
            oi_change=oi_change,
            funding_rate=_float(premium.get("lastFundingRate")),
            taker_buy_ratio=taker_buy_ratio,
            taker_sell_ratio=taker_sell_ratio,
            depth_imbalance=_depth_imbalance(depth),
            atr_pct=kline_features["atr_pct"],
            wick_ratio=kline_features["wick_ratio"],
            structure_metrics=structure_metrics,
        )

    def _open_interest_change(self, symbol: str, oi_now: Any, oi_hist: list[Any]) -> float:
        hist_values = [_float(row.get("sumOpenInterest")) for row in oi_hist if isinstance(row, dict)]
        hist_values = [value for value in hist_values if value > 0]
        if len(hist_values) >= 2:
            return _pct(hist_values[-1], hist_values[0])

        current = _float(oi_now.get("openInterest")) if isinstance(oi_now, dict) else 0.0
        previous = self._previous_open_interest.get(symbol)
        if current > 0:
            self._previous_open_interest[symbol] = current
        if current > 0 and previous and previous > 0:
            return _pct(current, previous)
        return 0.0


def _kline_features(rows: list[Any]) -> dict[str, Any]:
    candles = [row for row in rows if isinstance(row, list) and len(row) >= 11]
    if not candles:
        return {
            "price": 0.0,
            "change_5m": 0.0,
            "change_15m": 0.0,
            "change_1h": 0.0,
            "volume_spike": 1.0,
            "atr_pct": 0.0,
            "wick_ratio": 0.0,
            "structure_metrics": {},
            "taker_buy_ratio": 0.5,
            "taker_sell_ratio": 0.5,
            "quality_blockers": ["kline_missing"],
        }

    close = _float(candles[-1][4])
    change_5m = _pct(close, _close_n_bars_back(candles, 1))
    change_15m = _pct(close, _close_n_bars_back(candles, 3))
    change_1h = _pct(close, _close_n_bars_back(candles, 12))

    adjusted_quotes = _progress_adjusted_quote_volumes(candles)
    recent_quotes = adjusted_quotes[-3:]
    recent_quote = sum(recent_quotes)
    baseline_rows = candles[-15:-3] or candles[:-3] or candles
    baseline_quote = sum(_float(row[7]) for row in baseline_rows) / max(1, len(baseline_rows))
    recent_avg = recent_quote / max(1, len(recent_quotes))
    volume_spike = recent_avg / baseline_quote if baseline_quote > 0 else 1.0

    ranges = []
    wick_ratios = []
    for row in candles[-14:]:
        open_price = _float(row[1])
        high = _float(row[2])
        low = _float(row[3])
        row_close = _float(row[4])
        if row_close > 0:
            ranges.append((high - low) / row_close * 100)
        candle_range = max(high - low, 0.0)
        if candle_range > 0:
            upper = max(0.0, high - max(open_price, row_close))
            lower = max(0.0, min(open_price, row_close) - low)
            wick_ratios.append(max(upper, lower) / candle_range)
    structure_metrics = _kline_structure_metrics(candles, close, wick_ratios)

    quote_volume = sum(_float(row[7]) for row in candles[-3:])
    taker_buy_quote = sum(_float(row[10]) for row in candles[-3:])
    taker_buy_ratio = taker_buy_quote / quote_volume if quote_volume > 0 else 0.5
    taker_buy_ratio = min(max(taker_buy_ratio, 0.0), 1.0)
    close_moves = [
        abs(_pct(_float(candles[idx][4]), _float(candles[idx - 1][4])))
        for idx in range(1, len(candles))
    ]
    max_bar_move = max(close_moves) if close_moves else 0.0
    quality_blockers: list[str] = []
    if max_bar_move > float(settings.binance_kline_max_bar_move_pct or 35.0):
        quality_blockers.append("kline_close_discontinuity")

    return {
        "price": close,
        "change_5m": change_5m,
        "change_15m": change_15m,
        "change_1h": change_1h,
        "volume_spike": max(0.0, volume_spike),
        "atr_pct": sum(ranges) / max(1, len(ranges)),
        "wick_ratio": max(wick_ratios) if wick_ratios else 0.0,
        "structure_metrics": structure_metrics,
        "taker_buy_ratio": taker_buy_ratio,
        "taker_sell_ratio": 1.0 - taker_buy_ratio,
        "max_bar_move_pct": max_bar_move,
        "quality_blockers": quality_blockers,
    }


def _kline_structure_metrics(candles: list[Any], close: float, recent_wick_ratios: list[float]) -> dict[str, Any]:
    current = candles[-1]
    open_price, high, low, row_close = _candle_prices(current)
    candle_range = max(high - low, 0.0)
    body = abs(row_close - open_price)
    recent = candles[-14:]
    range_rows = candles[-20:]
    highs = [_float(row[2]) for row in range_rows]
    lows = [_float(row[3]) for row in range_rows]
    range_high = max(highs) if highs else 0.0
    range_low = min(lows) if lows else 0.0
    range_width = max(range_high - range_low, 0.0)
    previous_rows = (candles[-21:-1] or candles[:-1])
    previous_highs = [_float(row[2]) for row in previous_rows]
    previous_lows = [_float(row[3]) for row in previous_rows]
    prev_high = max(previous_highs) if previous_highs else 0.0
    prev_low = min(previous_lows) if previous_lows else 0.0

    if recent_wick_ratios:
        max_wick = max(recent_wick_ratios)
        max_wick_index = max(range(len(recent_wick_ratios)), key=lambda idx: recent_wick_ratios[idx])
        bars_since_max_wick = len(recent_wick_ratios) - 1 - max_wick_index
        avg_wick = sum(recent_wick_ratios) / len(recent_wick_ratios)
    else:
        max_wick = 0.0
        bars_since_max_wick = 0
        avg_wick = 0.0

    if highs:
        high_index = max(range(len(highs)), key=lambda idx: highs[idx])
        low_index = min(range(len(lows)), key=lambda idx: lows[idx])
        bars_since_range_high = len(highs) - 1 - high_index
        bars_since_range_low = len(lows) - 1 - low_index
    else:
        bars_since_range_high = 0
        bars_since_range_low = 0

    range_position = 0.5
    if range_width > 0:
        range_position = min(1.0, max(0.0, (close - range_low) / range_width))

    return {
        "current_wick_ratio": _round_metric(_candle_wick_ratio(current)),
        "max_wick_ratio_14": _round_metric(max_wick),
        "avg_wick_ratio_14": _round_metric(avg_wick),
        "bars_since_max_wick": int(bars_since_max_wick),
        "current_body_ratio": _round_metric(body / candle_range if candle_range > 0 else 0.0),
        "current_range_pct": _round_metric((candle_range / row_close * 100.0) if row_close > 0 else 0.0),
        "range_high_20": _round_metric(range_high),
        "range_low_20": _round_metric(range_low),
        "prev_high_20": _round_metric(prev_high),
        "prev_low_20": _round_metric(prev_low),
        "range_position": _round_metric(range_position),
        "range_width_pct": _round_metric((range_width / close * 100.0) if close > 0 else 0.0),
        "distance_to_resistance_pct": _round_metric(((range_high - close) / close * 100.0) if close > 0 else 0.0),
        "distance_to_support_pct": _round_metric(((close - range_low) / close * 100.0) if close > 0 else 0.0),
        "breakout_up": bool(prev_high > 0 and close > prev_high),
        "breakout_down": bool(prev_low > 0 and close < prev_low),
        "bars_since_range_high": int(bars_since_range_high),
        "bars_since_range_low": int(bars_since_range_low),
    }


def _candle_prices(row: list[Any]) -> tuple[float, float, float, float]:
    return _float(row[1]), _float(row[2]), _float(row[3]), _float(row[4])


def _candle_wick_ratio(row: list[Any]) -> float:
    open_price, high, low, close = _candle_prices(row)
    candle_range = max(high - low, 0.0)
    if candle_range <= 0:
        return 0.0
    upper = max(0.0, high - max(open_price, close))
    lower = max(0.0, min(open_price, close) - low)
    return max(upper, lower) / candle_range


def _round_metric(value: Any) -> float:
    parsed = _float(value)
    if not math.isfinite(parsed):
        return 0.0
    return round(parsed, 6)


def _progress_adjusted_quote_volumes(candles: list[Any]) -> list[float]:
    quotes = [_float(row[7]) for row in candles]
    if len(candles) < 2:
        return quotes

    last = candles[-1]
    try:
        open_time = int(float(last[0]))
        close_time = int(float(last[6]))
    except (TypeError, ValueError):
        return quotes

    now_ms = int(time.time() * 1000)
    if open_time <= now_ms < close_time:
        span = max(close_time - open_time, 1)
        elapsed = max(now_ms - open_time, 1)
        progress = max(0.15, min(1.0, elapsed / span))
        quotes[-1] = quotes[-1] / progress
    return quotes


def _close_n_bars_back(candles: list[Any], bars: int) -> float:
    if len(candles) > bars:
        return _float(candles[-bars - 1][4])
    return _float(candles[0][1])


def _taker_ratio(rows: list[Any], kline_features: dict[str, float]) -> tuple[float, float]:
    buy_volume = 0.0
    sell_volume = 0.0
    for row in rows:
        if not isinstance(row, dict):
            continue
        buy_volume += _float(row.get("buyVol"))
        sell_volume += _float(row.get("sellVol"))
    total = buy_volume + sell_volume
    if total > 0:
        buy_ratio = buy_volume / total
    else:
        buy_ratio = kline_features["taker_buy_ratio"]
    buy_ratio = min(max(buy_ratio, 0.0), 1.0)
    return buy_ratio, 1.0 - buy_ratio


def _depth_imbalance(depth: Any) -> float:
    if not isinstance(depth, dict):
        return 0.0
    bid_notional = _book_notional(depth.get("bids") or [])
    ask_notional = _book_notional(depth.get("asks") or [])
    total = bid_notional + ask_notional
    if total <= 0:
        return 0.0
    return (bid_notional - ask_notional) / total


def _book_notional(levels: list[Any]) -> float:
    total = 0.0
    for level in levels:
        if isinstance(level, list) and len(level) >= 2:
            total += _float(level[0]) * _float(level[1])
    return total


def _as_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if value is None:
        return []
    return [value]


async def _empty_list() -> list[Any]:
    return []


def _first_positive(*values: Any) -> float:
    for value in values:
        parsed = _float(value)
        if parsed > 0:
            return parsed
    return 0.0


def _compact_error(exc: Exception) -> str:
    message = str(exc).replace("\n", " ").replace("\r", " ").strip()
    return message[:160] or "no_detail"


def _pct(current: float, previous: float) -> float:
    if previous <= 0:
        return 0.0
    return (current - previous) / previous * 100


def _float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


binance_factor_source = BinanceFactorSource()
