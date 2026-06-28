from __future__ import annotations

import json
from typing import Any

from backend.positions.position_registry import position_registry
from backend.storage.db import db


EXCLUDED_CLOSE_REASONS = {"RESTORED_STALE_RECONCILE", "PRICE_SOURCE_STALE_RECONCILE", "ACCEPTANCE_TP2"}


def is_learning_close_reason(reason: Any) -> bool:
    return str(reason or "") not in EXCLUDED_CLOSE_REASONS


class TradeMemory:
    def samples(self, limit: int = 10000, require_radar: bool = True) -> list[dict[str, Any]]:
        closed_rows = position_registry.list_closed(limit=limit)
        out: list[dict[str, Any]] = []
        for closed in closed_rows:
            if not is_learning_close_reason(closed.get("close_reason")):
                continue
            radar = self._radar_for(closed.get("source_signal_id"), closed.get("symbol"))
            if require_radar and not radar:
                continue
            sample = self._sample_from(closed, radar or {})
            out.append(sample)
        return sorted(out, key=lambda x: int(x.get("close_time") or 0))

    def summary(self) -> dict[str, Any]:
        raw_closed = position_registry.list_closed(limit=10000)
        all_closed = [row for row in raw_closed if is_learning_close_reason(row.get("close_reason"))]
        joined = self.samples(limit=10000, require_radar=True)
        wins = sum(1 for x in joined if x["pnl"] > 0)
        losses = sum(1 for x in joined if x["pnl"] < 0)
        return {
            "closed_trades": len(all_closed),
            "raw_closed_trades": len(raw_closed),
            "excluded_closed_trades": len(raw_closed) - len(all_closed),
            "joined_samples": len(joined),
            "weak_samples": len(all_closed) - len(joined),
            "win_rate": round(wins / max(1, wins + losses), 4),
            "pnl": round(sum(x["pnl"] for x in joined), 4),
        }

    def _radar_for(self, scan_id: str | None, symbol: str | None) -> dict[str, Any] | None:
        if not scan_id or not symbol:
            return None
        with db.conn() as conn:
            row = conn.execute(
                "SELECT payload FROM radar_snapshots WHERE scan_id=? AND symbol=? ORDER BY id DESC LIMIT 1",
                (scan_id, symbol),
            ).fetchone()
        if not row:
            return None
        try:
            return json.loads(row["payload"])
        except Exception:
            return None

    def _sample_from(self, closed: dict[str, Any], radar: dict[str, Any]) -> dict[str, Any]:
        pnl = _f(closed.get("pnl"))
        side = str(closed.get("side") or radar.get("direction") or "")
        sample = {
            "sample_id": closed.get("position_id"),
            "symbol": closed.get("symbol") or radar.get("symbol"),
            "side": side,
            "pnl": pnl,
            "win": pnl > 0,
            "close_reason": closed.get("close_reason"),
            "close_time": int(closed.get("close_time") or 0),
            "open_time": int(closed.get("open_time") or 0),
            "strategy_id": closed.get("strategy_id"),
            "source_signal_id": closed.get("source_signal_id"),
            "radar": radar,
            "strategy_contract": closed.get("strategy_contract") or {},
        }
        for key in [
            "entry_price",
            "exit_price",
            "quantity",
            "margin",
            "notional",
            "gross_pnl",
            "fee",
            "roi",
            "risk_usdt",
            "risk_pct",
            "lifecycle_state",
            "mfe",
            "mae",
            "hold_time_ms",
            "cost_model_version",
        ]:
            sample[key] = closed.get(key)
        risk = abs(_f(closed.get("risk_usdt")))
        if risk > 0:
            sample["mfe_r"] = round(_f(closed.get("mfe")) / risk, 4)
            sample["mae_r"] = round(_f(closed.get("mae")) / risk, 4)
        for key in [
            "score",
            "rank",
            "fund_confirm_count",
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
            "atr_pct",
            "wick_ratio",
            "sm_delta",
            "sm_position",
            "heat_slope",
            "slope_score",
            "dealer_radar",
        ]:
            sample[key] = radar.get(key)
        sample["direction"] = side
        return sample


def _f(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


trade_memory = TradeMemory()
