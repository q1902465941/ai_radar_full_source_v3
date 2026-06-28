from __future__ import annotations

from typing import Any

from backend.config import settings
from backend.models import RadarItem


class MarketClassifier:
    def classify(self, item: RadarItem) -> dict[str, Any]:
        side = str(item.direction or "NEUTRAL").upper()
        price = max(0.0, float(item.price or 0.0))
        reasons: list[str] = []
        confirms = self._direction_confirmations(item)
        timeframe = self._timeframe_alignment(item)
        flow = self._flow_alignment(item)
        metrics = self._structure_metrics(item)
        atr_pct = max(0.20, float(item.atr_pct or 0.0))
        wick = self._current_wick_ratio(item, metrics)
        fake = str(item.fake_breakout_risk or "")
        short_term_anomaly = self._short_term_anomaly(item)
        evidence = self._evidence(item, confirms, timeframe, flow, metrics)

        if price <= 0 or side not in {"LONG", "SHORT"}:
            return self._wait(item, "range_or_chop", "observation", "NEUTRAL", ["direction_neutral_or_price_invalid"], evidence=evidence)

        if fake == "HIGH" or wick >= 0.88:
            if fake == "HIGH":
                reasons.append("fake_breakout_high")
            if wick >= 0.88:
                reasons.append("wick_noise_extreme")
            return self._wait(item, "fake_breakout", "invalid", side, reasons, evidence=evidence)

        overheated = self._overheated(item, atr_pct, metrics, side)
        if overheated:
            return self._wait(item, "exhaustion", "overheated", side, [overheated], evidence=evidence)

        regime = "range_or_chop"
        setup = "wait_for_edge"
        if timeframe and flow and confirms >= 5 and item.fund_confirm_count >= 3:
            regime = "trend_continuation"
            setup = "pullback_continuation"
        elif abs(float(item.change_5m or 0.0)) >= 0.35 and float(item.volume_spike or 0.0) >= 2.0 and item.fund_confirm_count >= 2:
            regime = "breakout"
            setup = "breakout_retest"
        elif self._higher_timeframe_bias(item) and confirms >= 3:
            regime = "pullback"
            setup = "pullback_confirmation"

        if fake != "LOW":
            reasons.append("fake_breakout_not_low")
        if not short_term_anomaly:
            reasons.append("short_term_anomaly_absent")
        if wick > self._max_actionable_wick(fake):
            reasons.append("wick_too_high")
        if item.fund_confirm_count < 3:
            reasons.append("fund_confirm_below_3")
        if confirms < 4:
            reasons.append("direction_confirmations_low")
        if regime == "range_or_chop":
            reasons.append("no_trade_structure")

        action = "OPEN_LONG" if side == "LONG" else "OPEN_SHORT"
        phase = "actionable" if not reasons else ("confirming" if confirms >= 3 and item.fund_confirm_count >= 2 else "building")
        if reasons:
            return self._wait(item, regime, phase, side, reasons, setup=setup, reference_geometry=regime != "range_or_chop", evidence=evidence)

        geometry = self._geometry(side, price, atr_pct, setup)
        risk_reward = self._risk_reward(side, geometry["ideal_entry_price"], geometry["stop_loss"], geometry["tp2"])
        confidence = self._confidence(item, confirms, wick)
        return {
            "regime": regime,
            "phase": phase,
            "bias": side,
            "setup": setup,
            "action": action,
            "entry_zone_low": geometry["entry_zone_low"],
            "entry_zone_high": geometry["entry_zone_high"],
            "ideal_entry_price": geometry["ideal_entry_price"],
            "stop_loss": geometry["stop_loss"],
            "tp1": geometry["tp1"],
            "tp2": geometry["tp2"],
            "risk_reward_r": risk_reward,
            "confidence": confidence,
            "invalidation": "structure_low_break" if side == "LONG" else "structure_high_break",
            "no_trade_reasons": [],
            "evidence": evidence,
        }

    def classify_probe(self, item: RadarItem, base: dict[str, Any] | None = None) -> dict[str, Any]:
        """Build a paper-probe structure when live-grade entry is not ready yet."""
        structure = base if isinstance(base, dict) and base else self.classify(item)
        if structure.get("action") in {"OPEN_LONG", "OPEN_SHORT"}:
            return structure

        side = str(item.direction or "NEUTRAL").upper()
        price = max(0.0, float(item.price or 0.0))
        reasons = {str(reason) for reason in structure.get("no_trade_reasons") or []}
        hard_reasons = {
            "direction_neutral_or_price_invalid",
            "fake_breakout_high",
            "wick_noise_extreme",
            "chase_displacement_high",
            "short_term_move_overextended",
        }
        if price <= 0 or side not in {"LONG", "SHORT"} or reasons & hard_reasons:
            return structure
        if structure.get("regime") in {"fake_breakout", "exhaustion"}:
            return structure
        if float(item.score or 0.0) < float(settings.paper_probe_min_score_floor):
            return structure

        confirms = self._direction_confirmations(item)
        min_confirms = max(1, int(settings.paper_probe_min_direction_confirmations or 1))
        min_fund = max(0, int(settings.paper_probe_min_fund_confirm or 0))
        wick_budget = max(0.0, float(settings.paper_probe_max_wick_ratio or 0.55))
        metrics = self._structure_metrics(item)
        if confirms < min_confirms or int(item.fund_confirm_count or 0) < min_fund:
            return structure
        if self._current_wick_ratio(item, metrics) > wick_budget:
            return structure

        atr_pct = max(0.20, float(item.atr_pct or 0.0))
        setup = "paper_probe_structure"
        geometry = self._geometry(side, price, atr_pct, setup)
        risk_reward = self._risk_reward(side, geometry["ideal_entry_price"], geometry["stop_loss"], geometry["tp2"])
        confidence = round(max(45.0, min(68.0, float(item.score or 0.0) + int(item.fund_confirm_count or 0) * 8.0)), 2)
        timeframe = self._timeframe_alignment(item)
        flow = self._flow_alignment(item)
        return {
            "regime": structure.get("regime") or "probe_sampling",
            "phase": "building",
            "bias": side,
            "setup": setup,
            "action": "OPEN_LONG" if side == "LONG" else "OPEN_SHORT",
            "entry_zone_low": geometry["entry_zone_low"],
            "entry_zone_high": geometry["entry_zone_high"],
            "ideal_entry_price": geometry["ideal_entry_price"],
            "stop_loss": geometry["stop_loss"],
            "tp1": geometry["tp1"],
            "tp2": geometry["tp2"],
            "risk_reward_r": risk_reward,
            "confidence": confidence,
            "invalidation": "probe_structure_low_break" if side == "LONG" else "probe_structure_high_break",
            "no_trade_reasons": [],
            "evidence": self._evidence(item, confirms, timeframe, flow, metrics)
            + [f"probe_relaxed_from={','.join(sorted(reasons))}" if reasons else "probe_relaxed_from=strict_wait"],
        }

    def _wait(
        self,
        item: RadarItem,
        regime: str,
        phase: str,
        bias: str,
        reasons: list[str],
        *,
        setup: str = "no_trade",
        reference_geometry: bool = False,
        evidence: list[str] | None = None,
    ) -> dict[str, Any]:
        price = float(item.price or 0.0)
        geometry = {
            "entry_zone_low": 0.0,
            "entry_zone_high": 0.0,
            "ideal_entry_price": price,
            "stop_loss": 0.0,
            "tp1": 0.0,
            "tp2": 0.0,
        }
        risk_reward = 0.0
        invalidation = ""
        if reference_geometry and price > 0 and bias in {"LONG", "SHORT"}:
            geometry = self._geometry(bias, price, max(0.20, float(item.atr_pct or 0.0)), setup)
            risk_reward = self._risk_reward(bias, geometry["ideal_entry_price"], geometry["stop_loss"], geometry["tp2"])
            invalidation = "reference_low_break" if bias == "LONG" else "reference_high_break"
        return {
            "regime": regime,
            "phase": phase,
            "bias": bias,
            "setup": setup,
            "action": "WAIT",
            "entry_zone_low": geometry["entry_zone_low"],
            "entry_zone_high": geometry["entry_zone_high"],
            "ideal_entry_price": geometry["ideal_entry_price"],
            "stop_loss": geometry["stop_loss"],
            "tp1": geometry["tp1"],
            "tp2": geometry["tp2"],
            "risk_reward_r": risk_reward,
            "confidence": max(0.0, min(100.0, float(item.score or 0.0) * 0.5)),
            "invalidation": invalidation,
            "no_trade_reasons": reasons,
            "evidence": evidence or [],
        }

    def _geometry(self, side: str, price: float, atr_pct: float, setup: str) -> dict[str, float]:
        atr = price * atr_pct / 100.0
        if setup == "breakout_retest":
            entry_pullback = 0.22
            entry_extension = 0.12
            stop_mult = 1.20
        else:
            entry_pullback = 0.35
            entry_extension = 0.10
            stop_mult = 1.15

        low_buffer = max(price * 0.002, atr * entry_pullback)
        high_buffer = max(price * 0.001, atr * entry_extension)
        stop_distance = max(price * 0.006, atr * stop_mult)
        entry = price
        if side == "LONG":
            stop = entry - stop_distance
            tp1 = entry + max(stop_distance * 1.0, price * 0.006)
            tp2 = entry + max(stop_distance * 2.2, price * 0.012)
            return {
                "entry_zone_low": round(entry - low_buffer, 8),
                "entry_zone_high": round(entry + high_buffer, 8),
                "ideal_entry_price": round(entry, 8),
                "stop_loss": round(stop, 8),
                "tp1": round(tp1, 8),
                "tp2": round(tp2, 8),
            }
        stop = entry + stop_distance
        tp1 = entry - max(stop_distance * 1.0, price * 0.006)
        tp2 = entry - max(stop_distance * 2.2, price * 0.012)
        return {
            "entry_zone_low": round(entry - high_buffer, 8),
            "entry_zone_high": round(entry + low_buffer, 8),
            "ideal_entry_price": round(entry, 8),
            "stop_loss": round(stop, 8),
            "tp1": round(tp1, 8),
            "tp2": round(tp2, 8),
        }

    def _risk_reward(self, side: str, entry: float, stop: float, target: float) -> float:
        risk = abs(entry - stop)
        if risk <= 0:
            return 0.0
        reward = (target - entry) if side == "LONG" else (entry - target)
        return round(max(0.0, reward / risk), 4)

    def _direction_confirmations(self, item: RadarItem) -> int:
        side = str(item.direction or "")
        confirmations = 0
        if side == "LONG":
            confirmations += int(float(item.change_5m or 0.0) > 0)
            confirmations += int(float(item.change_15m or 0.0) > 0)
            confirmations += int(float(item.change_1h or 0.0) >= 0)
            confirmations += int(float(item.taker_buy_ratio or 0.0) > 0.52)
            confirmations += int(float(item.depth_imbalance or 0.0) > 0)
            confirmations += int(float(item.sm_delta or 0.0) > 0)
        elif side == "SHORT":
            confirmations += int(float(item.change_5m or 0.0) < 0)
            confirmations += int(float(item.change_15m or 0.0) < 0)
            confirmations += int(float(item.change_1h or 0.0) <= 0)
            confirmations += int(float(item.taker_sell_ratio or 0.0) > 0.52)
            confirmations += int(float(item.depth_imbalance or 0.0) < 0)
            confirmations += int(float(item.sm_delta or 0.0) < 0)
        return confirmations

    def _timeframe_alignment(self, item: RadarItem) -> bool:
        if item.direction == "LONG":
            return item.change_5m > 0 and item.change_15m > 0 and item.change_1h >= 0
        if item.direction == "SHORT":
            return item.change_5m < 0 and item.change_15m < 0 and item.change_1h <= 0
        return False

    def _higher_timeframe_bias(self, item: RadarItem) -> bool:
        if item.direction == "LONG":
            return item.change_15m > 0 and item.change_1h >= 0 and item.change_5m <= item.change_15m
        if item.direction == "SHORT":
            return item.change_15m < 0 and item.change_1h <= 0 and item.change_5m >= item.change_15m
        return False

    def _flow_alignment(self, item: RadarItem) -> bool:
        if item.direction == "LONG":
            return item.taker_buy_ratio > 0.52 and item.depth_imbalance > 0 and item.sm_delta > 0
        if item.direction == "SHORT":
            return item.taker_sell_ratio > 0.52 and item.depth_imbalance < 0 and item.sm_delta < 0
        return False

    def _overheated(self, item: RadarItem, atr_pct: float, metrics: dict[str, Any], side: str) -> str:
        move = abs(float(item.change_5m or 0.0))
        if move > max(2.8, atr_pct * 2.6):
            return "chase_displacement_high" if self._chasing_range_extreme(side, metrics) else ""
        if abs(float(item.change_15m or 0.0)) > max(6.0, atr_pct * 5.0):
            return "short_term_move_overextended" if self._chasing_range_extreme(side, metrics) else ""
        return ""

    def _max_actionable_wick(self, fake: str) -> float:
        return 0.55 if fake == "LOW" else 0.68

    def _confidence(self, item: RadarItem, confirms: int, wick: float) -> float:
        raw = float(item.score or 0.0) + confirms * 2.5 + float(item.fund_confirm_count or 0) * 2.0 - max(0.0, wick - 0.45) * 35.0
        return round(max(0.0, min(95.0, raw)), 2)

    def _evidence(self, item: RadarItem, confirms: int, timeframe: bool, flow: bool, metrics: dict[str, Any] | None = None) -> list[str]:
        evidence = [
            f"direction_confirmations={confirms}",
            f"fund_confirm={item.fund_confirm_count}/{item.fund_confirm_total}",
            f"timeframe_aligned={str(timeframe).lower()}",
            f"flow_aligned={str(flow).lower()}",
            f"atr_pct={float(item.atr_pct or 0.0):.4f}",
        ]
        metrics = metrics or {}
        for key in (
            "current_wick_ratio",
            "max_wick_ratio_14",
            "range_position",
            "distance_to_support_pct",
            "distance_to_resistance_pct",
            "breakout_up",
            "breakout_down",
            "prev_high_20",
            "prev_low_20",
        ):
            if key in metrics:
                evidence.append(f"{key}={metrics.get(key)}")
        universal = (item.score_features or {}).get("universal_anomaly_model") if isinstance(item.score_features, dict) else {}
        if isinstance(universal, dict):
            probabilities = universal.get("probabilities") if isinstance(universal.get("probabilities"), dict) else {}
            evidence.append(
                "universal_direction="
                f"{universal.get('direction', 'NEUTRAL')}"
                f",p_long={probabilities.get('LONG', 0)}"
                f",p_short={probabilities.get('SHORT', 0)}"
                f",p_neutral={probabilities.get('NEUTRAL', 0)}"
            )
        features = item.score_features if isinstance(item.score_features, dict) else {}
        if "short_term_anomaly" in features:
            evidence.append(f"short_term_anomaly={str(bool(features.get('short_term_anomaly'))).lower()}")
        return evidence

    def _structure_metrics(self, item: RadarItem) -> dict[str, Any]:
        features = item.score_features if isinstance(item.score_features, dict) else {}
        metrics = features.get("structure_metrics") if isinstance(features, dict) else {}
        return metrics if isinstance(metrics, dict) else {}

    def _current_wick_ratio(self, item: RadarItem, metrics: dict[str, Any] | None = None) -> float:
        metrics = metrics if isinstance(metrics, dict) else self._structure_metrics(item)
        if "current_wick_ratio" in metrics:
            try:
                return max(0.0, float(metrics.get("current_wick_ratio") or 0.0))
            except (TypeError, ValueError):
                pass
        return max(0.0, float(item.wick_ratio or 0.0))

    def _short_term_anomaly(self, item: RadarItem) -> bool:
        features = item.score_features if isinstance(item.score_features, dict) else {}
        if "short_term_anomaly" not in features:
            return True
        return bool(features.get("short_term_anomaly"))

    def _chasing_range_extreme(self, side: str, metrics: dict[str, Any]) -> bool:
        if not metrics:
            return True
        position = self._optional_float(metrics.get("range_position"))
        if position is None:
            return True
        side = str(side or "").upper()
        if side == "LONG":
            return position >= 0.82 or bool(metrics.get("breakout_up"))
        if side == "SHORT":
            return position <= 0.18 or bool(metrics.get("breakout_down"))
        return True

    def _optional_float(self, value: Any) -> float | None:
        try:
            return float(value)
        except (TypeError, ValueError):
            return None


market_classifier = MarketClassifier()
