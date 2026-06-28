from __future__ import annotations

from typing import Any

from backend.account.account_service import account_service
from backend.config import settings
from backend.exchange.binance_futures import binance_futures
from backend.models import Position, now_ms
from backend.positions.position_registry import position_registry


class ExchangeReconciliation:
    """Read-only reconciliation between local live positions and Binance state."""

    def __init__(self) -> None:
        self.last_report: dict[str, Any] = self._missing_report()
        self._refreshing = False

    async def maybe_refresh(self, min_interval_seconds: float = 30.0) -> dict[str, Any]:
        cached = self.cached()
        age = cached.get("age_seconds")
        if cached.get("ts_ms") and isinstance(age, (int, float)) and age < max(1.0, float(min_interval_seconds)):
            return cached
        return await self.refresh()

    async def refresh(self, *, force: bool = False) -> dict[str, Any]:
        if self._refreshing and not force:
            return self.cached()
        self._refreshing = True
        try:
            report = await self._refresh_impl()
            self.last_report = report
            return self.cached()
        finally:
            self._refreshing = False

    def cached(self) -> dict[str, Any]:
        report = dict(self.last_report or self._missing_report())
        ts_ms = _safe_int(report.get("ts_ms"), 0)
        report["age_seconds"] = round(max(0, now_ms() - ts_ms) / 1000.0, 3) if ts_ms > 0 else None
        return report

    async def _refresh_impl(self) -> dict[str, Any]:
        if str(settings.trade_mode).lower() != "live":
            return self._skipped_report("trade_mode_not_live")
        if not binance_futures.configured():
            return self._skipped_report("binance_keys_missing")

        try:
            exchange_positions = await account_service.get_exchange_positions()
            open_orders = await account_service.get_open_orders()
        except Exception as exc:
            return self._error_report("exchange_reconciliation_query_failed", exc)

        local_positions = [
            p for p in position_registry.list_open()
            if self._is_real_live_position(p)
        ]
        local_rows = [self._local_position_row(p) for p in local_positions]
        exchange_rows = [self._exchange_position_row(row) for row in exchange_positions]

        local_by_key = {self._position_key(row): row for row in local_rows}
        exchange_by_key = {self._position_key(row): row for row in exchange_rows}
        issues: list[dict[str, Any]] = []

        for key, row in sorted(exchange_by_key.items()):
            if key not in local_by_key:
                issues.append({
                    "code": "exchange_position_without_local_record",
                    "severity": "critical",
                    "symbol": row.get("symbol"),
                    "side": row.get("side"),
                    "message": "Exchange has an open position that local registry does not manage.",
                })

        for key, row in sorted(local_by_key.items()):
            if key not in exchange_by_key:
                issues.append({
                    "code": "local_live_position_missing_on_exchange",
                    "severity": "critical",
                    "position_id": row.get("position_id"),
                    "symbol": row.get("symbol"),
                    "side": row.get("side"),
                    "message": "Local registry has a real live position that is not visible on exchange.",
                })

        expected_client_ids: set[str] = set()
        for position in local_positions:
            stop_id, tp_id = self._expected_client_ids(position)
            expected_client_ids.update({stop_id, tp_id})
            if not self._order_present(position.symbol, stop_id, position.exchange_stop_order, open_orders):
                issues.append({
                    "code": "live_position_missing_stop_order",
                    "severity": "critical",
                    "position_id": position.position_id,
                    "symbol": position.symbol,
                    "side": position.side,
                    "expected_client_order_id": stop_id,
                    "message": "Real live position has no matching open stop order on exchange.",
                })
            if not self._order_present(position.symbol, tp_id, position.exchange_tp_order, open_orders):
                issues.append({
                    "code": "live_position_missing_tp_order",
                    "severity": "critical",
                    "position_id": position.position_id,
                    "symbol": position.symbol,
                    "side": position.side,
                    "expected_client_order_id": tp_id,
                    "message": "Real live position has no matching open take-profit order on exchange.",
                })

        for order in open_orders or []:
            if not isinstance(order, dict):
                continue
            client_id = str(order.get("clientOrderId") or order.get("origClientOrderId") or "")
            if not self._is_system_protection_client_id(client_id):
                continue
            if client_id in expected_client_ids:
                continue
            issues.append({
                "code": "orphan_strategy_protection_order",
                "severity": "critical",
                "symbol": order.get("symbol"),
                "clientOrderId": client_id,
                "orderId": order.get("orderId"),
                "type": order.get("type"),
                "message": "Exchange has a system protection order that is not tied to a local live position.",
            })

        return {
            "ok": not issues,
            "ts_ms": now_ms(),
            "age_seconds": 0.0,
            "skipped": False,
            "reason": "",
            "mode": settings.trade_mode,
            "testnet": settings.binance_testnet,
            "local_live_positions": local_rows,
            "exchange_positions": exchange_rows,
            "open_order_count": len(open_orders or []),
            "issues": issues,
        }

    def _is_real_live_position(self, position: Position) -> bool:
        if getattr(position, "status", "") != "OPEN":
            return False
        if self._is_test_position(position):
            return False
        position_id = str(getattr(position, "position_id", "") or "")
        has_exchange_order = bool(getattr(position, "exchange_open_order", None))
        return position_id.startswith("livepos") or has_exchange_order

    def _is_test_position(self, position: Position) -> bool:
        open_order = position.exchange_open_order if isinstance(position.exchange_open_order, dict) else {}
        return bool(open_order.get("testOrder") or getattr(position, "lock_status", "") == "LIVE_TEST_ORDER")

    def _local_position_row(self, position: Position) -> dict[str, Any]:
        return {
            "position_id": position.position_id,
            "strategy_id": position.strategy_id,
            "symbol": position.symbol,
            "side": position.side,
            "quantity": position.quantity,
            "entry_price": position.entry_price,
            "stop_loss": position.stop_loss,
            "tp2": position.tp2,
            "lock_status": position.lock_status,
        }

    def _exchange_position_row(self, row: dict[str, Any]) -> dict[str, Any]:
        return {
            "symbol": str(row.get("symbol") or ""),
            "side": str(row.get("side") or ""),
            "positionAmt": _safe_float(row.get("positionAmt")),
            "entryPrice": _safe_float(row.get("entryPrice")),
            "markPrice": _safe_float(row.get("markPrice")),
            "unRealizedProfit": _safe_float(row.get("unRealizedProfit")),
            "positionSide": row.get("positionSide"),
        }

    def _position_key(self, row: dict[str, Any]) -> str:
        return f"{str(row.get('symbol') or '').upper()}:{str(row.get('side') or '').upper()}"

    def _expected_client_ids(self, position: Position) -> tuple[str, str]:
        return f"hy_sl_{position.strategy_id}"[:36], f"hy_tp_{position.strategy_id}"[:36]

    def _is_system_protection_client_id(self, client_id: str) -> bool:
        return client_id.startswith(("hy_sl_", "hy_tp_", "hy_slr_", "hy_tpr_"))

    def _order_present(self, symbol: str, expected_client_id: str, known_order: dict[str, Any], open_orders: list[dict[str, Any]]) -> bool:
        known_client_id = ""
        known_order_id = ""
        if isinstance(known_order, dict):
            known_client_id = str(known_order.get("clientOrderId") or known_order.get("origClientOrderId") or "")
            known_order_id = str(known_order.get("orderId") or "")
        expected_ids = {expected_client_id}
        if known_client_id:
            expected_ids.add(known_client_id)

        for order in open_orders or []:
            if not isinstance(order, dict):
                continue
            if str(order.get("symbol") or "").upper() != str(symbol or "").upper():
                continue
            client_id = str(order.get("clientOrderId") or order.get("origClientOrderId") or "")
            order_id = str(order.get("orderId") or "")
            if client_id in expected_ids or (known_order_id and order_id == known_order_id):
                return True
        return False

    def _missing_report(self) -> dict[str, Any]:
        return {
            "ok": False,
            "ts_ms": 0,
            "age_seconds": None,
            "skipped": False,
            "reason": "not_refreshed",
            "mode": settings.trade_mode,
            "testnet": settings.binance_testnet,
            "local_live_positions": [],
            "exchange_positions": [],
            "open_order_count": 0,
            "issues": [],
        }

    def _skipped_report(self, reason: str) -> dict[str, Any]:
        return {
            "ok": True,
            "ts_ms": now_ms(),
            "age_seconds": 0.0,
            "skipped": True,
            "reason": reason,
            "mode": settings.trade_mode,
            "testnet": settings.binance_testnet,
            "local_live_positions": [],
            "exchange_positions": [],
            "open_order_count": 0,
            "issues": [],
        }

    def _error_report(self, reason: str, exc: Exception) -> dict[str, Any]:
        return {
            "ok": False,
            "ts_ms": now_ms(),
            "age_seconds": 0.0,
            "skipped": False,
            "reason": reason,
            "mode": settings.trade_mode,
            "testnet": settings.binance_testnet,
            "local_live_positions": [],
            "exchange_positions": [],
            "open_order_count": 0,
            "issues": [{
                "code": reason,
                "severity": "critical",
                "message": f"{type(exc).__name__}:{exc}",
            }],
        }


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except Exception:
        return default


exchange_reconciliation = ExchangeReconciliation()
