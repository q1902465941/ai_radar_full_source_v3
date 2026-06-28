from __future__ import annotations

import inspect
from typing import Any, Callable

from backend.config import settings
from backend.market.binance_rest import binance_rest
from backend.models import RadarItem
from backend.trading.trade_economics import round_trip_cost_pct


KLINE_SAMPLE_MODEL = "first_touch_geometry_v1"


class StrategyGeometrySampler:
    def __init__(self, fetch_klines: Callable[..., Any] | None = None) -> None:
        self.fetch_klines = fetch_klines or binance_rest.klines

    async def evaluate(self, item: RadarItem) -> dict[str, Any]:
        side = str(item.direction or "")
        if side not in {"LONG", "SHORT"}:
            return self._empty(item, "neutral_side")

        rows = await self._fetch(item.symbol)
        candles = [_parse_candle(row) for row in rows]
        candles = [row for row in candles if row is not None]
        if len(candles) < 20:
            return self._empty(item, "kline_sample_count_low", sample_count=len(candles))

        variants = self._variants(item)
        scored = []
        for variant in variants:
            samples = self._evaluate_variant(candles, side, variant)
            scored.append({**variant, "samples": samples, "score": self._score(samples)})

        scored.sort(
            key=lambda row: (
                row["samples"]["pass_gate"],
                row["samples"]["expected_r"],
                row["samples"]["profit_factor"],
                row["samples"]["win_rate"],
                row["samples"]["sample_count"],
            ),
            reverse=True,
        )
        selected = scored[0] if scored else {}
        samples = selected.get("samples") or {}
        status = "ok" if samples.get("pass_gate") else "weak"
        return {
            "enabled": True,
            "status": status,
            "reason": "geometry selected from recent Binance kline samples",
            "sample_model": KLINE_SAMPLE_MODEL,
            "symbol": item.symbol,
            "side": side,
            "interval": str(settings.binance_kline_interval or "5m"),
            "variant_count": len(scored),
            "pass_count": sum(1 for row in scored if row["samples"]["pass_gate"]),
            "selected_geometry": self._selected_geometry(item, selected),
            "samples": samples,
            "instruction": (
                "Use this as mandatory local evidence for TP/SL geometry. "
                "If status is weak, Codex may still return WAIT or paper-only training, but must not claim live-quality edge."
            ),
        }

    async def _fetch(self, symbol: str) -> list[Any]:
        value = self.fetch_klines(symbol, str(settings.binance_kline_interval or "5m"), max(120, int(settings.binance_kline_limit or 30)))
        if inspect.isawaitable(value):
            value = await value
        return value if isinstance(value, list) else []

    def _variants(self, item: RadarItem) -> list[dict[str, float]]:
        atr_risk = max(0.0, float(item.atr_pct or 0.0)) / 100.0 * 0.8
        base_risks = {
            0.006,
            0.008,
            0.010,
            0.012,
            0.015,
            0.020,
            max(0.006, min(0.025, atr_risk)) if atr_risk > 0 else 0.010,
        }
        risks = sorted(base_risks)
        tp1_rs = (0.9, 1.1, 1.3)
        tp2_rs = (2.0, 2.4, 2.8, 3.2)
        return [
            {"risk_pct": risk_pct, "tp1_r": tp1_r, "tp2_r": tp2_r}
            for risk_pct in risks
            for tp1_r in tp1_rs
            for tp2_r in tp2_rs
            if tp1_r < tp2_r
        ]

    def _evaluate_variant(self, candles: list[dict[str, float]], side: str, variant: dict[str, float]) -> dict[str, Any]:
        horizon = max(3, min(72, int(getattr(settings, "replay_horizon_steps", 36) or 36)))
        risk_pct = float(variant["risk_pct"])
        tp2_r = float(variant["tp2_r"])
        cost_r = round_trip_cost_pct() / max(risk_pct, 0.0001)
        pnl_rs: list[float] = []
        direct_tp2_hits = 0
        stop_hits = 0
        timeouts = 0
        max_adverse_pct = 0.0

        max_start = max(0, len(candles) - 1)
        for start in range(max_start):
            entry = candles[start]["close"]
            if entry <= 0:
                continue
            future = candles[start + 1 : start + 1 + horizon]
            if not future:
                continue
            if side == "LONG":
                stop = entry * (1.0 - risk_pct)
                tp2 = entry * (1.0 + risk_pct * tp2_r)
                result_r, exit_kind, adverse_pct = _long_first_touch(entry, stop, tp2, future, risk_pct, cost_r)
            else:
                stop = entry * (1.0 + risk_pct)
                tp2 = entry * (1.0 - risk_pct * tp2_r)
                result_r, exit_kind, adverse_pct = _short_first_touch(entry, stop, tp2, future, risk_pct, cost_r)
            pnl_rs.append(result_r)
            max_adverse_pct = max(max_adverse_pct, adverse_pct)
            if exit_kind == "TP2":
                direct_tp2_hits += 1
            elif exit_kind == "SL":
                stop_hits += 1
            else:
                timeouts += 1

        wins = [value for value in pnl_rs if value > 0]
        losses = [value for value in pnl_rs if value < 0]
        gross_profit = sum(wins)
        gross_loss = abs(sum(losses))
        sample_count = len(pnl_rs)
        win_rate = len(wins) / max(1, len(wins) + len(losses))
        expected_r = sum(pnl_rs) / max(1, sample_count)
        profit_factor = gross_profit / gross_loss if gross_loss > 0 else (999.0 if gross_profit > 0 else 0.0)
        pass_gate = (
            sample_count >= max(60, int(getattr(settings, "event_calibration_min_samples", 20) or 20))
            and win_rate >= float(settings.strategy_min_paper_win_rate)
            and expected_r >= float(settings.strategy_min_expected_r)
            and profit_factor >= 1.15
            and tp2_r >= float(settings.strategy_min_tp2_r)
        )
        return {
            "sample_count": sample_count,
            "win_rate": round(win_rate, 4),
            "expected_r": round(expected_r, 4),
            "profit_factor": round(profit_factor, 4),
            "cost_r": round(cost_r, 4),
            "tp2_hit_rate": round(direct_tp2_hits / max(1, sample_count), 4),
            "stop_hit_rate": round(stop_hits / max(1, sample_count), 4),
            "timeout_rate": round(timeouts / max(1, sample_count), 4),
            "max_adverse_pct": round(max_adverse_pct, 4),
            "horizon_steps": horizon,
            "pass_gate": pass_gate,
        }

    def _score(self, samples: dict[str, Any]) -> float:
        return round(
            float(samples.get("expected_r") or 0.0) * 100
            + float(samples.get("profit_factor") or 0.0) * 4
            + float(samples.get("win_rate") or 0.0) * 25
            + float(samples.get("sample_count") or 0.0) * 0.01,
            4,
        )

    def _selected_geometry(self, item: RadarItem, selected: dict[str, Any]) -> dict[str, Any]:
        price = max(0.0, float(item.price or 0.0))
        side = str(item.direction or "")
        risk_pct = float(selected.get("risk_pct") or 0.0)
        tp1_r = float(selected.get("tp1_r") or 0.0)
        tp2_r = float(selected.get("tp2_r") or 0.0)
        if price <= 0 or side not in {"LONG", "SHORT"} or risk_pct <= 0:
            return {}
        if side == "LONG":
            stop = price * (1.0 - risk_pct)
            tp1 = price * (1.0 + risk_pct * tp1_r)
            tp2 = price * (1.0 + risk_pct * tp2_r)
        else:
            stop = price * (1.0 + risk_pct)
            tp1 = price * (1.0 - risk_pct * tp1_r)
            tp2 = price * (1.0 - risk_pct * tp2_r)
        return {
            "side": side,
            "entry": round(price, 10),
            "entry_zone_low": round(price * 0.999, 10),
            "entry_zone_high": round(price * 1.001, 10),
            "stop_loss": round(stop, 10),
            "tp1": round(tp1, 10),
            "tp2": round(tp2, 10),
            "risk_pct": round(risk_pct, 6),
            "tp1_r": round(tp1_r, 4),
            "tp2_r": round(tp2_r, 4),
        }

    def _empty(self, item: RadarItem, reason: str, *, sample_count: int = 0) -> dict[str, Any]:
        return {
            "enabled": True,
            "status": "unavailable",
            "reason": reason,
            "sample_model": KLINE_SAMPLE_MODEL,
            "symbol": item.symbol,
            "side": item.direction,
            "variant_count": 0,
            "pass_count": 0,
            "selected_geometry": {},
            "samples": {"sample_count": sample_count, "pass_gate": False},
        }


def _long_first_touch(
    entry: float,
    stop: float,
    tp2: float,
    future: list[dict[str, float]],
    risk_pct: float,
    cost_r: float,
) -> tuple[float, str, float]:
    max_adverse = 0.0
    for candle in future:
        max_adverse = max(max_adverse, max(0.0, (entry - candle["low"]) / entry * 100.0))
        if candle["low"] <= stop:
            return -1.0 - cost_r, "SL", max_adverse
        if candle["high"] >= tp2:
            return ((tp2 - entry) / entry) / risk_pct - cost_r, "TP2", max_adverse
    close = future[-1]["close"]
    return ((close - entry) / entry) / risk_pct - cost_r, "TIMEOUT", max_adverse


def _short_first_touch(
    entry: float,
    stop: float,
    tp2: float,
    future: list[dict[str, float]],
    risk_pct: float,
    cost_r: float,
) -> tuple[float, str, float]:
    max_adverse = 0.0
    for candle in future:
        max_adverse = max(max_adverse, max(0.0, (candle["high"] - entry) / entry * 100.0))
        if candle["high"] >= stop:
            return -1.0 - cost_r, "SL", max_adverse
        if candle["low"] <= tp2:
            return ((entry - tp2) / entry) / risk_pct - cost_r, "TP2", max_adverse
    close = future[-1]["close"]
    return ((entry - close) / entry) / risk_pct - cost_r, "TIMEOUT", max_adverse


def _parse_candle(row: Any) -> dict[str, float] | None:
    if not isinstance(row, list) or len(row) < 5:
        return None
    open_price = _float(row[1])
    high = _float(row[2])
    low = _float(row[3])
    close = _float(row[4])
    if min(open_price, high, low, close) <= 0:
        return None
    return {"open": open_price, "high": high, "low": low, "close": close}


def _float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


strategy_geometry_sampler = StrategyGeometrySampler()
