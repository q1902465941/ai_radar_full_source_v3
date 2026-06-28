from __future__ import annotations

from dataclasses import dataclass

from backend.config import settings


@dataclass
class TradeCostBreakdown:
    gross_pnl: float
    entry_fee: float
    exit_fee: float
    net_pnl: float
    exit_fill_price: float
    exit_notional: float


@dataclass
class TargetProfitBreakdown:
    ideal_gross_pnl: float
    gross_pnl: float
    entry_fee: float
    exit_fee: float
    cost_drag: float
    net_pnl: float
    tp1_net_pnl: float
    tp2_net_pnl: float
    profit_cost_ratio: float


def market_fill_price(side: str, reference_price: float, action: str) -> float:
    """Conservative paper fill model: market buys pay up, market sells receive down."""
    price = max(0.0, float(reference_price or 0.0))
    slip = max(0.0, float(settings.paper_slippage_pct or 0.0))
    if price <= 0:
        return 0.0
    if action == "entry":
        return price * (1.0 + slip) if side == "LONG" else price * (1.0 - slip)
    if action == "exit":
        return price * (1.0 - slip) if side == "LONG" else price * (1.0 + slip)
    return price


def trade_notional(price: float, quantity: float) -> float:
    return max(0.0, float(price or 0.0)) * max(0.0, float(quantity or 0.0))


def trade_fee(notional: float) -> float:
    return trade_notional(1.0, notional) * max(0.0, float(settings.paper_taker_fee_rate or 0.0))


def round_trip_cost_pct() -> float:
    return 2.0 * max(0.0, float(settings.paper_taker_fee_rate or 0.0)) + 2.0 * max(0.0, float(settings.paper_slippage_pct or 0.0))


def gross_pnl(side: str, entry_price: float, exit_price: float, quantity: float) -> float:
    qty = max(0.0, float(quantity or 0.0))
    entry = float(entry_price or 0.0)
    exit_ = float(exit_price or 0.0)
    if side == "LONG":
        return (exit_ - entry) * qty
    if side == "SHORT":
        return (entry - exit_) * qty
    return 0.0


def close_costs(
    side: str,
    entry_price: float,
    reference_exit_price: float,
    quantity: float,
    entry_fee_alloc: float,
    use_slippage: bool = True,
) -> TradeCostBreakdown:
    exit_fill = market_fill_price(side, reference_exit_price, "exit") if use_slippage else float(reference_exit_price or 0.0)
    exit_notional = trade_notional(exit_fill, quantity)
    exit_fee = trade_fee(exit_notional)
    gross = gross_pnl(side, entry_price, exit_fill, quantity)
    net = gross - max(0.0, float(entry_fee_alloc or 0.0)) - exit_fee
    return TradeCostBreakdown(
        gross_pnl=round(gross, 8),
        entry_fee=round(max(0.0, float(entry_fee_alloc or 0.0)), 8),
        exit_fee=round(exit_fee, 8),
        net_pnl=round(net, 8),
        exit_fill_price=round(exit_fill, 12),
        exit_notional=round(exit_notional, 8),
    )


def target_profit_breakdown(
    side: str,
    entry_price: float,
    quantity: float,
    tp1: float,
    tp2: float,
    tp1_close_ratio: float = 0.5,
) -> TargetProfitBreakdown:
    qty = max(0.0, float(quantity or 0.0))
    entry_ref = max(0.0, float(entry_price or 0.0))
    if qty <= 0 or entry_ref <= 0:
        return TargetProfitBreakdown(0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0)

    entry_fill = market_fill_price(side, entry_ref, "entry")
    entry_notional = trade_notional(entry_fill, qty)
    entry_fee = trade_fee(entry_notional)
    tp1_ratio = min(1.0, max(0.0, float(tp1_close_ratio or 0.0)))
    tp1_qty = qty * tp1_ratio
    tp2_qty = max(0.0, qty - tp1_qty)
    tp1_fee_alloc = entry_fee * tp1_ratio
    tp2_fee_alloc = entry_fee - tp1_fee_alloc

    costs_1 = close_costs(side, entry_fill, tp1, tp1_qty, tp1_fee_alloc)
    costs_2 = close_costs(side, entry_fill, tp2, tp2_qty, tp2_fee_alloc)
    ideal_gross = gross_pnl(side, entry_ref, tp1, tp1_qty) + gross_pnl(side, entry_ref, tp2, tp2_qty)
    gross = costs_1.gross_pnl + costs_2.gross_pnl
    exit_fee = costs_1.exit_fee + costs_2.exit_fee
    net = costs_1.net_pnl + costs_2.net_pnl
    cost_drag = max(0.0, ideal_gross - net)
    ratio = net / cost_drag if cost_drag > 0 else (999.0 if net > 0 else 0.0)
    return TargetProfitBreakdown(
        ideal_gross_pnl=round(ideal_gross, 8),
        gross_pnl=round(gross, 8),
        entry_fee=round(entry_fee, 8),
        exit_fee=round(exit_fee, 8),
        cost_drag=round(cost_drag, 8),
        net_pnl=round(net, 8),
        tp1_net_pnl=round(costs_1.net_pnl, 8),
        tp2_net_pnl=round(costs_2.net_pnl, 8),
        profit_cost_ratio=round(ratio, 4),
    )


def calc_roi(pnl: float, margin: float) -> float:
    margin_value = float(margin or 0.0)
    return (float(pnl or 0.0) / margin_value * 100.0) if margin_value > 0 else 0.0


def stop_distance_pct(entry_price: float, stop_loss: float) -> float:
    entry = float(entry_price or 0.0)
    if entry <= 0:
        return 0.0
    return abs(entry - float(stop_loss or 0.0)) / entry


def reward_r(side: str, entry_price: float, stop_loss: float, target_price: float) -> float:
    risk = abs(float(entry_price or 0.0) - float(stop_loss or 0.0))
    if risk <= 0:
        return 0.0
    if side == "LONG":
        reward = float(target_price or 0.0) - float(entry_price or 0.0)
    elif side == "SHORT":
        reward = float(entry_price or 0.0) - float(target_price or 0.0)
    else:
        reward = 0.0
    return reward / risk
