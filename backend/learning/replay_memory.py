from __future__ import annotations

import json
import time
from collections import defaultdict
from typing import Any

from backend.config import settings
from backend.storage.db import db


class ReplayMemory:
    def __init__(self) -> None:
        self._cache_until = 0.0
        self._cache_limit = 0
        self._sample_cache: list[dict[str, Any]] = []
        self.last_error = ""

    def samples(self, limit: int | None = None) -> list[dict[str, Any]]:
        limit = limit or settings.replay_max_samples
        now = time.time()
        if now < self._cache_until and self._cache_limit >= limit:
            return self._sample_cache[:limit]

        try:
            rows = self._load_snapshots(limit)
        except Exception as exc:
            self.last_error = f"{type(exc).__name__}:{exc}"
            self._sample_cache = []
            self._cache_limit = limit
            self._cache_until = now + max(1, int(settings.event_calibration_ttl_seconds))
            return []
        self.last_error = ""
        by_symbol: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in rows:
            by_symbol[row["symbol"]].append(row)
        for symbol_rows in by_symbol.values():
            symbol_rows.sort(key=lambda x: int(x.get("ts_ms") or 0))

        out: list[dict[str, Any]] = []
        stride = max(1, int(settings.replay_entry_stride))
        horizon = max(1, int(settings.replay_horizon_steps))
        for symbol, symbol_rows in by_symbol.items():
            for idx in range(0, max(0, len(symbol_rows) - 1), stride):
                entry = symbol_rows[idx]
                side = entry.get("direction")
                if side not in {"LONG", "SHORT"}:
                    continue
                if _f(entry.get("score")) < settings.replay_min_score:
                    continue
                future = symbol_rows[idx + 1 : idx + 1 + horizon]
                if not future:
                    continue
                sample = self._simulate(entry, future)
                if sample:
                    out.append(sample)

        out = sorted(out, key=lambda x: int(x.get("close_time") or x.get("ts_ms") or 0), reverse=True)
        self._sample_cache = out[:limit]
        self._cache_limit = limit
        self._cache_until = now + max(1, int(settings.event_calibration_ttl_seconds))
        return self._sample_cache

    def summary(self) -> dict[str, Any]:
        summary_limit = min(int(settings.replay_max_samples), int(settings.event_calibration_sample_limit))
        samples = self.samples(limit=summary_limit)
        wins = sum(1 for x in samples if x["pnl"] > 0)
        losses = sum(1 for x in samples if x["pnl"] < 0)
        return {
            "replay_samples": len(samples),
            "win_rate": round(wins / max(1, wins + losses), 4),
            "pnl_r": round(sum(x["pnl"] for x in samples), 4),
            "tp2": sum(1 for x in samples if x.get("close_reason") == "REPLAY_TP2"),
            "sl": sum(1 for x in samples if x.get("close_reason") == "REPLAY_SL"),
            "timeout": sum(1 for x in samples if x.get("close_reason") == "REPLAY_TIMEOUT"),
        }

    def _load_snapshots(self, sample_limit: int) -> list[dict[str, Any]]:
        snapshot_limit = max(
            1000,
            min(
                200000,
                int(sample_limit) * max(1, int(settings.replay_entry_stride))
                + int(settings.replay_horizon_steps) * max(10, int(settings.binance_symbol_limit)) * 2,
            ),
        )
        with db.conn() as conn:
            rows = conn.execute(
                """
                SELECT payload FROM (
                    SELECT payload, ts_ms FROM radar_snapshots
                    ORDER BY ts_ms DESC
                    LIMIT ?
                ) ORDER BY ts_ms ASC
                """,
                (snapshot_limit,),
            ).fetchall()
        out = []
        for row in rows:
            try:
                payload = json.loads(row["payload"])
            except Exception:
                continue
            out.append(payload)
        return out

    def _simulate(self, entry: dict[str, Any], future: list[dict[str, Any]]) -> dict[str, Any] | None:
        side = entry.get("direction")
        entry_price = _f(entry.get("price"))
        if entry_price <= 0:
            return None
        risk_pct = self._risk_pct(entry)
        if risk_pct <= 0:
            return None
        cost_r = self._cost_r(risk_pct)
        tp_r = max(1.0, float(settings.replay_tp_r))
        close_reason = "REPLAY_TIMEOUT"
        exit_row = future[-1]
        pnl_r = self._mark_to_r(side, entry_price, _f(exit_row.get("price")), risk_pct) - cost_r

        for row in future:
            price = _f(row.get("price"))
            move_r = self._mark_to_r(side, entry_price, price, risk_pct)
            if move_r <= -1.0:
                close_reason = "REPLAY_SL"
                exit_row = row
                pnl_r = -1.0 - cost_r
                break
            if move_r >= tp_r:
                close_reason = "REPLAY_TP2"
                exit_row = row
                pnl_r = tp_r - cost_r
                break

        sample = {**entry}
        sample.update(
            {
                "sample_id": f"replay_{entry.get('symbol')}_{entry.get('ts_ms')}_{side}",
                "symbol": entry.get("symbol"),
                "side": side,
                "direction": side,
                "pnl": round(pnl_r, 4),
                "win": pnl_r > 0,
                "close_reason": close_reason,
                "close_time": int(exit_row.get("ts_ms") or 0),
                "open_time": int(entry.get("ts_ms") or 0),
                "source_signal_id": entry.get("scan_id", ""),
                "strategy_id": "replay_sim",
                "entry_price": entry_price,
                "exit_price": _f(exit_row.get("price")),
                "risk_pct": round(risk_pct * 100.0, 4),
                "cost_r": round(cost_r, 4),
                "radar": entry,
                "sample_source": "replay",
            }
        )
        return sample

    def _risk_pct(self, entry: dict[str, Any]) -> float:
        atr_pct = max(0.0, _f(entry.get("atr_pct"))) / 100.0
        raw = atr_pct * max(0.1, float(settings.replay_atr_risk_mult))
        return min(max(raw, float(settings.replay_min_risk_pct)), float(settings.replay_max_risk_pct))

    def _cost_r(self, risk_pct: float) -> float:
        round_trip_cost_pct = 2.0 * max(0.0, float(settings.paper_taker_fee_rate)) + 2.0 * max(0.0, float(settings.paper_slippage_pct))
        return round_trip_cost_pct / max(risk_pct, 0.0001)

    def _mark_to_r(self, side: str, entry_price: float, current_price: float, risk_pct: float) -> float:
        if current_price <= 0 or entry_price <= 0:
            return -1.0
        if side == "LONG":
            return (current_price / entry_price - 1.0) / risk_pct
        if side == "SHORT":
            return (1.0 - current_price / entry_price) / risk_pct
        return 0.0


def _f(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


replay_memory = ReplayMemory()
