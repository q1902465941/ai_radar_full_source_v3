from backend.models import RadarItem, StrategyPlan, new_id
from backend.ai_strategy.strategy_contract import attach_contract, build_rule_contract
from backend.radar.market_classifier import market_classifier


class RuleStrategyGenerator:
    def generate(self, item: RadarItem) -> StrategyPlan:
        structured = self._from_market_structure(item, "strat", paper_probe=False)
        if structured is not None:
            return structured
        p = item.price
        side = item.direction
        if side == "NEUTRAL":
            return StrategyPlan(
                new_id("strat"),
                "WAIT",
                item.symbol,
                "NEUTRAL",
                p,
                p,
                p,
                p,
                p,
                p,
                0,
                "direction is neutral, wait for next scan",
                "WAIT_FOR_CONFIRMATION",
            )

        risk_mult = 1.25 if item.fake_breakout_risk == "MEDIUM" else 1.0
        stop_pct = max(0.004, min(0.018, (0.012 - min(item.score, 90) / 10000) * risk_mult))
        tp1_pct = max(0.005, stop_pct * 1.15)
        tp2_pct = max(0.010, stop_pct * 2.1)
        if item.fund_confirm_count < min(3, item.fund_confirm_total):
            return StrategyPlan(
                new_id("strat"),
                "WAIT",
                item.symbol,
                side,
                p,
                p,
                p,
                p,
                p,
                p,
                40,
                f"fund confirmation is below {min(3, item.fund_confirm_total)} required confirmations, wait for stronger flow",
                "WAIT_FOR_CONFIRMATION",
            )
        plan = self._open_plan(item, "strat", stop_pct, tp1_pct, tp2_pct, confidence=min(95, max(45, item.score)))
        return attach_contract(plan, build_rule_contract(item, plan, paper_probe=False))

    def generate_probe(self, item: RadarItem) -> StrategyPlan:
        structured = self._from_market_structure(item, "probe", paper_probe=True)
        if structured is not None:
            return structured
        p = item.price
        side = item.direction
        if side == "NEUTRAL":
            return StrategyPlan(
                new_id("probe"),
                "WAIT",
                item.symbol,
                "NEUTRAL",
                p,
                p,
                p,
                p,
                p,
                p,
                0,
                "paper probe waits for direction",
                "WAIT_FOR_CONFIRMATION",
            )
        risk_mult = 1.2 if item.fake_breakout_risk == "MEDIUM" else 1.0
        stop_pct = max(0.006, min(0.02, (0.013 - min(item.score, 90) / 10000) * risk_mult))
        tp1_pct = max(0.0075, stop_pct * 1.25)
        tp2_pct = max(0.015, stop_pct * 2.45)
        confidence = max(45, min(65, item.score + item.fund_confirm_count * 8))
        plan = self._open_plan(item, "probe", stop_pct, tp1_pct, tp2_pct, confidence=confidence)
        plan.reason = f"paper_probe: score={item.score}, fund={item.fund_confirm_count}/{item.fund_confirm_total}, fake={item.fake_breakout_risk}"
        plan.raw = {"paper_probe": True}
        return attach_contract(plan, build_rule_contract(item, plan, paper_probe=True))

    def _open_plan(
        self,
        item: RadarItem,
        id_prefix: str,
        stop_pct: float,
        tp1_pct: float,
        tp2_pct: float,
        *,
        confidence: float,
    ) -> StrategyPlan:
        p = item.price
        side = item.direction
        if side == "LONG":
            sl = p * (1 - stop_pct)
            tp1 = p * (1 + tp1_pct)
            tp2 = p * (1 + tp2_pct)
            action = "OPEN_LONG"
        else:
            sl = p * (1 + stop_pct)
            tp1 = p * (1 - tp1_pct)
            tp2 = p * (1 - tp2_pct)
            action = "OPEN_SHORT"
        return StrategyPlan(
            strategy_id=new_id(id_prefix),
            action=action,
            symbol=item.symbol,
            side=side,
            entry_zone_low=min(p * 0.999, p * 1.001),
            entry_zone_high=max(p * 0.999, p * 1.001),
            ideal_entry_price=p,
            stop_loss=round(sl, 8),
            tp1=round(tp1, 8),
            tp2=round(tp2, 8),
            confidence=confidence,
            reason=f"{item.trigger_mode}, fund={item.fund_confirm_count}/{item.fund_confirm_total}, {item.dealer_radar}",
        )

    def _from_market_structure(self, item: RadarItem, id_prefix: str, *, paper_probe: bool) -> StrategyPlan | None:
        structure = item.market_structure if isinstance(item.market_structure, dict) and item.market_structure else market_classifier.classify(item)
        action = str(structure.get("action") or "")
        if paper_probe and action == "WAIT":
            structure = market_classifier.classify_probe(item, structure)
            action = str(structure.get("action") or "")
        if action == "WAIT":
            reasons = structure.get("no_trade_reasons") or ["market_structure_wait"]
            return StrategyPlan(
                strategy_id=new_id(id_prefix),
                action="WAIT",
                symbol=item.symbol,
                side=structure.get("bias") if structure.get("bias") in {"LONG", "SHORT"} else item.direction,
                entry_zone_low=float(item.price or 0.0),
                entry_zone_high=float(item.price or 0.0),
                ideal_entry_price=float(item.price or 0.0),
                stop_loss=0.0,
                tp1=0.0,
                tp2=0.0,
                confidence=float(structure.get("confidence") or 0.0),
                reason="market_structure_wait:" + ",".join(str(reason) for reason in reasons[:4]),
                wait_type=str(structure.get("phase") or "WAIT_FOR_CONFIRMATION"),
                raw={"market_structure": structure, **({"paper_probe": True} if paper_probe else {})},
            )
        if action not in {"OPEN_LONG", "OPEN_SHORT"}:
            return None

        side = "LONG" if action == "OPEN_LONG" else "SHORT"
        entry = float(structure.get("ideal_entry_price") or 0.0)
        low = float(structure.get("entry_zone_low") or 0.0)
        high = float(structure.get("entry_zone_high") or 0.0)
        stop = float(structure.get("stop_loss") or 0.0)
        tp1 = float(structure.get("tp1") or 0.0)
        tp2 = float(structure.get("tp2") or 0.0)
        if not self._valid_geometry(side, low, high, entry, stop, tp1, tp2):
            return StrategyPlan(
                strategy_id=new_id(id_prefix),
                action="WAIT",
                symbol=item.symbol,
                side=side,
                entry_zone_low=float(item.price or 0.0),
                entry_zone_high=float(item.price or 0.0),
                ideal_entry_price=float(item.price or 0.0),
                stop_loss=0.0,
                tp1=0.0,
                tp2=0.0,
                confidence=0.0,
                reason="market_structure_invalid_geometry",
                wait_type="WAIT_FOR_VALID_STRUCTURE",
                raw={"market_structure": structure},
            )

        plan = StrategyPlan(
            strategy_id=new_id(id_prefix),
            action=action,
            symbol=item.symbol,
            side=side,
            entry_zone_low=round(low, 8),
            entry_zone_high=round(high, 8),
            ideal_entry_price=round(entry, 8),
            stop_loss=round(stop, 8),
            tp1=round(tp1, 8),
            tp2=round(tp2, 8),
            confidence=float(structure.get("confidence") or item.score or 0.0),
            reason=(
                f"{structure.get('regime')}/{structure.get('phase')}/"
                f"{structure.get('setup')}: invalid={structure.get('invalidation') or 'structure_break'}"
            ),
            raw={"market_structure": structure, **({"paper_probe": True} if paper_probe else {})},
        )
        return attach_contract(plan, build_rule_contract(item, plan, paper_probe=paper_probe))

    def _valid_geometry(self, side: str, low: float, high: float, entry: float, stop: float, tp1: float, tp2: float) -> bool:
        if min(low, high, entry, stop, tp1, tp2) <= 0:
            return False
        if low > high or not (low <= entry <= high):
            return False
        if side == "LONG":
            return stop < low <= entry < tp1 < tp2
        return tp2 < tp1 < entry <= high < stop


rule_strategy_generator = RuleStrategyGenerator()
