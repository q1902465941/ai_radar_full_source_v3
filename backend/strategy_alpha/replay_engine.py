from __future__ import annotations

import json
from collections import defaultdict
from typing import Any

from backend.storage.db import db
from backend.strategy_alpha.evaluator import StrategyAlphaEvaluator
from backend.trading.trade_economics import round_trip_cost_pct

FAKE_RISK_RANK = {"LOW": 0, "MEDIUM": 1, "HIGH": 2}


class StrategyAlphaReplayEngine:
    def __init__(self, *, db_obj=db, window_count: int = 3) -> None:
        self.db = db_obj
        self.window_count = max(1, int(window_count))
        self.evaluator = StrategyAlphaEvaluator()

    def simulate(self, strategy: dict[str, Any], market_data: list[dict[str, Any]] | None = None, *, limit: int = 20000) -> dict[str, Any]:
        rows = list(market_data if market_data is not None else self._load_market_data(limit=limit))
        by_symbol: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in rows:
            if not isinstance(row, dict):
                continue
            symbol = str(row.get("symbol") or "")
            if symbol:
                by_symbol[symbol].append(row)
        for symbol_rows in by_symbol.values():
            symbol_rows.sort(key=lambda row: int(row.get("ts_ms") or 0))

        params = dict(strategy.get("params") or {})
        trades: list[dict[str, Any]] = []
        for symbol, symbol_rows in sorted(by_symbol.items()):
            next_entry_time = 0
            for idx, entry in enumerate(symbol_rows[:-1]):
                entry_time = int(entry.get("ts_ms") or 0)
                if entry_time < next_entry_time:
                    continue
                if not self._entry_ok(entry, params):
                    continue
                horizon = max(1, int(params.get("horizon_steps") or 12))
                future = symbol_rows[idx + 1 : idx + 1 + horizon]
                if not future:
                    continue
                trade = self._simulate_trade(strategy, entry, future, params)
                if trade:
                    trades.append(trade)
                    next_entry_time = int(trade.get("close_time") or entry_time)

        trades.sort(key=lambda row: int(row.get("open_time") or 0))
        windows = self._windows(trades)
        evaluation = self.evaluator.evaluate(trades, windows)
        return {
            "strategy_id": strategy.get("strategy_id"),
            "sample_source": "research_alpha",
            "trades": trades,
            "windows": windows,
            "evaluation": evaluation,
        }

    def _load_market_data(self, *, limit: int) -> list[dict[str, Any]]:
        with self.db.conn() as conn:
            rows = conn.execute(
                """
                SELECT payload FROM (
                    SELECT payload, ts_ms FROM radar_snapshots
                    ORDER BY ts_ms DESC
                    LIMIT ?
                ) ORDER BY ts_ms ASC
                """,
                (max(1, int(limit)),),
            ).fetchall()
        out: list[dict[str, Any]] = []
        for row in rows:
            try:
                payload = json.loads(row["payload"])
            except Exception:
                continue
            if isinstance(payload, dict):
                out.append(payload)
        return out

    def _entry_ok(self, row: dict[str, Any], params: dict[str, Any]) -> bool:
        side = row.get("direction")
        if side not in {"LONG", "SHORT"}:
            return False
        if _f(row.get("price")) <= 0:
            return False
        if _f(row.get("score")) < _f(params.get("min_score"), 0.0):
            return False
        if _f(row.get("wick_ratio")) > _f(params.get("max_wick_ratio"), 1.0):
            return False
        if int(_f(row.get("fund_confirm_count"))) < int(_f(params.get("min_fund_confirm"), 0.0)):
            return False
        if int(_direction_confirmations(row)) < int(_f(params.get("min_direction_confirmations"), 0.0)):
            return False
        if _f(row.get("volume_spike")) < _f(params.get("min_volume_spike"), 0.0):
            return False
        if FAKE_RISK_RANK.get(str(row.get("fake_breakout_risk") or "HIGH"), 2) > FAKE_RISK_RANK.get(str(params.get("max_fake_breakout_risk") or "LOW"), 0):
            return False
        min_depth = _f(params.get("min_depth_alignment"), 0.0)
        min_taker = _f(params.get("min_taker_ratio"), 0.0)
        if side == "LONG":
            return _f(row.get("depth_imbalance")) >= min_depth and _f(row.get("taker_buy_ratio")) >= min_taker
        return _f(row.get("depth_imbalance")) <= -min_depth and _f(row.get("taker_sell_ratio")) >= min_taker

    def _simulate_trade(self, strategy: dict[str, Any], entry: dict[str, Any], future: list[dict[str, Any]], params: dict[str, Any]) -> dict[str, Any] | None:
        side = entry.get("direction")
        entry_price = _f(entry.get("price"))
        risk_pct = max(0.0001, _f(params.get("risk_pct"), 0.01))
        tp_r = max(0.1, _f(params.get("tp_r"), 1.0))
        exit_row = future[-1]
        close_reason = "ALPHA_TIMEOUT"
        gross_r = self._mark_to_r(side, entry_price, _f(exit_row.get("price")), risk_pct)
        for row in future:
            move = self._mark_to_r(side, entry_price, _f(row.get("price")), risk_pct)
            if move <= -1.0:
                exit_row = row
                close_reason = "ALPHA_SL"
                gross_r = -1.0
                break
            if move >= tp_r:
                exit_row = row
                close_reason = "ALPHA_TP"
                gross_r = tp_r
                break
        cost_r = _cost_r(risk_pct)
        pnl = gross_r - cost_r
        return {
            "sample_source": "research_alpha",
            "strategy_id": strategy.get("strategy_id"),
            "symbol": entry.get("symbol"),
            "side": side,
            "open_time": int(entry.get("ts_ms") or 0),
            "close_time": int(exit_row.get("ts_ms") or 0),
            "entry_price": entry_price,
            "exit_price": _f(exit_row.get("price")),
            "pnl": round(pnl, 6),
            "gross_r": round(gross_r, 6),
            "cost_r": round(cost_r, 6),
            "risk_pct": round(risk_pct, 6),
            "win": pnl > 0,
            "close_reason": close_reason,
        }

    def _mark_to_r(self, side: str, entry_price: float, current_price: float, risk_pct: float) -> float:
        if entry_price <= 0 or current_price <= 0:
            return -1.0
        if side == "LONG":
            return (current_price / entry_price - 1.0) / risk_pct
        if side == "SHORT":
            return (1.0 - current_price / entry_price) / risk_pct
        return 0.0

    def _windows(self, trades: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if not trades:
            return [{"window": idx, "trades": 0, "pnl": 0.0} for idx in range(self.window_count)]
        start = min(int(row.get("open_time") or 0) for row in trades)
        end = max(int(row.get("open_time") or 0) for row in trades)
        span = max(1, end - start + 1)
        windows = [{"window": idx, "trades": 0, "pnl": 0.0} for idx in range(self.window_count)]
        for trade in trades:
            idx = min(self.window_count - 1, int((int(trade.get("open_time") or 0) - start) / span * self.window_count))
            windows[idx]["trades"] += 1
            windows[idx]["pnl"] = round(float(windows[idx]["pnl"]) + _f(trade.get("pnl")), 6)
        return windows


def _f(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


def _cost_r(risk_pct: float) -> float:
    return round_trip_cost_pct() / max(0.0001, float(risk_pct or 0.0))


def _direction_confirmations(row: dict[str, Any]) -> float:
    if row.get("direction_confirmations") is not None:
        return _f(row.get("direction_confirmations"))
    score_explain = row.get("score_explain")
    if isinstance(score_explain, dict):
        return _f(score_explain.get("direction_confirmations"))
    return 0.0
