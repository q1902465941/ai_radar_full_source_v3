from backend.models import ExecutionPlan, Position, new_id, now_ms
from backend.positions.position_registry import position_registry
from backend.trading.trade_economics import market_fill_price, stop_distance_pct, trade_fee, trade_notional

class PaperExecutor:
    async def open_position(self, signal_id: str, strategy_id: str, score: float, plan: ExecutionPlan) -> Position:
        entry_fill = market_fill_price(plan.side, plan.entry_price, "entry")
        notional = trade_notional(entry_fill, plan.quantity)
        margin = notional / plan.dynamic_leverage if plan.dynamic_leverage > 0 else plan.dynamic_margin
        entry_fee = trade_fee(notional)
        risk_usdt = plan.risk_usdt or (notional * stop_distance_pct(entry_fill, plan.stop_loss))
        p=Position(
            position_id=new_id("pos"), strategy_id=strategy_id, source_signal_id=signal_id, symbol=plan.symbol,
            side=plan.side, status="OPEN", stage="Stage 1", score=score, entry_price=entry_fill, current_price=entry_fill,
            quantity=plan.quantity, initial_quantity=plan.quantity, margin=round(margin, 4), leverage=plan.dynamic_leverage,
            stop_loss=plan.stop_loss, tp1=plan.tp1, tp2=plan.tp2, best_price=entry_fill,
            initial_stop_loss=plan.stop_loss, initial_risk_unit=round(abs(entry_fill - plan.stop_loss), 8),
            notional=round(notional, 4), entry_fee=round(entry_fee, 8), risk_usdt=round(risk_usdt, 4), risk_pct=plan.risk_pct,
            open_time=now_ms(),
            strategy_contract=plan.strategy_contract,
        )
        position_registry.add(p)
        return p

paper_executor = PaperExecutor()
