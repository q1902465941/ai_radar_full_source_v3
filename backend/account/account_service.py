from __future__ import annotations
import asyncio
from typing import Any
from backend.config import settings
from backend.exchange.binance_futures import binance_futures, BinanceAPIError
from backend.positions.position_registry import position_registry


def _f(x: Any, default: float = 0.0) -> float:
    try:
        return float(x)
    except Exception:
        return default


class AccountService:
    async def get_account_summary(self) -> dict[str, Any]:
        if settings.trade_mode != "live" or not binance_futures.configured():
            return self._paper_summary("not_live_or_keys_missing")
        try:
            account = await self._live_call(binance_futures.account_info())
            balances = await self._live_call(binance_futures.account_balance())
            usdt = next((b for b in balances if b.get("asset") == "USDT"), {})
            return {
                "mode": "live",
                "testnet": settings.binance_testnet,
                "configured": True,
                "canTrade": account.get("canTrade"),
                "walletBalance": _f(usdt.get("balance") or account.get("totalWalletBalance")),
                "availableBalance": _f(usdt.get("availableBalance") or account.get("availableBalance")),
                "unrealizedProfit": _f(usdt.get("crossUnPnl") or account.get("totalUnrealizedProfit")),
                "marginBalance": _f(usdt.get("crossWalletBalance") or account.get("totalMarginBalance")),
                "totalInitialMargin": _f(account.get("totalInitialMargin")),
                "totalMaintMargin": _f(account.get("totalMaintMargin")),
            }
        except Exception as exc:
            return self._live_error_summary("binance_account_error", exc)

    def _paper_summary(self, reason: str) -> dict[str, Any]:
        closed = position_registry.list_closed()
        open_positions = position_registry.list_open()
        realized = sum(float(x.get("pnl", 0.0)) for x in closed)
        floating = sum(float(p.unrealized_pnl) for p in open_positions)
        equity = 1000.0 + realized + floating
        used_margin = sum(float(p.margin) for p in open_positions)
        return {
            "mode": settings.trade_mode,
            "testnet": settings.binance_testnet,
            "configured": binance_futures.configured(),
            "reason": reason,
            "canTrade": settings.live_trading_enabled,
            "walletBalance": equity,
            "availableBalance": max(0.0, equity - used_margin),
            "unrealizedProfit": floating,
            "marginBalance": equity,
            "totalInitialMargin": used_margin,
            "totalMaintMargin": 0.0,
        }

    def _live_error_summary(self, reason: str, exc: Exception) -> dict[str, Any]:
        out = {
            "mode": "live",
            "testnet": settings.binance_testnet,
            "configured": binance_futures.configured(),
            "reason": reason,
            "canTrade": False,
            "walletBalance": 0.0,
            "availableBalance": 0.0,
            "unrealizedProfit": 0.0,
            "marginBalance": 0.0,
            "totalInitialMargin": 0.0,
            "totalMaintMargin": 0.0,
            "error": repr(exc),
        }
        if isinstance(exc, BinanceAPIError) and isinstance(exc.payload, dict):
            out["error_code"] = exc.payload.get("code")
            out["error_message"] = exc.payload.get("msg")
        return out

    async def get_exchange_positions(self) -> list[dict[str, Any]]:
        if settings.trade_mode != "live" or not binance_futures.configured():
            return []
        data = await self._live_call(binance_futures.position_risk())
        positions = []
        for p in data:
            amt = _f(p.get("positionAmt"))
            if abs(amt) <= 0:
                continue
            positions.append({
                "symbol": p.get("symbol"),
                "side": "LONG" if amt > 0 else "SHORT",
                "positionAmt": amt,
                "entryPrice": _f(p.get("entryPrice")),
                "markPrice": _f(p.get("markPrice")),
                "unRealizedProfit": _f(p.get("unRealizedProfit")),
                "liquidationPrice": _f(p.get("liquidationPrice")),
                "leverage": int(_f(p.get("leverage"), 1)),
                "marginType": p.get("marginType"),
                "positionSide": p.get("positionSide"),
                "raw": p,
            })
        return positions

    async def get_open_orders(self, symbol: str | None = None) -> list[dict[str, Any]]:
        if settings.trade_mode != "live" or not binance_futures.configured():
            return []
        return await self._live_call(binance_futures.open_orders(symbol))

    async def _live_call(self, coro):
        timeout = max(3.0, min(15.0, float(settings.binance_http_timeout or 5.0) * 2.0))
        return await asyncio.wait_for(coro, timeout=timeout)


account_service = AccountService()
