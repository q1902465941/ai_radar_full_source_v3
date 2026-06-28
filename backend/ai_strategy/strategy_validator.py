from backend.models import StrategyPlan
from backend.ai_strategy.strategy_contract import contract_quality

class StrategyValidator:
    def validate(self, plan: StrategyPlan) -> tuple[bool,str]:
        if plan.action == "WAIT" or plan.action == "PAPER_OBSERVE":
            if not plan.reason: return False, "wait_without_reason"
            return True, "ok"
        entry=plan.ideal_entry_price; sl=plan.stop_loss; tp1=plan.tp1; tp2=plan.tp2
        if entry <= 0 or sl <= 0 or tp1 <= 0 or tp2 <= 0: return False, "non_positive_price"
        if abs(entry-sl)/entry < 0.002: return False, "sl_too_close"
        if abs(tp1-entry)/entry < 0.003: return False, "tp1_too_close"
        if abs(tp2-entry)/entry < 0.006: return False, "tp2_too_close"
        if plan.side == "LONG" and not (sl < entry < tp1 < tp2): return False, "invalid_long_geometry"
        if plan.side == "SHORT" and not (tp2 < tp1 < entry < sl): return False, "invalid_short_geometry"
        if plan.side not in ["LONG","SHORT"]: return False, "invalid_side"
        contract_ok, contract_reasons = contract_quality(plan.raw.get("strategy_contract"))
        if not contract_ok:
            return False, ",".join(contract_reasons[:3])
        return True, "ok"

strategy_validator = StrategyValidator()
