from __future__ import annotations

from backend.ai_strategy.strategy_quality_gate import strategy_quality_gate
from backend.config import settings
from backend.learning.learned_risk_guard import learned_risk_guard
from backend.models import ExecutionPlan, RadarItem, StrategyPlan
from backend.trading.performance_guard import PerformanceGuardReport, performance_guard
from backend.trading.trade_economics import reward_r, round_trip_cost_pct, stop_distance_pct, target_profit_breakdown


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _quality_reject_reasons(quality) -> list[str]:
    positive = {
        "score_strong",
        "score_good",
        "score_ok",
        "heat_accelerating",
        "volume_confirmed",
        "dealer_extension",
        "ai_confidence_good",
        "recent_matched_pattern_recovered",
    }
    reasons = [str(reason) for reason in getattr(quality, "reasons", []) or []]
    blocking = [reason for reason in reasons if reason not in positive]
    return (blocking or reasons)[:3]


class AutoTradingRiskModel:
    """Risk-first execution model.

    The old model started from a tiny margin multiplier, which produced positions
    with 2-5 USDT notional value. This model starts from account allocation,
    checks exchange-like minimum notional, then caps loss at the stop.
    """

    def decide(self, item: RadarItem, plan: StrategyPlan, account: dict, market: dict, *, paper_probe: bool = False) -> ExecutionPlan:
        paper_probe = bool(
            paper_probe
            and account.get("execution_context") == "paper_closed_loop"
            and not settings.live_trading_enabled
        )
        controlled_paper_acceptance = _is_controlled_paper_acceptance(plan, account)
        if plan.action == "WAIT":
            return self._observe(item, plan, "WAIT", 60, "AI requested wait")
        if (
            settings.require_codex_strategy_for_entry
            and not _is_codex_generated(plan)
            and not controlled_paper_acceptance
        ):
            provider = str((plan.raw or {}).get("provider") or "unknown")
            return self._observe(
                item,
                plan,
                "OBSERVE",
                300,
                f"Codex-generated strategy required before entry; provider={provider}",
            )
        if item.fake_breakout_risk == "HIGH":
            return self._observe(item, plan, "OBSERVE", 180, "fake breakout risk is high")

        live_context = (
            str(account.get("execution_context") or "").lower() == "live"
            or str(account.get("trade_mode") or "").lower() == "live"
        )
        if live_context and account.get("can_trade") is False:
            return self._observe(item, plan, "OBSERVE", 300, "account canTrade=false")

        entry = float(plan.ideal_entry_price or item.price or 0.0)
        stop_pct = stop_distance_pct(entry, plan.stop_loss)
        if entry <= 0 or stop_pct <= 0:
            return self._observe(item, plan, "OBSERVE", 180, "invalid entry or stop distance")
        current_price = float(item.price or 0.0)
        if current_price <= 0:
            return self._observe(item, plan, "OBSERVE", 180, "invalid current market price")
        zone_low = min(float(plan.entry_zone_low or entry), float(plan.entry_zone_high or entry))
        zone_high = max(float(plan.entry_zone_low or entry), float(plan.entry_zone_high or entry))
        zone_pad = current_price * 0.0005
        if zone_low > 0 and zone_high > 0 and not (zone_low - zone_pad <= current_price <= zone_high + zone_pad):
            return self._observe(
                item,
                plan,
                "OBSERVE",
                120,
                f"market price outside entry zone: price={current_price:.8g}, zone={zone_low:.8g}-{zone_high:.8g}",
            )
        entry_drift_pct = abs(current_price - entry) / current_price
        if entry_drift_pct > max(0.0005, float(settings.trade_max_entry_drift_pct)):
            return self._observe(
                item,
                plan,
                "OBSERVE",
                120,
                f"entry price drift too large: drift={entry_drift_pct:.2%}",
            )

        quality = strategy_quality_gate.evaluate(item, plan)
        plan.raw = {**plan.raw, "quality_gate": quality.asdict()}
        if paper_probe and (quality.tp2_r < 1.4 or quality.cost_r > 0.75):
            return self._observe(
                item,
                plan,
                "OBSERVE",
                180,
                f"paper probe rejected by basic economics: tp2R={quality.tp2_r:.2f}, costR={quality.cost_r:.2f}",
            )
        if not paper_probe and not quality.paper_ok:
            return self._observe(
                item,
                plan,
                "OBSERVE",
                180,
                f"quality rejected: win={quality.estimated_win_rate:.2%}, ev={quality.expected_r:.2f}R, reasons={','.join(_quality_reject_reasons(quality))}",
            )

        bypass_performance_guard = (
            not settings.auto_trading_use_performance_guard
            and account.get("execution_context") == "paper_closed_loop"
            and not performance_guard.summary().get("recovery_mode")
        )
        if paper_probe:
            perf_summary = performance_guard.summary()
            performance = PerformanceGuardReport(
                allow=True,
                recovery_mode=bool(perf_summary.get("recovery_mode")),
                reasons=["bypassed_for_paper_probe_sampling"],
                global_trades=int(perf_summary.get("trades") or 0),
                global_win_rate=float(perf_summary.get("win_rate") or 0.0),
                global_pnl=float(perf_summary.get("pnl") or 0.0),
                recent_win_rate=float(perf_summary.get("recent_win_rate") or 0.0),
                loss_streak=int(perf_summary.get("loss_streak") or 0),
                symbol_side_trades=0,
                symbol_side_win_rate=0.0,
                symbol_side_pnl=0.0,
                direction_confirmations=0,
            )
        elif not bypass_performance_guard:
            performance = performance_guard.evaluate(item, plan, quality)
        else:
            performance = PerformanceGuardReport(
                allow=True,
                recovery_mode=False,
                reasons=["bypassed_for_paper_closed_loop"],
                global_trades=0,
                global_win_rate=0.0,
                global_pnl=0.0,
                recent_win_rate=0.0,
                loss_streak=0,
                symbol_side_trades=0,
                symbol_side_win_rate=0.0,
                symbol_side_pnl=0.0,
                direction_confirmations=0,
            )
        plan.raw = {**plan.raw, "performance_guard": performance.asdict()}
        if not performance.allow:
            return self._observe(
                item,
                plan,
                "OBSERVE",
                300,
                f"performance guard rejected: {','.join(performance.reasons[:4])}; history win={performance.global_win_rate:.1%}, pnl={performance.global_pnl:.2f}",
            )

        equity = max(1.0, float(account.get("equity") or 1000.0))
        available = float(account.get("available_balance") or equity)
        reserve = equity * _clamp(float(settings.trade_reserved_balance_pct), 0.0, 0.8)
        usable_available = max(0.0, available - reserve)
        if usable_available <= 0:
            return self._observe(item, plan, "OBSERVE", 180, "available balance below reserve")

        loss_streak = int(account.get("loss_streak", 0) or 0)
        market_heat = float(market.get("market_heat", 50) or 50)
        volatility = str(market.get("volatility_regime", "normal") or "normal")

        signal_factor = _clamp((item.score / 100.0) * (plan.confidence / 100.0), 0.35, 0.95)
        quality_factor = _clamp(quality.estimated_win_rate / max(settings.strategy_min_live_win_rate, 0.01), 0.75, 1.35)
        ev_factor = _clamp(1.0 + quality.expected_r, 0.8, 1.6)
        fund_factor = {3: 1.15, 2: 0.85, 1: 0.45, 0: 0.0}.get(min(3, int(item.fund_confirm_count or 0)), 0.0)
        fake_factor = {"LOW": 1.0, "MEDIUM": 0.55, "HIGH": 0.0}.get(item.fake_breakout_risk, 0.55)
        drawdown_factor = 0.25 if loss_streak >= 3 else (0.55 if loss_streak == 2 else 1.0)
        if paper_probe:
            quality_factor = _clamp(max(quality.estimated_win_rate, 0.32) / 0.55, 0.65, 1.05)
            ev_factor = _clamp(1.0 + quality.expected_r, 0.65, 1.15)
            drawdown_factor = max(drawdown_factor, 0.85)
        heat_factor = 0.75 if market_heat > 82 else (1.0 if market_heat >= 55 else 0.8)
        vol_margin_factor = {"extreme": 0.35, "high": 0.65, "normal": 1.0, "low": 0.85}.get(volatility, 1.0)

        target_margin = (
            equity
            * max(0.0, float(settings.trade_target_margin_pct))
            * signal_factor
            * quality_factor
            * ev_factor
            * fund_factor
            * fake_factor
            * drawdown_factor
            * heat_factor
            * vol_margin_factor
        )

        floor_margin = max(float(settings.trade_min_margin_usdt), equity * 0.025)
        cap_margin = min(usable_available, equity * max(0.01, float(settings.trade_max_margin_pct)))
        if cap_margin < floor_margin:
            return self._observe(
                item,
                plan,
                "OBSERVE",
                180,
                f"insufficient usable balance: cap_margin={cap_margin:.2f}, floor_margin={floor_margin:.2f}",
            )

        leverage = self._select_leverage(stop_pct, item, volatility)
        margin = _clamp(target_margin, floor_margin, cap_margin)
        notional = margin * leverage

        min_notional = max(float(settings.trade_min_notional_usdt), floor_margin * min(leverage, 2))
        if notional < min_notional:
            required_margin = min_notional / leverage
            if required_margin > cap_margin:
                return self._observe(
                    item,
                    plan,
                    "OBSERVE",
                    180,
                    f"notional below minimum and balance cap prevents resize: required_margin={required_margin:.2f}",
                )
            margin = max(margin, required_margin)
            notional = margin * leverage

        fee_slip_buffer_pct = round_trip_cost_pct()
        risk_usdt = notional * (stop_pct + fee_slip_buffer_pct)
        vol_risk_factor = {"extreme": 0.35, "high": 0.65, "normal": 1.0, "low": 0.85}.get(volatility, 1.0)
        max_risk_usdt = equity * max(0.001, float(settings.trade_max_risk_pct)) * drawdown_factor * vol_risk_factor
        if risk_usdt > max_risk_usdt:
            capped_notional = max_risk_usdt / max(stop_pct + fee_slip_buffer_pct, 0.0001)
            capped_margin = capped_notional / leverage
            if capped_margin < floor_margin or capped_notional < min_notional:
                return self._observe(
                    item,
                    plan,
                    "OBSERVE",
                    180,
                    f"risk cap blocks trade: risk={risk_usdt:.2f}, max={max_risk_usdt:.2f}",
                )
            margin = min(margin, capped_margin)
            notional = margin * leverage
            risk_usdt = notional * (stop_pct + fee_slip_buffer_pct)

        quantity = notional / entry
        if quantity <= 0:
            return self._observe(item, plan, "OBSERVE", 180, "quantity is zero")

        tp1_close_ratio = 0.5
        economics = target_profit_breakdown(plan.side, entry, quantity, plan.tp1, plan.tp2, tp1_close_ratio)
        min_net_profit = max(0.0, float(settings.trade_min_net_profit_usdt))
        min_profit_cost_ratio = max(0.0, float(settings.trade_min_profit_cost_ratio))
        if economics.net_pnl < min_net_profit:
            return self._observe(
                item,
                plan,
                "OBSERVE",
                180,
                (
                    f"target net profit too small: net={economics.net_pnl:.4f}USDT, "
                    f"min={min_net_profit:.4f}USDT, notional={notional:.2f}USDT"
                ),
            )
        if economics.cost_drag > 0 and economics.profit_cost_ratio < min_profit_cost_ratio:
            return self._observe(
                item,
                plan,
                "OBSERVE",
                180,
                (
                    f"profit does not cover fees/slippage enough: net/cost={economics.profit_cost_ratio:.2f}, "
                    f"min={min_profit_cost_ratio:.2f}, cost={economics.cost_drag:.4f}USDT"
                ),
            )
        if economics.tp1_net_pnl <= 0:
            return self._observe(
                item,
                plan,
                "OBSERVE",
                180,
                f"tp1 partial close is not net profitable after costs: tp1_net={economics.tp1_net_pnl:.4f}USDT",
            )

        learned = learned_risk_guard.evaluate(item, plan, recovery_mode=performance.recovery_mode)
        plan.raw = {**plan.raw, "learned_guard": learned.asdict()}
        if not paper_probe and not learned.allow_paper:
            return self._observe(
                item,
                plan,
                "OBSERVE",
                300,
                (
                    "learned guard rejected: "
                    f"{','.join(learned.reasons[:4])}; "
                    f"matched={learned.matched_samples}, win={learned.win_rate:.1%}, pf={learned.profit_factor:.2f}, pnl={learned.pnl:.2f}"
                ),
            )

        open_slots = int(account.get("open_positions", 0) or 0)
        max_open = int(account.get("max_open_positions", settings.max_open_positions) or settings.max_open_positions)
        if open_slots >= max_open:
            decision = "OBSERVE"
        elif paper_probe:
            decision = "PAPER_ONLY"
        elif quality.live_ok and learned.allow_live:
            decision = "OPEN"
        else:
            decision = "PAPER_ONLY"

        mode = "paper" if item.fake_breakout_risk == "MEDIUM" or volatility == "extreme" else str(account.get("trade_mode", "paper"))
        tp1_r = reward_r(plan.side, entry, plan.stop_loss, plan.tp1)
        tp2_r = reward_r(plan.side, entry, plan.stop_loss, plan.tp2)
        return ExecutionPlan(
            decision=decision,
            mode=mode,
            symbol=item.symbol,
            side=plan.side,
            dynamic_margin=round(margin, 4),
            dynamic_leverage=leverage,
            quantity=round(quantity, 6),
            entry_price=entry,
            stop_loss=plan.stop_loss,
            tp1=plan.tp1,
            tp2=plan.tp2,
            tp1_close_ratio=tp1_close_ratio,
            tp2_close_ratio=1.0,
            management_mode="RISK_LOCK_AND_TRAIL",
            cooldown_after_trade=300,
            reason=(
                ("paper_probe; " if paper_probe else "")
                +
                f"margin={margin:.2f}USDT, notional={notional:.2f}USDT, lev={leverage}x, "
                f"risk={risk_usdt:.2f}USDT/{risk_usdt / equity:.2%}, "
                f"win={quality.estimated_win_rate:.1%}, ev={quality.expected_r:.2f}R, "
                f"tpR={tp1_r:.2f}/{tp2_r:.2f}, target_net={economics.net_pnl:.2f}USDT, "
                f"net/cost={economics.profit_cost_ratio:.2f}"
            ),
            notional=round(notional, 4),
            risk_usdt=round(risk_usdt, 4),
            risk_pct=round(risk_usdt / equity * 100.0, 4),
            strategy_contract=plan.raw.get("strategy_contract", {}),
        )

    def _select_leverage(self, stop_pct: float, item: RadarItem, volatility: str) -> int:
        if volatility == "extreme":
            return 1
        if stop_pct <= 0.004 and item.fund_confirm_count >= 3:
            return 4
        if stop_pct <= 0.008:
            return 3
        if stop_pct <= 0.015:
            return 2
        return 1

    def _observe(self, item: RadarItem, plan: StrategyPlan, decision: str, cooldown: int, reason: str) -> ExecutionPlan:
        return ExecutionPlan(
            decision,
            "paper",
            item.symbol,
            item.direction,
            0,
            0,
            0,
            float(plan.ideal_entry_price or item.price or 0.0),
            plan.stop_loss,
            plan.tp1,
            plan.tp2,
            0.5,
            1.0,
            "NONE",
            cooldown,
            reason,
            strategy_contract=plan.raw.get("strategy_contract", {}),
        )


auto_trading_risk_model = AutoTradingRiskModel()


def _is_codex_generated(plan: StrategyPlan) -> bool:
    provider = str((plan.raw or {}).get("provider") or "").strip()
    return provider == "codex_cli"


def _is_controlled_paper_acceptance(plan: StrategyPlan, account: dict) -> bool:
    raw = plan.raw if isinstance(plan.raw, dict) else {}
    return bool(
        str(account.get("execution_context") or "") == "paper_closed_loop"
        and not settings.live_trading_enabled
        and raw.get("acceptance_mode") is True
        and str(raw.get("candidate_source") or "") == "acceptance_controlled_paper_cycle"
    )
