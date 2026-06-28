from __future__ import annotations

from backend.config import settings
from backend.models import ExecutionPlan, Position, new_id, now_ms
from backend.exchange.binance_futures import binance_futures
from backend.positions.position_registry import position_registry
from backend.storage.db import db
from backend.trading.trade_economics import market_fill_price, stop_distance_pct, trade_fee, trade_notional


class LiveExecutor:
    def _open_side(self, side: str) -> str:
        return "BUY" if side == "LONG" else "SELL"

    def _close_side(self, side: str) -> str:
        return "SELL" if side == "LONG" else "BUY"

    def _is_test_position(self, p: Position) -> bool:
        open_order = p.exchange_open_order if isinstance(p.exchange_open_order, dict) else {}
        return bool(open_order.get("testOrder") or p.lock_status == "LIVE_TEST_ORDER")

    def _fill_from_order(self, order: dict, plan: ExecutionPlan) -> tuple[float, float]:
        if order.get("testOrder"):
            return plan.quantity, market_fill_price(plan.side, plan.entry_price, "entry")
        qty = _safe_float(order.get("executedQty"), 0.0)
        if qty <= 0:
            raise RuntimeError("LIVE_ORDER_NOT_FILLED")
        avg_price = _safe_float(order.get("avgPrice"), 0.0)
        cum_quote = _safe_float(order.get("cumQuote"), 0.0)
        if avg_price <= 0 and qty > 0 and cum_quote > 0:
            avg_price = cum_quote / qty
        if avg_price <= 0:
            raise RuntimeError("LIVE_ORDER_FILL_PRICE_MISSING")
        return qty, avg_price

    async def open_position(self, signal_id: str, strategy_id: str, score: float, plan: ExecutionPlan) -> Position:
        if not settings.live_trading_enabled:
            raise RuntimeError("LIVE_TRADING_DISABLED")
        if not binance_futures.configured():
            raise RuntimeError("BINANCE_KEYS_MISSING")
        if not settings.live_use_test_order and not settings.attach_protection_orders:
            raise RuntimeError("PROTECTION_ORDERS_REQUIRED_FOR_REAL_ORDER")

        await binance_futures.exchange_info()
        constraints = binance_futures.market_order_constraints(plan.symbol, plan.quantity, plan.entry_price)
        if not constraints.get("ok"):
            reasons = ",".join(constraints.get("reasons") or ["unknown"])
            raise RuntimeError(f"EXCHANGE_ORDER_CONSTRAINT_FAILED:{reasons}")
        await self._assert_supported_account_mode()
        await binance_futures.change_margin_type(plan.symbol, settings.binance_margin_type.upper())
        await binance_futures.change_leverage(plan.symbol, plan.dynamic_leverage)

        open_side = self._open_side(plan.side)
        close_side = self._close_side(plan.side)
        client_id = f"hy_open_{strategy_id}"[:36]

        if settings.live_use_test_order:
            order = await binance_futures.test_order(
                symbol=plan.symbol,
                side=open_side,
                type="MARKET",
                quantity=binance_futures.format_market_quantity(plan.symbol, plan.quantity),
            )
            order = {"testOrder": True, "response": order}
        else:
            order = await binance_futures.market_open(plan.symbol, open_side, plan.quantity, client_id=client_id)

        stop_order = None
        tp_order = None
        if settings.attach_protection_orders and not settings.live_use_test_order:
            try:
                stop_order = await binance_futures.stop_market(
                    plan.symbol,
                    close_side,
                    plan.stop_loss,
                    client_id=f"hy_sl_{strategy_id}"[:36],
                )
                tp_order = await binance_futures.take_profit_market(
                    plan.symbol,
                    close_side,
                    plan.tp2,
                    client_id=f"hy_tp_{strategy_id}"[:36],
                )
            except Exception as exc:
                # No naked live position: if protection order fails, try reduce-only market close.
                close_order = None
                try:
                    close_order = await binance_futures.market_close(plan.symbol, close_side, plan.quantity, client_id=f"hy_force_{strategy_id}"[:36])
                except Exception as close_exc:
                    self._record_unprotected_live_position(
                        signal_id=signal_id,
                        strategy_id=strategy_id,
                        score=score,
                        plan=plan,
                        order=order,
                        stop_order=stop_order,
                        tp_order=tp_order,
                        protection_error=exc,
                        force_close_error=close_exc,
                    )
                    raise RuntimeError("PROTECTION_ORDER_FAILED_FORCE_CLOSE_FAILED") from close_exc
                finally:
                    if close_order is not None:
                        await self._cancel_strategy_protection_orders(plan.symbol, strategy_id, [stop_order, tp_order])
                raise RuntimeError("PROTECTION_ORDER_FAILED_FORCE_CLOSE_ATTEMPTED") from exc

        filled_qty, entry_fill = self._fill_from_order(order, plan)
        notional = trade_notional(entry_fill, filled_qty)
        margin = notional / plan.dynamic_leverage if plan.dynamic_leverage > 0 else plan.dynamic_margin
        entry_fee = trade_fee(notional)
        risk_usdt = plan.risk_usdt or (notional * stop_distance_pct(entry_fill, plan.stop_loss))

        p = Position(
            position_id=new_id("livepos"),
            strategy_id=strategy_id,
            source_signal_id=signal_id,
            symbol=plan.symbol,
            side=plan.side,
            status="OPEN",
            stage="Stage 1",
            score=score,
            entry_price=entry_fill,
            current_price=entry_fill,
            quantity=filled_qty,
            initial_quantity=filled_qty,
            margin=round(margin, 4),
            leverage=plan.dynamic_leverage,
            stop_loss=plan.stop_loss,
            tp1=plan.tp1,
            tp2=plan.tp2,
            best_price=entry_fill,
            initial_stop_loss=plan.stop_loss,
            initial_risk_unit=round(abs(entry_fill - plan.stop_loss), 8),
            notional=round(notional, 4),
            entry_fee=round(entry_fee, 8),
            risk_usdt=round(risk_usdt, 4),
            risk_pct=plan.risk_pct,
            open_time=now_ms(),
            strategy_contract=plan.strategy_contract,
            exchange_open_order=order or {},
            exchange_stop_order=stop_order or {},
            exchange_tp_order=tp_order or {},
        )
        p.lock_status = "LIVE_TEST_ORDER" if settings.live_use_test_order else "LIVE_PROTECTION_ATTACHED"
        position_registry.add(p)
        return p

    async def close_position(self, p: Position):
        if self._is_test_position(p):
            return {"testOrder": True, "closeSkipped": True, "reason": "live_test_order_has_no_exchange_position"}
        if not settings.live_trading_enabled:
            raise RuntimeError("LIVE_TRADING_DISABLED")
        if not binance_futures.configured():
            raise RuntimeError("BINANCE_KEYS_MISSING")
        await binance_futures.exchange_info()
        await self._assert_supported_account_mode()
        order = await binance_futures.market_close(p.symbol, self._close_side(p.side), p.quantity, client_id=f"hy_close_{p.position_id}"[:36])
        cancel_report = await self._cancel_strategy_protection_orders(
            p.symbol,
            p.strategy_id,
            [p.exchange_stop_order, p.exchange_tp_order],
        )
        if isinstance(order, dict):
            order["protection_cancel"] = cancel_report
        return order

    async def reduce_position(self, p: Position, quantity: float, reason: str = "reduce"):
        if self._is_test_position(p):
            return {"testOrder": True, "reduceSkipped": True, "reason": "live_test_order_has_no_exchange_position"}
        if not settings.live_trading_enabled:
            raise RuntimeError("LIVE_TRADING_DISABLED")
        if not binance_futures.configured():
            raise RuntimeError("BINANCE_KEYS_MISSING")
        await binance_futures.exchange_info()
        await self._assert_supported_account_mode()
        return await binance_futures.market_close(
            p.symbol,
            self._close_side(p.side),
            quantity,
            client_id=f"hy_{reason}_{p.position_id}"[:36],
        )

    async def replace_protection_orders(self, p: Position, reason: str = "replace") -> dict:
        if self._is_test_position(p):
            return {"testOrder": True, "replaceSkipped": True, "reason": "live_test_order_has_no_exchange_position"}
        if not settings.live_trading_enabled:
            raise RuntimeError("LIVE_TRADING_DISABLED")
        if not binance_futures.configured():
            raise RuntimeError("BINANCE_KEYS_MISSING")
        await binance_futures.exchange_info()
        await self._assert_supported_account_mode()
        close_side = self._close_side(p.side)
        old_orders = [p.exchange_stop_order, p.exchange_tp_order]
        stop_order = None
        tp_order = None
        try:
            stop_order = await binance_futures.stop_market(
                p.symbol,
                close_side,
                p.stop_loss,
                client_id=f"hy_slr_{p.position_id}"[:36],
            )
            tp_order = await binance_futures.take_profit_market(
                p.symbol,
                close_side,
                p.tp2,
                client_id=f"hy_tpr_{p.position_id}"[:36],
            )
        except Exception as exc:
            db.set_kv(
                "live_executor.trading_freeze",
                {
                    "active": True,
                    "reason": "PROTECTION_REPLACE_FAILED_OLD_PROTECTION_LEFT_ACTIVE",
                    "symbol": p.symbol,
                    "position_id": p.position_id,
                    "strategy_id": p.strategy_id,
                    "error": f"{type(exc).__name__}:{exc}",
                    "ts_ms": now_ms(),
                },
            )
            raise RuntimeError("PROTECTION_REPLACE_FAILED_OLD_PROTECTION_LEFT_ACTIVE") from exc
        cancel_report = []
        for order in old_orders:
            report = await self._cancel_known_order(p.symbol, order)
            if report:
                cancel_report.append(report)
        failed_cancel = [row for row in cancel_report if isinstance(row, dict) and row.get("ok") is False]
        if failed_cancel:
            db.set_kv(
                "live_executor.trading_freeze",
                {
                    "active": True,
                    "reason": "PROTECTION_REPLACE_OLD_CANCEL_FAILED",
                    "symbol": p.symbol,
                    "position_id": p.position_id,
                    "strategy_id": p.strategy_id,
                    "new_stop_order": stop_order or {},
                    "new_tp_order": tp_order or {},
                    "old_cancel": cancel_report,
                    "ts_ms": now_ms(),
                },
            )
            raise RuntimeError("PROTECTION_REPLACE_OLD_CANCEL_FAILED")
        p.exchange_stop_order = stop_order or {}
        p.exchange_tp_order = tp_order or {}
        return {
            "ok": True,
            "reason": reason,
            "stop_order": p.exchange_stop_order,
            "tp_order": p.exchange_tp_order,
            "old_cancel": cancel_report,
        }

    async def _assert_supported_account_mode(self) -> None:
        if not (
            settings.trade_mode == "live"
            and settings.live_trading_enabled
            and not settings.live_use_test_order
        ):
            return
        if await binance_futures.position_side_dual():
            raise RuntimeError("BINANCE_HEDGE_MODE_UNSUPPORTED")

    def _record_unprotected_live_position(
        self,
        *,
        signal_id: str,
        strategy_id: str,
        score: float,
        plan: ExecutionPlan,
        order: dict,
        stop_order: dict | None,
        tp_order: dict | None,
        protection_error: Exception,
        force_close_error: Exception,
    ) -> Position:
        try:
            filled_qty, entry_fill = self._fill_from_order(order, plan)
        except Exception:
            filled_qty = plan.quantity
            entry_fill = market_fill_price(plan.side, plan.entry_price, "entry")
        notional = trade_notional(entry_fill, filled_qty)
        margin = notional / plan.dynamic_leverage if plan.dynamic_leverage > 0 else plan.dynamic_margin
        entry_fee = trade_fee(notional)
        risk_usdt = plan.risk_usdt or (notional * stop_distance_pct(entry_fill, plan.stop_loss))
        p = Position(
            position_id=new_id("livepos_unprotected"),
            strategy_id=strategy_id,
            source_signal_id=signal_id,
            symbol=plan.symbol,
            side=plan.side,
            status="OPEN",
            stage="Stage 1",
            score=score,
            entry_price=entry_fill,
            current_price=entry_fill,
            quantity=filled_qty,
            initial_quantity=filled_qty,
            margin=round(margin, 4),
            leverage=plan.dynamic_leverage,
            stop_loss=plan.stop_loss,
            tp1=plan.tp1,
            tp2=plan.tp2,
            best_price=entry_fill,
            initial_stop_loss=plan.stop_loss,
            initial_risk_unit=round(abs(entry_fill - plan.stop_loss), 8),
            notional=round(notional, 4),
            entry_fee=round(entry_fee, 8),
            risk_usdt=round(risk_usdt, 4),
            risk_pct=plan.risk_pct,
            open_time=now_ms(),
            strategy_contract=plan.strategy_contract,
            exchange_open_order=order or {},
            exchange_stop_order=stop_order or {},
            exchange_tp_order=tp_order or {},
            exchange_close_order={
                "force_close_failed": True,
                "protection_error": f"{type(protection_error).__name__}:{protection_error}",
                "force_close_error": f"{type(force_close_error).__name__}:{force_close_error}",
            },
        )
        p.lock_status = "UNPROTECTED_LIVE_FORCE_CLOSE_FAILED"
        p.lifecycle_state = "UNPROTECTED_LIVE_INCIDENT"
        position_registry.add(p)
        db.set_kv(
            "live_executor.trading_freeze",
            {
                "active": True,
                "reason": p.lock_status,
                "symbol": p.symbol,
                "position_id": p.position_id,
                "strategy_id": p.strategy_id,
                "protection_error": p.exchange_close_order["protection_error"],
                "force_close_error": p.exchange_close_order["force_close_error"],
                "ts_ms": now_ms(),
            },
        )
        return p

    async def _cancel_strategy_protection_orders(self, symbol: str, strategy_id: str, orders: list[dict | None]) -> list[dict]:
        expected_ids = {
            f"hy_sl_{strategy_id}"[:36],
            f"hy_tp_{strategy_id}"[:36],
        }
        reports: list[dict] = []
        seen: set[str] = set()
        for order in orders:
            report = await self._cancel_known_order(symbol, order)
            if report:
                key = str(report.get("clientOrderId") or report.get("orderId") or "")
                if key:
                    seen.add(key)
                reports.append(report)

        try:
            open_orders = await binance_futures.open_orders(symbol)
        except Exception as exc:
            reports.append({"queried_open_orders": False, "error": f"{type(exc).__name__}:{exc}"})
            return reports

        for row in open_orders or []:
            if not isinstance(row, dict):
                continue
            client_id = str(row.get("clientOrderId") or "")
            if client_id not in expected_ids or client_id in seen:
                continue
            report = await self._cancel_known_order(symbol, row)
            if report:
                reports.append(report)
        return reports

    async def _cancel_known_order(self, symbol: str, order: dict | None) -> dict:
        if not isinstance(order, dict) or order.get("testOrder"):
            return {}
        order_id = _safe_int(order.get("orderId"), 0)
        client_id = str(order.get("clientOrderId") or order.get("origClientOrderId") or "")
        if order_id <= 0 and not client_id:
            return {}
        try:
            if order_id > 0:
                result = await binance_futures.cancel_order(symbol, order_id=order_id)
            else:
                result = await binance_futures.cancel_order(symbol, orig_client_order_id=client_id)
            return {
                "ok": True,
                "orderId": order_id or None,
                "clientOrderId": client_id or None,
                "result": result,
            }
        except Exception as exc:
            return {
                "ok": False,
                "orderId": order_id or None,
                "clientOrderId": client_id or None,
                "error": f"{type(exc).__name__}:{exc}",
            }


live_executor = LiveExecutor()


def _safe_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


def _safe_int(value, default: int = 0) -> int:
    try:
        return int(float(value))
    except Exception:
        return default
