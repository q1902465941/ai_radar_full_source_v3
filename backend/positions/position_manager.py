from __future__ import annotations

import asyncio
from pathlib import Path
import shutil

from backend.config import settings
from backend.ai_strategy.position_policy_client import ai_position_policy_client
from backend.learning.ai_strategy_feedback import ai_strategy_feedback
from backend.market.binance_rest import binance_rest
from backend.market.market_service import PriceQuote, market_service
from backend.models import ClosedPosition, Position, PositionDecision, PositionPolicyReview, now_ms
from backend.positions.position_registry import position_registry
from backend.storage.db import db
from backend.trading.trade_economics import calc_roi, close_costs, trade_notional


class PositionManager:
    def __init__(self):
        self._manage_lock = asyncio.Lock()
        self._ai_review_inflight: set[str] = set()

    def update_position(self, p: Position, current_price: float):
        self._ensure_lifecycle_contract(p)
        p.current_price = current_price
        entry_fee_alloc = self._entry_fee_alloc(p, p.quantity)
        costs = close_costs(p.side, p.entry_price, current_price, p.quantity, entry_fee_alloc)
        p.unrealized_pnl = round(costs.net_pnl, 4)
        p.mfe = round(max(float(p.mfe or 0.0), p.unrealized_pnl), 4)
        p.mae = round(min(float(p.mae or 0.0), p.unrealized_pnl), 4)
        p.roi = round(calc_roi(p.unrealized_pnl + p.realized_pnl, p.margin), 2)
        if p.side == "LONG":
            p.best_price = max(p.best_price, current_price)
        if p.side == "SHORT":
            p.best_price = min(p.best_price, current_price)

    def close_position(self, p: Position, reason: str, exit_price: float | None = None, *, exit_price_is_fill: bool = False):
        exit_price = exit_price or p.current_price
        entry_fee_alloc = self._entry_fee_alloc(p, p.quantity)
        costs = close_costs(p.side, p.entry_price, exit_price, p.quantity, entry_fee_alloc, use_slippage=not exit_price_is_fill)
        pnl = p.realized_pnl + costs.net_pnl
        fee = p.realized_fee + costs.entry_fee + costs.exit_fee
        gross = p.realized_gross_pnl + costs.gross_pnl
        closed_notional = p.notional or trade_notional(p.entry_price, p.initial_quantity)
        closed = ClosedPosition(
            position_id=p.position_id,
            strategy_id=p.strategy_id,
            symbol=p.symbol,
            side=p.side,
            entry_price=p.entry_price,
            exit_price=costs.exit_fill_price,
            quantity=p.initial_quantity,
            margin=p.margin,
            pnl=round(pnl, 4),
            roi=round(calc_roi(pnl, p.margin), 2),
            close_reason=reason,
            score_at_entry=p.score,
            open_time=p.open_time,
            close_time=now_ms(),
            source_signal_id=p.source_signal_id,
            notional=round(closed_notional, 4),
            gross_pnl=round(gross, 4),
            fee=round(fee, 4),
            risk_usdt=p.risk_usdt,
            risk_pct=p.risk_pct,
            strategy_contract=p.strategy_contract,
            lifecycle_state="CLOSED",
            mfe=p.mfe,
            mae=p.mae,
            mfe_r=p.mfe_r,
            mae_r=p.mae_r,
            hold_time_ms=max(0, now_ms() - int(p.open_time or now_ms())),
            exit_decision=p.last_decision,
            decision_log=list(p.decision_log or [])[-30:],
            last_ai_review=p.last_ai_review,
            ai_review_log=list(p.ai_review_log or [])[-20:],
            exchange_open_order=p.exchange_open_order,
            exchange_stop_order=p.exchange_stop_order,
            exchange_tp_order=p.exchange_tp_order,
            exchange_close_order=p.exchange_close_order,
        )
        position_registry.close_archive(closed)
        try:
            ai_strategy_feedback.record_close(closed)
        except Exception:
            pass
        return closed

    async def managed_close(self, p: Position, reason: str, exit_price: float | None = None):
        exit_price_is_fill = False
        if self._is_live_position(p):
            from backend.trading.live_executor import live_executor

            try:
                p.exchange_close_order = await live_executor.close_position(p) or {}
            except Exception as exc:
                self._record_live_management_failure(p, "CLOSE", reason, exc)
                raise
            _, exchange_exit = self._fill_from_exchange_order(
                p.exchange_close_order,
                fallback_qty=p.quantity,
                fallback_price=exit_price or p.current_price,
            )
            if exchange_exit > 0:
                exit_price = exchange_exit
                exit_price_is_fill = True
        return self.close_position(p, reason, exit_price, exit_price_is_fill=exit_price_is_fill)

    async def partial_tp1(self, p: Position):
        await self.partial_reduce(p, 0.5, p.tp1, "TP1_PARTIAL")
        if p.side == "LONG":
            p.stop_loss = max(self._net_breakeven_price(p), p.entry_price + 0.2 * (p.tp1 - p.entry_price))
        else:
            p.stop_loss = min(self._net_breakeven_price(p), p.entry_price - 0.2 * (p.entry_price - p.tp1))
        p.stop_loss = round(p.stop_loss, 8)
        p.stage = "Stage 2"
        p.lifecycle_state = "SCALE_OUT"
        p.defense_level = "NORMAL"
        p.lock_status = "TP1_PARTIAL_NET_LOCK"
        await self._sync_live_protection(p, "TP1_PARTIAL")

    async def partial_reduce(self, p: Position, ratio: float, exit_price: float, reason: str):
        close_qty = p.quantity * max(0.0, min(1.0, ratio))
        if close_qty <= 0:
            return
        exit_price_is_fill = False
        if self._is_live_position(p):
            from backend.trading.live_executor import live_executor

            try:
                order = await live_executor.reduce_position(p, close_qty, reason.lower())
            except Exception as exc:
                self._record_live_management_failure(p, "REDUCE", reason, exc)
                raise
            actual_qty, actual_exit = self._fill_from_exchange_order(order or {}, fallback_qty=close_qty, fallback_price=exit_price)
            if actual_qty > 0:
                close_qty = min(p.quantity, actual_qty)
            if actual_exit > 0:
                exit_price = actual_exit
                exit_price_is_fill = True

        entry_fee_alloc = self._entry_fee_alloc(p, close_qty)
        costs = close_costs(p.side, p.entry_price, exit_price, close_qty, entry_fee_alloc, use_slippage=not exit_price_is_fill)
        p.realized_pnl += costs.net_pnl
        p.realized_fee += costs.entry_fee + costs.exit_fee
        p.realized_gross_pnl += costs.gross_pnl
        p.quantity -= close_qty
        p.quantity = round(max(0.0, p.quantity), 8)
        p.lifecycle_state = "SCALE_OUT" if reason == "TP1_PARTIAL" else "DEFENSIVE"
        p.lock_status = "TP1_PARTIAL_NET_LOCK" if reason == "TP1_PARTIAL" else "DEFENSIVE_PARTIAL_REDUCE"

    def _record_live_management_failure(self, p: Position, action: str, reason: str, exc: Exception) -> None:
        lock_status = f"LIVE_{action}_FAILED_MANUAL_REVIEW"
        error = f"{type(exc).__name__}:{exc}"
        p.lifecycle_state = "LIVE_MANAGEMENT_FAILED"
        p.lock_status = lock_status
        p.exchange_close_order = {
            **(p.exchange_close_order if isinstance(p.exchange_close_order, dict) else {}),
            "live_management_failed": True,
            "action": action,
            "reason": reason,
            "error": error,
            "ts_ms": now_ms(),
        }
        self._record_decision(p, "NOOP", lock_status, ["live_management_failed", error])
        position_registry.add(p)
        db.set_kv(
            "live_executor.trading_freeze",
            {
                "active": True,
                "reason": lock_status,
                "symbol": p.symbol,
                "position_id": p.position_id,
                "strategy_id": p.strategy_id,
                "action": action,
                "close_reason": reason,
                "error": error,
                "ts_ms": now_ms(),
            },
        )

    def trailing_stop(self, p: Position, signal=None):
        atr_pct = max(0.0, float(getattr(signal, "atr_pct", 0.0) or 0.0))
        atr_distance = p.current_price * atr_pct / 100.0 * 0.9
        min_distance = p.entry_price * 0.004
        max_distance = p.entry_price * 0.025
        dist = min(max(atr_distance, min_distance), max_distance)
        breakeven = self._net_breakeven_price(p)
        changed = False
        if p.side == "LONG":
            new_sl = max(p.best_price - dist, breakeven)
            if new_sl > p.stop_loss:
                p.stop_loss = round(new_sl, 8)
                p.lifecycle_state = "TREND_HOLD"
                p.lock_status = "ATR_NET_TRAIL"
                changed = True
        else:
            new_sl = min(p.best_price + dist, breakeven)
            if new_sl < p.stop_loss:
                p.stop_loss = round(new_sl, 8)
                p.lifecycle_state = "TREND_HOLD"
                p.lock_status = "ATR_NET_TRAIL"
                changed = True
        return changed

    async def _sync_live_protection(self, p: Position, reason: str) -> None:
        if not self._is_live_position(p):
            return
        from backend.trading.live_executor import live_executor

        report = await live_executor.replace_protection_orders(p, reason)
        p.last_decision = {**(p.last_decision or {}), "protection_sync": report}

    def _fill_from_exchange_order(self, order: dict, *, fallback_qty: float, fallback_price: float) -> tuple[float, float]:
        if not isinstance(order, dict) or order.get("testOrder"):
            return fallback_qty, fallback_price
        qty = _safe_float(order.get("executedQty"), 0.0)
        avg_price = _safe_float(order.get("avgPrice"), 0.0)
        cum_quote = _safe_float(order.get("cumQuote"), 0.0)
        if avg_price <= 0 and qty > 0 and cum_quote > 0:
            avg_price = cum_quote / qty
        return (qty if qty > 0 else fallback_qty), (avg_price if avg_price > 0 else fallback_price)

    async def manage_all(self):
        if self._manage_lock.locked():
            return
        async with self._manage_lock:
            await self._manage_all_unlocked()

    async def _manage_all_unlocked(self):
        from backend.radar.radar_engine import radar_engine

        for p in list(position_registry.list_open()):
            signal = radar_engine.get_symbol(p.symbol)
            quote = await self._price_quote_for_position(p)
            self._apply_price_quote(p, quote)
            unsafe_quote = self._quote_not_safe_for_position(quote, p)
            if unsafe_quote:
                p.price_stale = True
            if quote.price <= 0 or quote.stale or unsafe_quote:
                self._record_decision(
                    p,
                    "NOOP",
                    "PRICE_SOURCE_STALE",
                    self._stale_price_evidence(quote),
                )
                position_registry.add(p)
                continue
            price = quote.price
            self.update_position(p, price)

            if p.lock_status == "RESTORED_STALE":
                self._record_decision(p, "EXIT", "RESTORED_STALE_RECONCILE", ["restored stale position"])
                await self.managed_close(p, "RESTORED_STALE_RECONCILE", price)
                continue

            hard_stop_reason = self._hard_stop_reason(p, price)
            if hard_stop_reason:
                self._record_decision(p, "EXIT", hard_stop_reason, ["hard stop fired"])
                await self.managed_close(p, hard_stop_reason, price)
                continue

            decision = self.position_decision(p, signal, price)
            decision = await self._ai_review_position(p, signal, decision)
            if decision.action == "EXIT":
                await self.managed_close(p, decision.reason, price)
                continue
            if decision.action == "REDUCE":
                await self.partial_reduce(p, decision.reduce_ratio or 0.25, price, decision.reason)
                position_registry.add(p)
                continue

            if self._max_hold_expired(p):
                self._record_decision(p, "EXIT", "MAX_HOLD_TIMEOUT", ["time stop with no favorable development"])
                await self.managed_close(p, "MAX_HOLD_TIMEOUT", price)
                continue

            if p.stage == "Stage 1":
                if p.lifecycle_state not in {"DEFENSIVE", "EXIT_READY"}:
                    p.lifecycle_state = "PROTECTING"
                if p.side == "LONG":
                    if price <= p.stop_loss:
                        self._record_decision(p, "EXIT", "SL", ["stage1 hard stop"])
                        await self.managed_close(p, "SL", price)
                        continue
                    if self._stage1_soft_lock(p, price):
                        await self._sync_live_protection(p, "NET_BREAKEVEN_LOCK")
                    if price >= p.tp1:
                        self._record_decision(p, "REDUCE", "TP1_PARTIAL", ["tp1 reached after costs"], reduce_ratio=0.5)
                        await self.partial_tp1(p)
                elif p.side == "SHORT":
                    if price >= p.stop_loss:
                        self._record_decision(p, "EXIT", "SL", ["stage1 hard stop"])
                        await self.managed_close(p, "SL", price)
                        continue
                    if self._stage1_soft_lock(p, price):
                        await self._sync_live_protection(p, "NET_BREAKEVEN_LOCK")
                    if price <= p.tp1:
                        self._record_decision(p, "REDUCE", "TP1_PARTIAL", ["tp1 reached after costs"], reduce_ratio=0.5)
                        await self.partial_tp1(p)

            if p.stage == "Stage 2":
                if p.lifecycle_state not in {"DEFENSIVE", "EXIT_READY", "SCALE_OUT"}:
                    p.lifecycle_state = "TREND_HOLD"
                if self.trailing_stop(p, signal):
                    await self._sync_live_protection(p, "TRAILING_STOP")
                if p.side == "LONG":
                    if price >= p.tp2:
                        self._record_decision(p, "EXIT", "TP2", ["tp2 target reached"])
                        await self.managed_close(p, "TP2", price)
                        continue
                    if price <= p.stop_loss:
                        self._record_decision(p, "EXIT", "LOCKED_STOP", ["locked stop fired"])
                        await self.managed_close(p, "LOCKED_STOP", price)
                        continue
                elif p.side == "SHORT":
                    if price <= p.tp2:
                        self._record_decision(p, "EXIT", "TP2", ["tp2 target reached"])
                        await self.managed_close(p, "TP2", price)
                        continue
                    if price >= p.stop_loss:
                        self._record_decision(p, "EXIT", "LOCKED_STOP", ["locked stop fired"])
                        await self.managed_close(p, "LOCKED_STOP", price)
                        continue
            position_registry.add(p)

    async def _price_quote_for_position(self, p: Position) -> PriceQuote:
        try:
            return await market_service.price_quote(p.symbol, p.side)
        except Exception as exc:
            cached = market_service.cached_price_quote(p.symbol)
            cached.error = ";".join(part for part in [cached.error, f"price_quote:{type(exc).__name__}"] if part)
            cached.stale = True
            return cached

    def _apply_price_quote(self, p: Position, quote: PriceQuote) -> None:
        p.price_source = quote.source
        p.price_age_seconds = quote.age_seconds
        p.price_stale = bool(quote.stale)
        p.price_error = quote.error
        p.price_bid = quote.bid
        p.price_ask = quote.ask
        p.last_price_update_ms = quote.ts_ms

    def _stale_price_evidence(self, quote: PriceQuote) -> list[str]:
        evidence = [
            f"price_source={quote.source}",
            f"price_age_seconds={quote.age_seconds:.3f}",
        ]
        if quote.price > 0:
            evidence.append(f"cached_price={quote.price:.8g}")
        if quote.error:
            evidence.append(f"price_error={quote.error}")
        if self._quote_not_safe_for_position(quote):
            evidence.append("price_source_not_safe_for_position_valuation")
        evidence.append("skip_position_decision_until_fresh_price")
        return evidence

    def _quote_not_safe_for_position(self, quote: PriceQuote, p: Position | None = None) -> bool:
        source = str(quote.source or "")
        safe_prefixes = (
            "book_ticker_",
            "ticker_price",
            "premium_mark_price",
            "ticker_24hr_last_price",
        )
        if p is not None and self._is_live_position(p) and not settings.binance_testnet:
            if str(binance_rest.last_public_source or "") == "testnet_fallback":
                return True
        return not source.startswith(safe_prefixes)

    def position_decision(self, p: Position, signal, price: float) -> PositionDecision:
        self._refresh_realtime_state(p, signal, price)
        thesis_alive, thesis_reason, evidence = self._thesis_state(p, signal)
        p.thesis_alive = thesis_alive
        p.defense_level = self._defense_level(p, signal, thesis_alive, evidence)
        if not thesis_alive:
            p.lifecycle_state = "EXIT_READY"
            return self._record_decision(p, "EXIT", thesis_reason, evidence)
        if self._defensive_reduce_ready(p, signal, evidence):
            p.lifecycle_state = "DEFENSIVE"
            p.lock_status = "DEFENSIVE_PARTIAL_REDUCE"
            return self._record_decision(p, "REDUCE", "DEFENSIVE_PARTIAL_REDUCE", evidence, reduce_ratio=0.25)
        if p.defense_level in {"DEFENSIVE", "WATCH"}:
            if p.defense_level == "DEFENSIVE":
                p.lifecycle_state = "DEFENSIVE"
                if not str(p.lock_status or "").startswith("TP1_"):
                    if "strong_reverse_signal" in evidence:
                        p.lock_status = "DEFENSIVE_REVERSE_SIGNAL"
                    elif "fake_breakout_high" in evidence or "fake_breakout_medium" in evidence:
                        p.lock_status = "DEFENSIVE_FAKE_BREAKOUT"
                    elif "signal_score_decayed" in evidence:
                        p.lock_status = "DEFENSIVE_SIGNAL_DECAY"
                    else:
                        p.lock_status = "DEFENSIVE_HOLD"
            return self._record_decision(p, "PROTECT", f"{p.defense_level}_THESIS_ALIVE", evidence)
        return self._record_decision(p, "HOLD", "THESIS_ALIVE", evidence)

    async def _ai_review_position(self, p: Position, signal, rule_decision: PositionDecision) -> PositionDecision:
        if not self._ai_review_required(p, rule_decision):
            return rule_decision
        review_key = p.position_id or p.symbol
        if review_key in self._ai_review_inflight:
            return rule_decision
        self._ai_review_inflight.add(review_key)
        try:
            review = await ai_position_policy_client.review(p, signal, rule_decision)
            adjusted, safety_override = self._apply_ai_review(p, rule_decision, review)
            self._store_ai_review(p, review, rule_decision, adjusted, safety_override)
            return adjusted
        finally:
            self._ai_review_inflight.discard(review_key)

    def _ai_review_required(self, p: Position, rule_decision: PositionDecision) -> bool:
        if not settings.ai_position_review_enabled or not settings.ai_enabled:
            return False
        provider = str(settings.ai_position_review_provider or "codex_cli").strip().lower()
        if provider not in {"codex_cli", "deepseek"}:
            return False
        if provider == "codex_cli" and not shutil_which_codex():
            return False
        trigger = (
            rule_decision.action in {"REDUCE", "EXIT"}
            or rule_decision.defense_level in {"WATCH", "DEFENSIVE", "EXIT_READY"}
            or rule_decision.adverse_r >= float(settings.ai_position_review_trigger_adverse_r)
        )
        if not trigger:
            return False
        last = p.last_ai_review if isinstance(p.last_ai_review, dict) else {}
        last_ts = int(last.get("ts_ms") or 0)
        min_interval_ms = max(5, int(settings.ai_position_review_min_interval_seconds or 60)) * 1000
        if rule_decision.action not in {"REDUCE", "EXIT"} and last_ts > 0 and now_ms() - last_ts < min_interval_ms:
            return False
        return True

    def _apply_ai_review(
        self,
        p: Position,
        rule_decision: PositionDecision,
        review: PositionPolicyReview,
    ) -> tuple[PositionDecision, str]:
        if review.status != "ok":
            return rule_decision, "ai_unavailable_fallback_to_rule"
        evidence = list(rule_decision.evidence or [])
        evidence.append(f"ai_action={review.action}")
        if review.reason:
            evidence.append(f"ai_reason={review.reason[:120]}")

        if rule_decision.action in {"EXIT", "REDUCE"}:
            return rule_decision, "rule_safety_priority"

        if review.action == "EXIT":
            if self._ai_exit_allowed(p, review):
                p.thesis_alive = False
                p.defense_level = "EXIT_READY"
                p.lifecycle_state = "EXIT_READY"
                return self._record_decision(p, "EXIT", "AI_EXIT_THESIS_INVALIDATED", evidence), ""
            return rule_decision, "ai_exit_blocked_inside_safety_kernel"

        if review.action == "REDUCE":
            if self._ai_reduce_allowed(p, review):
                p.lifecycle_state = "DEFENSIVE"
                p.defense_level = "DEFENSIVE"
                p.lock_status = "AI_DEFENSIVE_PARTIAL_REDUCE"
                ratio = max(0.1, min(0.5, float(review.reduce_ratio or 0.25)))
                return self._record_decision(p, "REDUCE", "AI_DEFENSIVE_PARTIAL_REDUCE", evidence, reduce_ratio=ratio), ""
            return rule_decision, "ai_reduce_blocked_without_profit_or_favorable_r"

        if review.action == "PROTECT" and rule_decision.action in {"HOLD", "PROTECT"}:
            p.lifecycle_state = "DEFENSIVE" if p.defense_level == "DEFENSIVE" else "PROTECTING"
            if not str(p.lock_status or "").startswith("TP1_"):
                p.lock_status = "AI_PROTECT_REVIEW"
            return self._record_decision(p, "PROTECT", "AI_PROTECT_THESIS_ALIVE", evidence), ""

        if (
            review.action == "HOLD"
            and rule_decision.action == "PROTECT"
            and p.defense_level == "WATCH"
            and p.adverse_r <= p.noise_budget_r
        ):
            p.lifecycle_state = "PROTECTING"
            return self._record_decision(p, "HOLD", "AI_HOLD_NORMAL_NOISE", evidence), ""

        return rule_decision, "ai_review_kept_rule_decision"

    def _ai_exit_allowed(self, p: Position, review: PositionPolicyReview) -> bool:
        if p.adverse_r < max(float(p.noise_budget_r or 0.0), 0.35):
            return False
        if review.confidence < 0.55:
            return False
        if review.thesis_alive:
            return False
        return True

    def _ai_reduce_allowed(self, p: Position, review: PositionPolicyReview) -> bool:
        if p.quantity <= 0 or p.quantity <= p.initial_quantity * 0.55:
            return False
        if review.confidence < 0.45:
            return False
        return p.roi > 0 or p.favorable_r >= 0.25 or p.mfe_r >= 0.45

    def _store_ai_review(
        self,
        p: Position,
        review: PositionPolicyReview,
        rule_decision: PositionDecision,
        adjusted: PositionDecision,
        safety_override: str,
    ) -> None:
        row = review.asdict()
        row["rule_action"] = rule_decision.action
        row["rule_reason"] = rule_decision.reason
        row["applied_action"] = adjusted.action
        row["applied_reason"] = adjusted.reason
        row["safety_override"] = safety_override
        p.last_ai_review = row
        log = list(p.ai_review_log or [])
        if self._should_append_ai_review_log(log[-1] if log else {}, row):
            log.append(row)
        p.ai_review_log = log[-20:]
        if isinstance(p.last_decision, dict):
            p.last_decision["ai_review"] = row

    def _should_append_ai_review_log(self, previous: dict, row: dict) -> bool:
        if not previous:
            return True
        keys = ("status", "action", "applied_action", "applied_reason", "safety_override", "reason")
        if any(previous.get(key) != row.get(key) for key in keys):
            return True
        return int(row.get("ts_ms") or 0) - int(previous.get("ts_ms") or 0) >= 5 * 60 * 1000

    def _stage1_soft_lock(self, p: Position, price: float) -> bool:
        risk = self._risk_unit(p)
        if risk <= 0:
            return False
        breakeven = self._net_breakeven_price(p)
        if p.side == "LONG" and price - p.entry_price >= risk * 0.75 and p.stop_loss < breakeven:
            p.stop_loss = round(breakeven, 8)
            p.lifecycle_state = "PROTECTING"
            p.lock_status = "NET_BREAKEVEN_LOCK"
            self._record_decision(p, "PROTECT", "NET_BREAKEVEN_LOCK", ["favorable move reached 0.75R"])
            return True
        if p.side == "SHORT" and p.entry_price - price >= risk * 0.75 and p.stop_loss > breakeven:
            p.stop_loss = round(breakeven, 8)
            p.lifecycle_state = "PROTECTING"
            p.lock_status = "NET_BREAKEVEN_LOCK"
            self._record_decision(p, "PROTECT", "NET_BREAKEVEN_LOCK", ["favorable move reached 0.75R"])
            return True
        return False

    def _hard_stop_reason(self, p: Position, price: float) -> str:
        if p.stage == "Stage 1":
            if p.side == "LONG" and price <= p.stop_loss:
                return "SL"
            if p.side == "SHORT" and price >= p.stop_loss:
                return "SL"
        if p.stage == "Stage 2":
            if p.side == "LONG" and price <= p.stop_loss:
                return "LOCKED_STOP"
            if p.side == "SHORT" and price >= p.stop_loss:
                return "LOCKED_STOP"
        return ""

    def _refresh_realtime_state(self, p: Position, signal, price: float) -> None:
        self._ensure_position_risk_state(p)
        p.adverse_r = round(self._adverse_r(p), 4)
        p.favorable_r = round(self._favorable_r(p), 4)
        risk_usdt = max(0.0, float(p.risk_usdt or 0.0))
        if risk_usdt > 0:
            p.mfe_r = round(float(p.mfe or 0.0) / risk_usdt, 4)
            p.mae_r = round(float(p.mae or 0.0) / risk_usdt, 4)
        else:
            p.mfe_r = round(max(0.0, p.favorable_r), 4)
            p.mae_r = round(-max(0.0, p.adverse_r), 4)
        noise_pct, noise_r = self._noise_budget(p, signal)
        p.noise_budget_pct = round(noise_pct, 4)
        p.noise_budget_r = round(noise_r, 4)

    def _ensure_position_risk_state(self, p: Position) -> None:
        if float(p.initial_stop_loss or 0.0) <= 0:
            p.initial_stop_loss = p.stop_loss
        if float(p.initial_risk_unit or 0.0) <= 0:
            p.initial_risk_unit = abs(float(p.entry_price or 0.0) - float(p.initial_stop_loss or 0.0))

    def _noise_budget(self, p: Position, signal) -> tuple[float, float]:
        risk = self._risk_unit(p)
        entry = max(0.0, float(p.entry_price or 0.0))
        risk_pct = risk / entry if entry > 0 else 0.0
        atr_pct = max(0.0, float(getattr(signal, "atr_pct", 0.0) or 0.0)) / 100.0 if signal else 0.0
        wick_ratio = max(0.0, float(getattr(signal, "wick_ratio", 0.0) or 0.0)) if signal else 0.0
        wick_noise = atr_pct * min(1.5, 0.45 + wick_ratio)
        budget_pct = max(0.0025, risk_pct * 0.35, atr_pct * 0.75, wick_noise * 0.55)
        if risk_pct > 0:
            budget_pct = min(budget_pct, risk_pct * 0.8)
        budget_pct = min(budget_pct, 0.03)
        noise_price = entry * budget_pct
        noise_r = noise_price / risk if risk > 0 else 0.35
        return budget_pct * 100.0, min(0.8, max(0.2, noise_r))

    def _thesis_state(self, p: Position, signal) -> tuple[bool, str, list[str]]:
        evidence = self._position_evidence(p, signal)
        cut_r = max(float(settings.position_adverse_cut_r), float(p.noise_budget_r or 0.0))
        severe_r = max(0.8, cut_r)
        if not signal:
            return True, "THESIS_ALIVE", ["no fresh radar signal; keep contract rules"]
        if self._strong_reverse(signal, p) and p.adverse_r >= cut_r:
            return False, "REVERSE_THESIS_INVALIDATED", evidence
        if signal.fake_breakout_risk == "HIGH" and p.roi <= 0 and p.adverse_r >= severe_r:
            return False, "FAKE_BREAKOUT_THESIS_INVALIDATED", evidence
        if self._score_decayed(signal, p) and p.roi <= 0 and p.adverse_r >= severe_r:
            return False, "SIGNAL_DECAY_THESIS_INVALIDATED", evidence
        if self._flow_against(signal, p) and getattr(signal, "fund_confirm_count", 0) < 2 and p.roi < 0 and p.adverse_r >= cut_r:
            return False, "ADVERSE_FUND_WEAK_CUT", evidence
        return True, "THESIS_ALIVE", evidence

    def _position_evidence(self, p: Position, signal) -> list[str]:
        out = [
            f"adverse_r={p.adverse_r:.2f}",
            f"favorable_r={p.favorable_r:.2f}",
            f"noise_budget_r={p.noise_budget_r:.2f}",
            f"mfe_r={p.mfe_r:.2f}",
            f"mae_r={p.mae_r:.2f}",
        ]
        if not signal:
            return out
        if self._strong_reverse(signal, p):
            out.append("strong_reverse_signal")
        elif getattr(signal, "direction", "") in {"LONG", "SHORT"} and signal.direction != p.side:
            out.append("minor_reverse_signal")
        if self._score_decayed(signal, p):
            out.append("signal_score_decayed")
        if getattr(signal, "fake_breakout_risk", "") == "HIGH":
            out.append("fake_breakout_high")
        elif getattr(signal, "fake_breakout_risk", "") == "MEDIUM":
            out.append("fake_breakout_medium")
        if self._flow_against(signal, p):
            out.append("flow_or_structure_against_position")
        if getattr(signal, "fund_confirm_count", 3) < 2:
            out.append("fund_confirm_weak")
        return out

    def _defense_level(self, p: Position, signal, thesis_alive: bool, evidence: list[str]) -> str:
        if not thesis_alive:
            return "EXIT_READY"
        if not signal:
            return "NORMAL"
        if p.adverse_r >= max(0.35, p.noise_budget_r) or self._strong_reverse(signal, p):
            return "DEFENSIVE"
        if p.adverse_r >= max(0.15, p.noise_budget_r * 0.5):
            return "WATCH"
        if any(x in evidence for x in ("signal_score_decayed", "fake_breakout_medium", "flow_or_structure_against_position")):
            return "WATCH"
        return "NORMAL"

    def _defensive_reduce_ready(self, p: Position, signal, evidence: list[str]) -> bool:
        if not signal or p.quantity <= 0:
            return False
        if p.lock_status == "DEFENSIVE_PARTIAL_REDUCE":
            return False
        if p.quantity <= p.initial_quantity * 0.55:
            return False
        if p.roi <= 0 or p.mfe_r < 0.45:
            return False
        return p.defense_level == "DEFENSIVE" and any(
            x in evidence
            for x in ("strong_reverse_signal", "signal_score_decayed", "fake_breakout_medium", "flow_or_structure_against_position")
        )

    def _strong_reverse(self, signal, p: Position) -> bool:
        return (
            getattr(signal, "direction", "") in {"LONG", "SHORT"}
            and signal.direction != p.side
            and float(getattr(signal, "score", 0.0) or 0.0) >= float(settings.position_reverse_exit_score)
            and int(getattr(signal, "fund_confirm_count", 0) or 0) >= 2
        )

    def _score_decayed(self, signal, p: Position) -> bool:
        return float(getattr(signal, "score", 0.0) or 0.0) <= max(
            20.0,
            float(p.score or 0.0) - float(settings.position_signal_decay_score),
        )

    def _flow_against(self, signal, p: Position) -> bool:
        if p.side == "LONG":
            return (
                float(getattr(signal, "taker_sell_ratio", 0.5) or 0.5) >= 0.58
                or float(getattr(signal, "depth_imbalance", 0.0) or 0.0) <= -0.12
                or self._timeframes_against(signal, p)
            )
        return (
            float(getattr(signal, "taker_buy_ratio", 0.5) or 0.5) >= 0.58
            or float(getattr(signal, "depth_imbalance", 0.0) or 0.0) >= 0.12
            or self._timeframes_against(signal, p)
        )

    def _timeframes_against(self, signal, p: Position) -> bool:
        c5 = float(getattr(signal, "change_5m", 0.0) or 0.0)
        c15 = float(getattr(signal, "change_15m", 0.0) or 0.0)
        c1h = float(getattr(signal, "change_1h", 0.0) or 0.0)
        if p.side == "LONG":
            return c5 < 0 and c15 < 0 and c1h <= 0
        return c5 > 0 and c15 > 0 and c1h >= 0

    def _record_decision(
        self,
        p: Position,
        action: str,
        reason: str,
        evidence: list[str] | None = None,
        *,
        reduce_ratio: float = 0.0,
    ) -> PositionDecision:
        decision = PositionDecision(
            ts_ms=now_ms(),
            action=action,
            reason=reason,
            defense_level=p.defense_level,
            thesis_alive=bool(p.thesis_alive),
            adverse_r=round(float(p.adverse_r or 0.0), 4),
            favorable_r=round(float(p.favorable_r or 0.0), 4),
            mfe_r=round(float(p.mfe_r or 0.0), 4),
            mae_r=round(float(p.mae_r or 0.0), 4),
            noise_budget_pct=round(float(p.noise_budget_pct or 0.0), 4),
            noise_budget_r=round(float(p.noise_budget_r or 0.0), 4),
            reduce_ratio=round(float(reduce_ratio or 0.0), 4),
            evidence=list(evidence or []),
        )
        row = decision.asdict()
        p.last_decision = row
        log = list(p.decision_log or [])
        if self._should_append_decision_log(log[-1] if log else {}, row):
            log.append(row)
        p.decision_log = log[-30:]
        return decision

    def _should_append_decision_log(self, previous: dict, row: dict) -> bool:
        if not previous:
            return True
        if row.get("action") in {"REDUCE", "EXIT"}:
            return True
        signature_keys = ("action", "reason", "defense_level", "thesis_alive")
        if any(previous.get(key) != row.get(key) for key in signature_keys):
            return True
        for key in ("adverse_r", "favorable_r", "mfe_r", "mae_r", "noise_budget_r"):
            if abs(float(previous.get(key) or 0.0) - float(row.get(key) or 0.0)) >= 0.05:
                return True
        return False

    def _signal_exit_reason(self, p: Position, signal) -> str:
        if not signal:
            return ""
        adverse_r = self._adverse_r(p)
        if signal.fake_breakout_risk == "HIGH" and p.roi <= 0:
            p.lifecycle_state = "DEFENSIVE"
            p.lock_status = "DEFENSIVE_FAKE_BREAKOUT"
            if adverse_r >= max(0.8, float(settings.position_adverse_cut_r)):
                p.lifecycle_state = "EXIT_READY"
                return "FAKE_BREAKOUT_THESIS_INVALIDATED"
        if signal.score <= max(20.0, p.score - settings.position_signal_decay_score) and p.roi <= 0:
            p.lifecycle_state = "DEFENSIVE"
            p.lock_status = "DEFENSIVE_SIGNAL_DECAY"
            if adverse_r >= max(0.8, float(settings.position_adverse_cut_r)):
                p.lifecycle_state = "EXIT_READY"
                return "SIGNAL_DECAY_THESIS_INVALIDATED"
        if signal.direction in {"LONG", "SHORT"} and signal.direction != p.side:
            if signal.score >= settings.position_reverse_exit_score and signal.fund_confirm_count >= 2:
                p.lifecycle_state = "DEFENSIVE"
                p.lock_status = "DEFENSIVE_REVERSE_SIGNAL"
                if adverse_r >= float(settings.position_adverse_cut_r):
                    p.lifecycle_state = "EXIT_READY"
                    return "REVERSE_THESIS_INVALIDATED"
        return ""

    def _adverse_exit_reason(self, p: Position, signal, price: float) -> str:
        if not signal or p.stage != "Stage 1":
            return ""
        risk = self._risk_unit(p)
        if risk <= 0:
            return ""
        adverse = p.entry_price - price if p.side == "LONG" else price - p.entry_price
        if adverse < risk * settings.position_adverse_cut_r:
            return ""
        if signal.fake_breakout_risk in {"MEDIUM", "HIGH"}:
            p.lifecycle_state = "EXIT_READY"
            return "ADVERSE_FAKE_CUT"
        if signal.direction in {"LONG", "SHORT"} and signal.direction != p.side and signal.score >= max(50.0, p.score - 5.0):
            p.lifecycle_state = "EXIT_READY"
            return "ADVERSE_REVERSE_CUT"
        if signal.score <= p.score - settings.position_adverse_cut_score_drop:
            p.lifecycle_state = "EXIT_READY"
            return "ADVERSE_SIGNAL_CUT"
        if signal.fund_confirm_count < 2 and p.roi < 0:
            p.lifecycle_state = "EXIT_READY"
            return "ADVERSE_FUND_WEAK_CUT"
        return ""

    def _max_hold_expired(self, p: Position) -> bool:
        max_hold_seconds = self._max_hold_seconds(p)
        if max_hold_seconds <= 0:
            return False
        age_s = (now_ms() - p.open_time) / 1000
        return age_s >= max_hold_seconds and p.roi <= 0

    def _max_hold_seconds(self, p: Position) -> int:
        configured = int(settings.position_max_hold_seconds or 0)
        contract = p.strategy_contract if isinstance(p.strategy_contract, dict) else {}
        time_stop = contract.get("time_stop") if isinstance(contract.get("time_stop"), dict) else {}
        contract_seconds = _safe_positive_int(time_stop.get("seconds"))
        if configured > 0 and contract_seconds > 0:
            return min(configured, contract_seconds)
        return contract_seconds or configured

    def _risk_unit(self, p: Position) -> float:
        self._ensure_position_risk_state(p)
        return max(0.0, float(p.initial_risk_unit or 0.0))

    def _adverse_r(self, p: Position) -> float:
        risk = self._risk_unit(p)
        if risk <= 0:
            return 0.0
        adverse = p.entry_price - p.current_price if p.side == "LONG" else p.current_price - p.entry_price
        return max(0.0, adverse / risk)

    def _favorable_r(self, p: Position) -> float:
        risk = self._risk_unit(p)
        if risk <= 0:
            return 0.0
        favorable = p.current_price - p.entry_price if p.side == "LONG" else p.entry_price - p.current_price
        return max(0.0, favorable / risk)

    def _entry_fee_alloc(self, p: Position, quantity: float) -> float:
        if p.initial_quantity <= 0:
            return 0.0
        return max(0.0, float(p.entry_fee or 0.0)) * max(0.0, float(quantity or 0.0)) / p.initial_quantity

    def _net_breakeven_price(self, p: Position) -> float:
        cost_buffer = 2.0 * max(0.0, float(settings.paper_taker_fee_rate)) + 2.0 * max(0.0, float(settings.paper_slippage_pct))
        if p.side == "LONG":
            return p.entry_price * (1.0 + cost_buffer)
        return p.entry_price * (1.0 - cost_buffer)

    def _is_live_position(self, p: Position) -> bool:
        if getattr(p, "status", "") != "OPEN":
            return False
        open_order = p.exchange_open_order if isinstance(p.exchange_open_order, dict) else {}
        if open_order.get("testOrder") or getattr(p, "lock_status", "") == "LIVE_TEST_ORDER":
            return False
        has_exchange_order = bool(open_order.get("orderId") or open_order.get("clientOrderId") or open_order.get("origClientOrderId"))
        return str(p.position_id or "").startswith("livepos") or has_exchange_order

    def _ensure_lifecycle_contract(self, p: Position) -> None:
        self._ensure_position_risk_state(p)
        if not isinstance(p.decision_log, list):
            p.decision_log = []
        if not isinstance(p.last_decision, dict):
            p.last_decision = {}
        if not isinstance(p.ai_review_log, list):
            p.ai_review_log = []
        if not isinstance(p.last_ai_review, dict):
            p.last_ai_review = {}
        contract = p.strategy_contract if isinstance(p.strategy_contract, dict) else {}
        if not contract:
            p.strategy_contract = contract
        contract.setdefault(
            "position_lifecycle",
            {
                "states": [
                    "WAITING",
                    "ENTRY_READY",
                    "OPENED",
                    "PROTECTING",
                    "TREND_HOLD",
                    "SCALE_IN",
                    "SCALE_OUT",
                    "DEFENSIVE",
                    "EXIT_READY",
                    "CLOSED",
                ],
                "initial_state_after_fill": "PROTECTING",
                "principle": "scan results are evidence, not orders; position management controls the lifecycle after entry.",
            },
        )
        contract.setdefault(
            "hold_logic",
            {
                "continue_holding_if": [
                    "hard stop has not fired",
                    "trade thesis is not invalidated",
                    "minor reverse signal has not broken market structure",
                ],
                "do_not_exit_for": ["minor reverse signal alone", "small score noise alone"],
            },
        )
        contract.setdefault(
            "reduce_logic",
            {
                "reduce_if": [
                    "TP1 is reached with net profit after cost",
                    "risk weakens but thesis is not fully invalidated",
                ],
                "tp1_close_ratio": 0.5,
            },
        )
        contract.setdefault("add_logic", {"add_if": ["disabled until research proves scale-in edge"], "max_adds": 0})
        contract.setdefault(
            "exit_logic",
            {
                "core_exit_only_if": [
                    "hard stop is hit",
                    "trade thesis is invalidated",
                    "risk limit is hit",
                    "time stop fires without favorable development",
                    "TP2 is reached",
                ],
                "minor_reverse_signal_action": "DEFENSIVE first; do not close core position unless thesis invalidation also occurs.",
            },
        )
        contract.setdefault(
            "realtime_position_manager",
            {
                "decision_actions": ["HOLD", "PROTECT", "REDUCE", "EXIT", "NOOP"],
                "noise_principle": "normal adverse movement inside the noise budget is not thesis failure",
                "exit_principle": "exit only when a hard stop fires, TP2 is reached, or the trade thesis is invalidated beyond the noise budget",
                "learning_fields": [
                    "adverse_r",
                    "favorable_r",
                    "mfe_r",
                    "mae_r",
                    "noise_budget_r",
                    "defense_level",
                    "thesis_alive",
                    "decision_log",
                ],
            },
        )
        contract.setdefault(
            "time_stop",
            {
                "seconds": int(settings.position_max_hold_seconds or 0),
                "rule": "Exit or reduce risk if the trade does not develop before the time stop and remains non-profitable.",
            },
        )
        contract.setdefault("review_metrics", ["MFE", "MAE", "R_multiple", "max_drawdown", "hold_time"])
        p.strategy_contract = contract

    def summary(self):
        opens = position_registry.list_open()
        from backend.trading.performance_guard import performance_guard

        closed = performance_guard.performance_rows(position_registry.list_closed())
        floating = sum(p.unrealized_pnl for p in opens)
        realized = sum(x.get("pnl", 0) for x in closed)
        used_margin = sum(float(p.margin) for p in opens)
        win = sum(1 for x in closed if x.get("pnl", 0) > 0)
        loss = sum(1 for x in closed if x.get("pnl", 0) < 0)
        total = win + loss
        equity = float(settings.paper_account_equity_usdt or 1000.0) + realized + floating
        return {
            "open_count": len(opens),
            "floating_pnl": round(floating, 4),
            "realized_pnl": round(realized, 4),
            "total_pnl": round(floating + realized, 4),
            "win_count": win,
            "loss_count": loss,
            "win_rate": round(win / total * 100, 2) if total else 0.0,
            "used_margin": round(used_margin, 4),
            "available_balance": round(max(0.0, equity - used_margin), 4),
            "performance_guard": performance_guard.summary(),
        }


def shutil_which_codex() -> bool:
    from backend.ai_strategy.codex_cli_strategy_client import default_codex_command

    command = settings.codex_command or default_codex_command()
    return bool(shutil.which(command) or (command and Path(command).exists()))


def _safe_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


position_manager = PositionManager()


def _safe_positive_int(value) -> int:
    try:
        parsed = int(float(value))
    except Exception:
        return 0
    return parsed if parsed > 0 else 0
