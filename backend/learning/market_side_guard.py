from __future__ import annotations

from typing import Any

from backend.config import settings
from backend.models import now_ms


def side_block_min_trades() -> int:
    return max(4, min(int(settings.evolve_min_backtest_trades or 12), int(settings.evolve_min_holdout_trades or 4)))


def side_block_ttl_ms() -> int:
    hours = _f(getattr(settings, "market_side_block_ttl_hours", 6.0), 6.0)
    return max(60_000, int(hours * 3_600_000))


def market_side_block_reason(metrics: dict[str, Any]) -> str:
    trades = int(_f(metrics.get("trades")))
    if trades < side_block_min_trades():
        return ""
    win_rate = _f(metrics.get("win_rate"))
    profit_factor = _f(metrics.get("profit_factor"))
    net_pnl = _f(metrics.get("net_pnl_r") or metrics.get("pnl_r") or metrics.get("pnl"))
    if net_pnl < 0 and win_rate <= 0.10 and profit_factor <= 0.35:
        return "recent_market_backtest_catastrophic"
    if net_pnl < 0 and profit_factor < 1.0:
        return "recent_market_backtest_negative_expectancy"
    return ""


def market_side_block_active(block: dict[str, Any], current_ms: int | None = None) -> bool:
    if str(block.get("side") or "").upper() not in {"LONG", "SHORT"}:
        return False
    expires_at = int(_f(block.get("expires_at_ms")))
    if expires_at <= 0:
        created_at = int(_f(block.get("created_at_ms")))
        expires_at = created_at + side_block_ttl_ms() if created_at > 0 else 0
    return expires_at > int(current_ms if current_ms is not None else now_ms())


def market_side_report_fresh(market: dict[str, Any], current_ms: int | None = None) -> bool:
    generated_at = int(_f(market.get("generated_at_ms")))
    if generated_at <= 0:
        return False
    return generated_at + side_block_ttl_ms() > int(current_ms if current_ms is not None else now_ms())


def side_blocks_from_market_metrics(
    by_side_metrics: dict[str, Any],
    existing_blocks: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    current_ms = now_ms()
    ttl_ms = side_block_ttl_ms()
    side_blocks: list[dict[str, Any]] = []
    measured_sides: set[str] = set()
    blocked_sides: set[str] = set()
    for side, metrics in (by_side_metrics or {}).items():
        side = str(side or "").upper()
        if side not in {"LONG", "SHORT"} or not isinstance(metrics, dict):
            continue
        if int(_f(metrics.get("trades"))) >= side_block_min_trades():
            measured_sides.add(side)
        reason = market_side_block_reason(metrics)
        if not reason:
            continue
        created_at = current_ms
        side_blocks.append(
            {
                "side": side,
                "reason": reason,
                "created_at_ms": created_at,
                "expires_at_ms": created_at + ttl_ms,
                "trades": int(_f(metrics.get("trades"))),
                "win_rate": _f(metrics.get("win_rate")),
                "profit_factor": _f(metrics.get("profit_factor")),
                "net_pnl_r": _f(metrics.get("net_pnl_r") or metrics.get("pnl_r") or metrics.get("pnl")),
            }
        )
        blocked_sides.add(side)
    for existing in existing_blocks or []:
        side = str(existing.get("side") or "").upper()
        if side not in {"LONG", "SHORT"} or side in measured_sides or side in blocked_sides:
            continue
        if not market_side_block_active(existing, current_ms):
            continue
        side_blocks.append(dict(existing))
        blocked_sides.add(side)
    return side_blocks


def _f(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default
