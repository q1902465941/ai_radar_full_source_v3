from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class _MarketView:
    row: dict[str, Any]

    def __getattr__(self, name: str) -> Any:
        try:
            return self.row[name]
        except KeyError as exc:
            raise AttributeError(name) from exc

    def asdict(self) -> dict[str, Any]:
        return dict(self.row)


class EvolvedStrategyAdapter:
    def __init__(self, strategy: dict[str, Any]):
        self.strategy = dict(strategy)
        self.name = str(
            self.strategy.get("name")
            or self.strategy.get("strategy_id")
            or "evolved_strategy"
        )

    def update_weights(self, weights: dict[str, float]) -> None:
        self.strategy["learning_weight"] = float(weights.get(self.name, weights.get(self.strategy_id, 1.0)))

    @property
    def strategy_id(self) -> str:
        return str(self.strategy.get("strategy_id") or self.name)

    def generate_signal(self, market: Any) -> dict[str, Any] | None:
        row = _market_row(market)
        symbol = str(row.get("symbol") or row.get("s") or "").upper()
        side = str(row.get("side") or row.get("direction") or "").upper()
        if side == "BUY":
            side = "LONG"
        elif side == "SELL":
            side = "SHORT"
        if not symbol or side not in {"LONG", "SHORT"}:
            return None
        if not _strategy_matches(self.strategy, _MarketView({**row, "symbol": symbol, "direction": side, "side": side})):
            return None
        volatility = _float(row.get("volatility"), 0.0)
        if volatility <= 0:
            volatility = max(1e-6, _float(row.get("atr_pct"), 1.0) / 100.0)
        return {
            "name": self.name,
            "strategy": self.strategy_id,
            "symbol": symbol,
            "side": side,
            "price": _float(row.get("price") or row.get("entry_price") or row.get("c"), 0.0),
            "score": _float(row.get("score"), _float((self.strategy.get("metrics") or {}).get("score"), 0.0)),
            "volatility": volatility,
            "trades": list(self.strategy.get("trades") or []),
            "strategy_payload": dict(self.strategy),
        }


def _strategy_matches(strategy: dict[str, Any], market: _MarketView) -> bool:
    try:
        from backend.learning.strategy_filter import strategy_matches
    except Exception:
        return True
    try:
        return bool(strategy_matches(strategy, market))
    except Exception:
        return True


def _market_row(market: Any) -> dict[str, Any]:
    if isinstance(market, dict):
        return dict(market)
    if hasattr(market, "asdict"):
        try:
            return dict(market.asdict())
        except Exception:
            pass
    if hasattr(market, "__dict__"):
        return dict(vars(market))
    return {}


def _float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default
