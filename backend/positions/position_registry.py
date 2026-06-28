from __future__ import annotations
from dataclasses import fields
from backend.config import settings
from backend.models import Position, ClosedPosition, now_ms
from backend.storage.db import db
from backend.trading.trade_economics import calc_roi, close_costs, gross_pnl as trade_gross_pnl, market_fill_price, stop_distance_pct, trade_fee, trade_notional

class PositionRegistry:
    def __init__(self):
        self.open: dict[str, Position] = {}
        self.closed: list[ClosedPosition] = []
        self._load_open_positions()

    def add(self, p: Position):
        self.open[p.position_id]=p
        db.save_position(p.asdict())

    def remove(self, position_id: str):
        self.open.pop(position_id, None)
        db.delete_position(position_id)

    def list_open(self) -> list[Position]:
        return list(self.open.values())

    def has_symbol(self, symbol: str) -> bool:
        return any(p.symbol == symbol and p.status == "OPEN" for p in self.open.values())

    def close_archive(self, closed: ClosedPosition):
        self.closed.insert(0, closed)
        db.archive_closed_position(closed.asdict())
        self.open.pop(closed.position_id, None)

    def list_closed(self, limit: int = 10000):
        local=[c.asdict() for c in self.closed]
        persisted=db.list_closed(limit=limit)
        seen={x["position_id"] for x in local}
        rows = local + [x for x in persisted if x["position_id"] not in seen]
        return [self._normalize_closed_costs(x) for x in rows[:limit]]

    def _normalize_closed_costs(self, row: dict) -> dict:
        if row.get("cost_model_version"):
            if float(row.get("notional") or 0.0) > 0:
                return row
            return self._repair_closed_notional(row)
        out = dict(row)
        try:
            side = str(out.get("side") or "")
            qty = float(out.get("quantity") or 0.0)
            entry_ref = float(out.get("entry_price") or 0.0)
            exit_ref = float(out.get("exit_price") or 0.0)
            margin = float(out.get("margin") or 0.0)
            if side not in {"LONG", "SHORT"} or qty <= 0 or entry_ref <= 0 or exit_ref <= 0:
                return out
            entry_fill = market_fill_price(side, entry_ref, "entry")
            entry_notional = trade_notional(entry_fill, qty)
            entry_fee = trade_fee(entry_notional)
            costs = close_costs(side, entry_fill, exit_ref, qty, entry_fee)
            out["legacy_pnl"] = out.get("pnl", 0.0)
            out["raw_entry_price"] = entry_ref
            out["raw_exit_price"] = exit_ref
            out["entry_price"] = round(entry_fill, 12)
            out["exit_price"] = costs.exit_fill_price
            out["notional"] = round(entry_notional, 4)
            out["gross_pnl"] = round(costs.gross_pnl, 4)
            out["fee"] = round(costs.entry_fee + costs.exit_fee, 4)
            out["pnl"] = round(costs.net_pnl, 4)
            out["roi"] = round(calc_roi(costs.net_pnl, margin), 2)
            out["cost_model_version"] = "retro_net_v1"
        except Exception:
            return row
        return out

    def _repair_closed_notional(self, row: dict) -> dict:
        out = dict(row)
        try:
            side = str(out.get("side") or "")
            qty = float(out.get("quantity") or 0.0)
            entry = float(out.get("entry_price") or 0.0)
            exit_ = float(out.get("exit_price") or 0.0)
            margin = float(out.get("margin") or 0.0)
            if side not in {"LONG", "SHORT"} or qty <= 0 or entry <= 0 or exit_ <= 0:
                return out
            entry_notional = trade_notional(entry, qty)
            exit_notional = trade_notional(exit_, qty)
            entry_fee = trade_fee(entry_notional)
            exit_fee = trade_fee(exit_notional)
            gross = trade_gross_pnl(side, entry, exit_, qty)
            net = gross - entry_fee - exit_fee
            out["notional"] = round(entry_notional, 4)
            out["gross_pnl"] = round(gross, 4)
            out["fee"] = round(entry_fee + exit_fee, 4)
            out["pnl"] = round(net, 4)
            out["roi"] = round(calc_roi(net, margin), 2)
            out["cost_model_version"] = "repaired_net_v1"
        except Exception:
            return row
        return out

    def _load_open_positions(self) -> None:
        for row in db.list_positions():
            try:
                p = self._position_from_row(row)
            except Exception:
                continue
            if p.status == "OPEN":
                self.open[p.position_id] = p

    def _position_from_row(self, row: dict) -> Position:
        allowed = {field.name for field in fields(Position)}
        data = {key: value for key, value in dict(row).items() if key in allowed}
        data.setdefault("source_signal_id", "restored")
        data.setdefault("stage", "Stage 1")
        data.setdefault("status", "OPEN")
        data.setdefault("current_price", data.get("entry_price", 0.0))
        data.setdefault("initial_quantity", data.get("quantity", 0.0))
        data.setdefault("best_price", data.get("entry_price", data.get("current_price", 0.0)))
        entry = _f(data.get("entry_price"))
        qty = _f(data.get("initial_quantity") or data.get("quantity"))
        notional = _f(data.get("notional"))
        if notional <= 0 and entry > 0 and qty > 0:
            notional = trade_notional(entry, qty)
            data["notional"] = round(notional, 4)
        if _f(data.get("entry_fee")) <= 0 and notional > 0:
            data["entry_fee"] = round(trade_fee(notional), 8)
        if _f(data.get("risk_usdt")) <= 0 and notional > 0:
            risk_pct = stop_distance_pct(entry, _f(data.get("stop_loss")))
            data["risk_usdt"] = round(notional * risk_pct, 4)
        open_time = _f(data.get("open_time"))
        if (
            data.get("status") == "OPEN"
            and settings.position_max_hold_seconds > 0
            and open_time > 0
            and now_ms() - open_time > settings.position_max_hold_seconds * 1000
        ):
            data["lock_status"] = "RESTORED_STALE"
        return Position(**data)

def _f(value) -> float:
    try:
        return float(value or 0.0)
    except Exception:
        return 0.0

position_registry = PositionRegistry()
