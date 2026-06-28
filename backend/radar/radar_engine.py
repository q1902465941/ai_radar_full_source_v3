from __future__ import annotations

import asyncio
import time
import uuid

from backend.config import settings
from backend.learning.learning_data_audit import learning_data_audit
from backend.learning.market_side_guard import market_side_block_active, market_side_block_reason, market_side_report_fresh
from backend.models import now_ms
from backend.learning.radar_weight_calibrator import radar_weight_calibrator
from backend.market.binance_factor_source import binance_factor_source
from backend.market.market_service import market_service
from backend.models import RadarItem
from backend.radar.candidate_feature_enhancer import candidate_feature_enhancer
from backend.radar.dealer_radar import dealer_label
from backend.radar.fake_breakout import fake_breakout
from backend.radar.fund_confirm import fund_confirm, fund_confirm_components
from backend.radar.heat_tracker import heat_tracker
from backend.radar.market_classifier import market_classifier
from backend.radar.active_coins import active_coin_registry
from backend.radar.universal_anomaly_model import universal_anomaly_model
from backend.market.dynamic_symbol_stream import dynamic_symbol_stream
from backend.radar.score_engine import direction, score_engine
from backend.radar.smart_money import smart_money
from backend.radar.trigger_detector import stage_for, trigger_mode
from backend.storage.db import db


class RadarEngine:
    def __init__(self):
        self.top50: list[RadarItem] = []
        self.top4: list[RadarItem] = []
        self.trade_top5: list[RadarItem] = []
        self.last_scan_id = ""
        self.last_scan_time = "--:--:--"
        self.market_heat = 0
        self.alert_count = 0
        self._scan_lock = asyncio.Lock()
        self.last_scan_duration_seconds = 0.0
        self.last_scan_error = ""
        self.last_scan_started_monotonic = 0.0

    async def scan(self, force_refresh: bool = False) -> list[RadarItem]:
        async with self._scan_lock:
            started = time.monotonic()
            self.last_scan_started_monotonic = started
            self.last_scan_error = ""
            try:
                return await self._scan_locked(force_refresh=force_refresh)
            except asyncio.CancelledError:
                self.last_scan_error = "CancelledError:scan_cancelled"
                raise
            except Exception as exc:
                self.last_scan_error = f"{type(exc).__name__}:{exc}"
                raise
            finally:
                self.last_scan_duration_seconds = round(time.monotonic() - started, 3)
                self.last_scan_started_monotonic = 0.0

    def scan_in_progress(self) -> bool:
        return self._scan_lock.locked()

    def scan_status(self, *, compact: bool = False) -> dict:
        started = float(self.last_scan_started_monotonic or 0.0)
        return {
            "in_progress": self.scan_in_progress(),
            "running_seconds": round(max(0.0, time.monotonic() - started), 3) if started else 0.0,
            "last_duration_seconds": self.last_scan_duration_seconds,
            "last_error": self.last_scan_error,
            "last_scan_id": self.last_scan_id,
            "last_scan_time": self.last_scan_time,
            "top50_count": len(self.top50),
            "market_refresh": {
                "source": binance_factor_source.last_refresh_source,
                "degraded": bool(binance_factor_source.last_refresh_degraded),
                "error": binance_factor_source.last_refresh_error,
                "symbol_count": binance_factor_source.last_symbol_count,
                "snapshot_count": binance_factor_source.last_snapshot_count,
                "effective_concurrency": binance_factor_source.last_effective_concurrency,
                "timings": dict(binance_factor_source.last_refresh_timings or {}),
                "failed_symbols": list(binance_factor_source.last_failed_symbols or [])[:8],
                "refresh_in_progress": bool(binance_factor_source.refresh_in_progress),
                "current": {
                    "source": binance_factor_source.current_refresh_source,
                    "degraded": bool(binance_factor_source.current_refresh_degraded),
                    "error": binance_factor_source.current_refresh_error,
                    "symbol_count": binance_factor_source.current_symbol_count,
                    "snapshot_count": binance_factor_source.current_snapshot_count,
                    "effective_concurrency": binance_factor_source.current_effective_concurrency,
                    "timings": dict(binance_factor_source.current_refresh_timings or {}),
                },
            },
            "active_coins": self._active_coins_status(compact=compact),
            "dynamic_stream": self._dynamic_stream_status(compact=compact),
        }

    def _active_coins_status(self, *, compact: bool = False) -> dict:
        if not compact:
            return active_coin_registry.diagnostics()
        return {
            "active_count": len(active_coin_registry.active_symbols()),
            "active_symbols": active_coin_registry.active_symbols()[:200],
            "recent_removed": list(getattr(active_coin_registry, "_recent_removed", []) or [])[:20],
        }

    def _dynamic_stream_status(self, *, compact: bool = False) -> dict:
        if not compact:
            return dynamic_symbol_stream.diagnostics()
        return {
            "active_count": len(dynamic_symbol_stream.active_symbols()),
            "active_symbols": dynamic_symbol_stream.active_symbols()[:200],
            "running": bool(getattr(dynamic_symbol_stream, "_task", None) and not dynamic_symbol_stream._task.done()),
            "last_error": str(getattr(dynamic_symbol_stream, "_last_error", "") or ""),
        }

    async def _scan_locked(self, force_refresh: bool = False) -> list[RadarItem]:
        snapshots = await market_service.get_snapshots(force_refresh=force_refresh)
        if settings.radar_exclude_major_symbols_from_anomaly:
            major_symbols = self._major_symbols()
            snapshots = [snapshot for snapshot in snapshots if snapshot.symbol not in major_symbols]
        weight_report = radar_weight_calibrator.report()
        score_weights = weight_report.get("effective_weights") or {}
        score_calibration = radar_weight_calibrator.compact_context(weight_report)
        raw_items: list[tuple[RadarItem, list[float]]] = []
        for idx, snapshot in enumerate(snapshots, start=1):
            short_term_anomaly = self._is_short_term_anomaly(snapshot)
            sm_pos, sm_delta = smart_money.estimate(snapshot)
            prev_hist = heat_tracker.pre_history(snapshot.symbol)
            hscore = 0 if len(prev_hist) < 2 else max(0, min(100, (prev_hist[-1] - prev_hist[0]) * 2))
            dirn = direction(snapshot)
            fake, fake_score = fake_breakout(snapshot, dirn)
            features = score_engine.feature_scores(snapshot, sm_pos, hscore, fake_score)
            anomaly_score = score_engine.total(features, weights=score_weights)
            fund_count, fund_total = fund_confirm(snapshot, dirn)
            fund_components = fund_confirm_components(snapshot, dirn)
            dealer = dealer_label(snapshot, dirn, sm_delta, fund_count, fake)
            score_features = {
                **features,
                "structure_metrics": getattr(snapshot, "structure_metrics", {}) or {},
                "short_term_anomaly": short_term_anomaly,
                "scan_candidate_policy": "active_pool_classify_not_drop",
                "short_term_anomaly_thresholds": {
                    "change_5m": float(settings.radar_anomaly_min_change_5m or 0.0),
                    "change_15m": float(settings.radar_anomaly_min_change_15m or 0.0),
                    "change_1h": float(settings.radar_anomaly_min_change_1h or 0.0),
                },
            }
            item = RadarItem(
                rank=0,
                symbol=snapshot.symbol,
                base_asset=snapshot.symbol.replace("USDT", ""),
                price=snapshot.price,
                direction=dirn,
                stage="观察",
                trigger_mode="异动",
                score=0.0,
                score_history=[],
                rank_history=[],
                heat_slope=0.0,
                slope_score=0.0,
                fake_breakout_risk=fake,
                change_5m=snapshot.change_5m,
                change_15m=snapshot.change_15m,
                change_1h=snapshot.change_1h,
                oi_change=snapshot.oi_change,
                fund_confirm_count=fund_count,
                fund_confirm_total=fund_total,
                dealer_radar=dealer,
                sm_position=sm_pos,
                sm_delta=sm_delta,
                volume_spike=snapshot.volume_spike,
                funding_rate=snapshot.funding_rate,
                taker_buy_ratio=snapshot.taker_buy_ratio,
                taker_sell_ratio=snapshot.taker_sell_ratio,
                depth_imbalance=snapshot.depth_imbalance,
                atr_pct=snapshot.atr_pct,
                wick_ratio=snapshot.wick_ratio,
                ai_candidate=False,
                score_features=score_features,
                score_explain={},
            )
            score_features["universal_anomaly_model"] = universal_anomaly_model.predict(item)
            quality_score, quality_explain = self._scan_quality_score(item, anomaly_score, fund_components, score_weights, score_calibration)
            item.score = quality_score
            item.score_features = {
                **score_features,
                "anomaly_score": anomaly_score,
                "trade_quality_score": quality_score,
                "fund_confirm_components": fund_components,
                "rank_model": "production_trade_quality_v2",
            }
            item.score_explain = quality_explain
            raw_items.append((item, prev_hist))
            await asyncio.sleep(0)

        raw_items.sort(key=lambda row: self._scan_rank_key(row[0]), reverse=True)
        items = []
        for rank, (item, prev_hist) in enumerate(raw_items[:50], start=1):
            item.rank = rank
            heat_tracker.update(item.symbol, item.score, rank, item.oi_change, item.sm_position)
            histories = heat_tracker.histories(item.symbol)
            heat_slope, slope_score = heat_tracker.slope(item.symbol)
            item.score_history = histories["score_history"]
            item.rank_history = histories["rank_history"]
            item.heat_slope = heat_slope
            item.slope_score = slope_score
            item.trigger_mode = trigger_mode(item.score, slope_score, item.volume_spike, item.oi_change, item.fake_breakout_risk)
            item.stage = stage_for(prev_hist, item.score, item.fund_confirm_count, item.trigger_mode)
            item.market_structure = market_classifier.classify(item)
            item.score_features = {**(item.score_features or {}), "market_structure": item.market_structure}
            items.append(item)
            await asyncio.sleep(0)

        self.top50 = items
        self.trade_top5 = self.select_confirmed_top5(items)
        self.top4 = self.trade_top5
        trade_symbols = {x.symbol for x in self.trade_top5[:5]}
        for item in self.top50:
            item.ai_candidate = item.symbol in trade_symbols
        self.last_scan_id = uuid.uuid4().hex[:12]
        self.last_scan_time = time.strftime("%H:%M:%S")
        self.market_heat = round(sum(i.score for i in self.top50[:20]) / 20 if self.top50 else 0)
        self.alert_count = len(self.trade_top5)
        await asyncio.to_thread(db.save_radar_items, self.last_scan_id, [i.asdict() for i in self.top50])
        return self.top50

    def _scan_quality_score(
        self,
        item: RadarItem,
        anomaly_score: float,
        fund_components: dict[str, bool],
        score_weights: dict,
        score_calibration: dict,
    ) -> tuple[float, dict]:
        confirms = self._direction_confirmations(item)
        fund_count = sum(1 for ok in fund_components.values() if ok)
        timeframe_aligned = self._timeframe_fully_aligned(item)
        flow_aligned = self._flow_aligned(item)
        fake_adjust = {"LOW": 10.0, "MEDIUM": -8.0, "HIGH": -36.0}.get(item.fake_breakout_risk, -12.0)
        current_wick = self._current_wick_ratio(item)
        wick_penalty = max(0.0, current_wick - 0.62) * 55.0
        trap_penalty = 16.0 if "trap" in str(item.dealer_radar or "").lower() else 0.0
        chase_penalty = self._chase_penalty(item)
        funding_penalty = max(0.0, abs(float(item.funding_rate or 0.0)) - 0.0015) * 2500.0
        universal_bonus = self._universal_model_bonus(item)
        components = {
            "anomaly": min(22.0, max(0.0, float(anomaly_score or 0.0)) * 0.30),
            "fund_confirmation": fund_count * 9.0,
            "direction_confirmation": confirms * 5.5,
            "timeframe_alignment": 8.0 if timeframe_aligned else -8.0,
            "flow_alignment": 7.0 if flow_aligned else -4.0,
            "universal_anomaly": universal_bonus,
            "fake_breakout": fake_adjust,
            "wick_noise": -wick_penalty,
            "dealer_trap": -trap_penalty,
            "chase_risk": -chase_penalty,
            "funding_extreme": -funding_penalty,
        }
        total = 12.0 + sum(components.values())
        quality_score = round(max(0.0, min(100.0, total)), 2)
        positives = [
            {"name": key, "contribution": round(value, 4), "role": "positive"}
            for key, value in sorted(components.items(), key=lambda row: row[1], reverse=True)
            if value > 0
        ][:4]
        penalties = [
            {"name": key, "contribution": round(value, 4), "role": "penalty"}
            for key, value in sorted(components.items(), key=lambda row: row[1])
            if value < 0
        ][:4]
        anomaly_explain = score_engine.explain(
            {
                key: value
                for key, value in item.score_features.items()
                if key in {"trend_score", "volume_score", "volatility_score", "oi_score", "taker_score", "timeframe_score", "sm_score", "heat_score", "fake_penalty"}
            },
            weights=score_weights,
            calibration=score_calibration,
        )
        return quality_score, {
            "score": quality_score,
            "score_model": "production_trade_quality_v2",
            "anomaly_score": anomaly_score,
            "components": {key: round(value, 4) for key, value in components.items()},
            "fund_confirm_components": fund_components,
            "direction_confirmations": confirms,
            "top_positive": positives,
            "top_penalty": penalties,
            "anomaly_explain": anomaly_explain,
            "caveat": "score is production trade-readiness, not a direct win-rate or order command",
        }

    def _universal_model_bonus(self, item: RadarItem) -> float:
        prediction = (item.score_features or {}).get("universal_anomaly_model") if isinstance(item.score_features, dict) else {}
        if not isinstance(prediction, dict):
            return 0.0
        probabilities = prediction.get("probabilities") if isinstance(prediction.get("probabilities"), dict) else {}
        side = str(item.direction or "").upper()
        model_side = str(prediction.get("direction") or "").upper()
        side_probability = self._safe_float(probabilities.get(side))
        model_probability = self._safe_float(probabilities.get(model_side))
        if side in {"LONG", "SHORT"} and model_side == side and side_probability >= 0.58:
            return min(8.0, (side_probability - 0.50) * 20.0)
        if model_side in {"LONG", "SHORT"} and side in {"LONG", "SHORT"} and model_side != side and model_probability >= 0.55:
            return -8.0
        return 0.0

    def _scan_rank_key(self, item: RadarItem) -> tuple:
        tradeable_side = item.direction in {"LONG", "SHORT"}
        clean_risk = item.fake_breakout_risk != "HIGH" and "trap" not in str(item.dealer_radar or "").lower()
        tier = 0
        if tradeable_side and clean_risk and item.fund_confirm_count >= 3 and self._direction_confirmations(item) >= 4:
            tier = 3
        elif tradeable_side and clean_risk and item.fund_confirm_count >= 2:
            tier = 2
        elif tradeable_side and clean_risk:
            tier = 1
        return (
            tier,
            round(float(item.score or 0.0), 5),
            item.fund_confirm_count,
            self._direction_confirmations(item),
            -self._current_wick_ratio(item),
        )

    def select_confirmed_top5(self, items: list[RadarItem]) -> list[RadarItem]:
        tradeable = [
            item
            for item in items
            if not self._is_major_symbol(item.symbol)
            and item.direction in {"LONG", "SHORT"}
            and item.fake_breakout_risk != "HIGH"
            and item.fund_confirm_count >= 1
            and self._direction_confirmations(item) >= 3
            and self._current_wick_ratio(item) <= 0.90
        ]
        return sorted(tradeable, key=self._trade_top5_rank, reverse=True)[:5]

    def select_ai_candidates(self, items: list[RadarItem]) -> list[RadarItem]:
        scored: list[tuple[tuple, RadarItem]] = []
        for item in items:
            if self._is_major_symbol(item.symbol):
                continue
            ok, feature, reasons = self._production_candidate_check(item)
            if not ok:
                continue
            scored.append((self._production_candidate_rank(item, feature), item))
        scored.sort(key=lambda row: row[0], reverse=True)
        return [item for _, item in scored]

    def select_ai_review_candidates(self, items: list[RadarItem]) -> list[RadarItem]:
        scored: list[tuple[tuple, RadarItem]] = []
        for item in items:
            if self._is_major_symbol(item.symbol):
                continue
            ok, feature, reasons = self._production_review_candidate_check(item)
            if not ok:
                continue
            scored.append((self._production_candidate_rank(item, feature), item))
        scored.sort(key=lambda row: row[0], reverse=True)
        return [item for _, item in scored]

    def production_candidate_diagnostics(self, items: list[RadarItem], limit: int = 20) -> dict:
        rows = []
        passed = []
        review_passed = []
        rejection_counts: dict[str, int] = {}
        for item in items[: max(1, limit)]:
            ok, feature, reasons = self._production_candidate_check(item)
            review_ok, _, review_reasons = self._production_review_candidate_check(item)
            if self._is_major_symbol(item.symbol):
                ok = False
                review_ok = False
                if "major_symbol_context_only" not in reasons:
                    reasons = ["major_symbol_context_only", *reasons]
                if "major_symbol_context_only" not in review_reasons:
                    review_reasons = ["major_symbol_context_only", *review_reasons]
            for reason in reasons:
                rejection_counts[reason] = rejection_counts.get(reason, 0) + 1
            row = {
                "symbol": item.symbol,
                "side": item.direction,
                "rank": item.rank,
                "score": item.score,
                "score_explain": {
                    "top_positive": (item.score_explain or {}).get("top_positive", [])[:3],
                    "top_penalty": (item.score_explain or {}).get("top_penalty", [])[:3],
                    "caveat": (item.score_explain or {}).get("caveat", ""),
                },
                "fund_confirm": f"{item.fund_confirm_count}/{item.fund_confirm_total}",
                "direction_confirmations": self._direction_confirmations(item),
                "fake_breakout_risk": item.fake_breakout_risk,
                "wick_ratio": item.wick_ratio,
                "current_wick_ratio": self._current_wick_ratio(item),
                "cyqnt": {
                    "feature_score": feature.feature_score,
                    "estimated_win_rate": feature.estimated_win_rate,
                    "selection_score": feature.selection_score,
                    "reasons": feature.reasons,
                },
                "ok": ok,
                "failed": reasons,
                "review_ok": review_ok,
                "review_failed": review_reasons,
            }
            rows.append(row)
            if ok:
                passed.append(row)
            if review_ok:
                review_passed.append(row)
        return {
            "passed_count": len(self.select_ai_candidates(items)),
            "review_count": len(self.select_ai_review_candidates(items)),
            "rejection_counts": rejection_counts,
            "top_checked": rows,
            "passed_top_checked": passed,
            "review_top_checked": review_passed,
            "policy": {
                "major_symbols_context_only": settings.radar_exclude_major_symbols_from_anomaly,
                "major_symbols": sorted(self._major_symbols()),
                "requires_current_market_confirmations": 3,
                "requires_timeframe_full_alignment": True,
                "review_allows_high_quality_fund_confirm_2": True,
                "min_direction_confirmations": 4,
                "min_cyqnt_estimated_win_rate": self._production_min_estimated_win_rate(),
                "min_cyqnt_feature_score": 46.0,
                "review_min_cyqnt_feature_score": 54.0,
                "review_min_cyqnt_selection_score": 68.0,
                "max_wick_low_fake": self._strict_low_fake_wick_threshold(),
                "review_max_wick_low_fake": self._strict_low_fake_wick_threshold(),
                "max_wick_medium_fake": 0.68,
            },
        }

    def _production_candidate_check(self, item: RadarItem) -> tuple[bool, object, list[str]]:
        feature = candidate_feature_enhancer.evaluate(item)
        reasons: list[str] = []
        if self._is_major_symbol(item.symbol):
            reasons.append("major_symbol_context_only")
        if item.direction not in {"LONG", "SHORT"}:
            reasons.append("direction_neutral")
        if item.fake_breakout_risk == "HIGH":
            reasons.append("fake_breakout_high")
        elif item.fake_breakout_risk != "LOW":
            reasons.append("fake_breakout_not_low")
        if not self._full_fund_confirm(item):
            reasons.append("fund_confirm_below_3")
        confirms = self._direction_confirmations(item)
        if confirms < 4:
            reasons.append("direction_confirmations_low")
        if not self._timeframe_fully_aligned(item):
            reasons.append("timeframe_not_fully_aligned")
        if "trap" in str(item.dealer_radar or "").lower():
            reasons.append("dealer_trap")
        if self._recent_market_side_block(item):
            reasons.append("market_backtest_side_disallowed")
        if self._candidate_risk_fraction(item) < 0.007:
            reasons.append("stop_structure_too_tight_for_recent_market")
        if item.direction == "LONG" and float(item.change_5m or 0.0) > 3.0 and self._chasing_range_extreme(item):
            reasons.append("long_chase_displacement_high")
        if item.direction == "SHORT" and float(item.change_5m or 0.0) < -3.0 and self._chasing_range_extreme(item):
            reasons.append("short_chase_displacement_high")

        min_win = self._production_min_estimated_win_rate()
        if float(feature.estimated_win_rate or 0.0) < min_win:
            reasons.append("cyqnt_win_rate_low")
        if float(feature.feature_score or 0.0) < 46.0:
            reasons.append("cyqnt_feature_score_low")

        wick = self._current_wick_ratio(item)
        if item.fake_breakout_risk == "LOW":
            if wick > self._strict_low_fake_wick_threshold():
                reasons.append("wick_too_high")
        elif wick > 0.68:
            reasons.append("wick_too_high_for_medium_fake")

        raw_score = float(item.score or 0.0)
        selection_score = float(feature.selection_score or 0.0)
        strong_enhanced = selection_score >= 66.0 and float(feature.feature_score or 0.0) >= 54.0
        strong_raw = raw_score >= 58.0
        exceptional = (
            item.fake_breakout_risk == "LOW"
            and confirms >= 5
            and selection_score >= 72.0
            and float(feature.estimated_win_rate or 0.0) >= max(min_win, 0.56)
        )
        if not (strong_raw or strong_enhanced or exceptional):
            reasons.append("production_score_low")

        return not reasons, feature, reasons

    def _production_review_candidate_check(self, item: RadarItem) -> tuple[bool, object, list[str]]:
        ok, feature, reasons = self._production_candidate_check(item)
        if ok:
            return True, feature, []

        review_reasons = [
            reason
            for reason in reasons
            if reason not in {"fund_confirm_below_3", "timeframe_not_fully_aligned", "wick_too_high"}
        ]
        min_partial_fund = min(2, int(item.fund_confirm_total or 3))
        if int(item.fund_confirm_count or 0) < min_partial_fund:
            review_reasons.append("fund_confirm_below_2")

        confirms = self._direction_confirmations(item)
        min_confirms = 4 if item.fake_breakout_risk == "LOW" else 5
        if confirms < min_confirms and "direction_confirmations_low" not in review_reasons:
            review_reasons.append("direction_confirmations_low")

        min_win = self._production_min_estimated_win_rate()
        if float(feature.estimated_win_rate or 0.0) < min_win and "cyqnt_win_rate_low" not in review_reasons:
            review_reasons.append("cyqnt_win_rate_low")
        if float(feature.feature_score or 0.0) < 54.0:
            review_reasons.append("review_cyqnt_feature_score_low")
        if float(feature.selection_score or 0.0) < 68.0:
            review_reasons.append("review_selection_score_low")

        wick = self._current_wick_ratio(item)
        if item.fake_breakout_risk == "LOW":
            if wick > self._strict_low_fake_wick_threshold() and "wick_too_high" not in review_reasons:
                review_reasons.append("wick_too_high")
        elif wick > 0.68 and "wick_too_high_for_medium_fake" not in review_reasons:
            review_reasons.append("wick_too_high_for_medium_fake")

        return not review_reasons, feature, review_reasons

    def _production_candidate_rank(self, item: RadarItem, feature) -> tuple:
        confirms = self._direction_confirmations(item)
        fake_bonus = 8.0 if item.fake_breakout_risk == "LOW" else 0.0
        wick_penalty = max(0.0, self._current_wick_ratio(item) - 0.55) * 20.0
        return (
            round(float(feature.estimated_win_rate or 0.0), 5),
            round(float(feature.selection_score or 0.0) + fake_bonus - wick_penalty, 5),
            confirms,
            round(float(feature.feature_score or 0.0), 5),
            round(float(item.score or 0.0), 5),
            -int(getattr(item, "rank", 999) or 999),
        )

    def _production_min_estimated_win_rate(self) -> float:
        gate = min(float(settings.strategy_min_live_win_rate or 0.58), float(settings.strategy_min_paper_win_rate or 0.60))
        return max(0.54, gate - 0.02)

    def _candidate_risk_fraction(self, item: RadarItem) -> float:
        atr_pct = max(0.0, float(item.atr_pct or 0.0)) / 100.0
        raw = atr_pct * max(0.1, float(settings.replay_atr_risk_mult or 0.9))
        return min(max(raw, float(settings.replay_min_risk_pct or 0.006)), float(settings.replay_max_risk_pct or 0.025))

    def _recent_market_side_block(self, item: RadarItem) -> bool:
        if item.direction not in {"LONG", "SHORT"}:
            return False
        try:
            market = (learning_data_audit.summary().get("market_backtest") or {})
            current_ms = now_ms()
            for block in market.get("side_blocks") or []:
                if str(block.get("side") or "").upper() == item.direction and market_side_block_active(block, current_ms):
                    return True
            if not market_side_report_fresh(market, current_ms):
                return False
            by_side = market.get("by_side_metrics") or {}
            metrics = by_side.get(item.direction) or {}
        except Exception:
            return False
        trades = int(float(metrics.get("trades") or 0))
        return bool(trades and market_side_block_reason(metrics))

    def _strict_low_fake_wick_threshold(self) -> float:
        return min(0.55, max(0.0, float(settings.paper_probe_max_wick_ratio or 0.55)))

    def _full_fund_confirm(self, item: RadarItem) -> bool:
        return item.fund_confirm_count >= min(3, item.fund_confirm_total)

    def _major_symbols(self) -> set[str]:
        return {
            symbol.strip().upper()
            for symbol in str(settings.radar_major_symbols or "").split(",")
            if symbol.strip()
        }

    def _is_major_symbol(self, symbol: str) -> bool:
        return bool(settings.radar_exclude_major_symbols_from_anomaly and str(symbol or "").upper() in self._major_symbols())

    def _is_short_term_anomaly(self, snapshot) -> bool:
        return (
            abs(float(getattr(snapshot, "change_5m", 0.0) or 0.0)) >= float(settings.radar_anomaly_min_change_5m or 0.0)
            or abs(float(getattr(snapshot, "change_15m", 0.0) or 0.0)) >= float(settings.radar_anomaly_min_change_15m or 0.0)
            or abs(float(getattr(snapshot, "change_1h", 0.0) or 0.0)) >= float(settings.radar_anomaly_min_change_1h or 0.0)
        )

    def _timeframe_fully_aligned(self, item: RadarItem) -> bool:
        if item.direction == "LONG":
            return item.change_5m > 0 and item.change_15m > 0 and item.change_1h >= 0
        if item.direction == "SHORT":
            return item.change_5m < 0 and item.change_15m < 0 and item.change_1h <= 0
        return False

    def _flow_aligned(self, item: RadarItem) -> bool:
        if item.direction == "LONG":
            return item.taker_buy_ratio >= 0.53 or item.depth_imbalance >= 0.06 or item.sm_delta >= 0
        if item.direction == "SHORT":
            return item.taker_sell_ratio >= 0.53 or item.depth_imbalance <= -0.06 or item.sm_delta <= 0
        return False

    def _chase_penalty(self, item: RadarItem) -> float:
        if not self._chasing_range_extreme(item):
            return 0.0
        change_5m = float(item.change_5m or 0.0)
        if item.direction == "LONG" and change_5m > 3.0:
            return min(18.0, (change_5m - 3.0) * 5.0)
        if item.direction == "SHORT" and change_5m < -3.0:
            return min(18.0, (abs(change_5m) - 3.0) * 5.0)
        return 0.0

    def _structure_metrics(self, item: RadarItem) -> dict:
        features = item.score_features if isinstance(item.score_features, dict) else {}
        metrics = features.get("structure_metrics") if isinstance(features, dict) else {}
        return metrics if isinstance(metrics, dict) else {}

    def _current_wick_ratio(self, item: RadarItem) -> float:
        metrics = self._structure_metrics(item)
        if "current_wick_ratio" in metrics:
            try:
                return max(0.0, float(metrics.get("current_wick_ratio") or 0.0))
            except (TypeError, ValueError):
                pass
        return max(0.0, float(item.wick_ratio or 0.0))

    def _chasing_range_extreme(self, item: RadarItem) -> bool:
        metrics = self._structure_metrics(item)
        if not metrics:
            return True
        try:
            position = float(metrics.get("range_position"))
        except (TypeError, ValueError):
            return True
        if item.direction == "LONG":
            return position >= 0.82 or bool(metrics.get("breakout_up"))
        if item.direction == "SHORT":
            return position <= 0.18 or bool(metrics.get("breakout_down"))
        return True

    def _safe_float(self, value, default: float = 0.0) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    def _direction_confirmations(self, item: RadarItem) -> int:
        if item.direction == "LONG":
            checks = [
                item.change_5m > 0,
                item.change_15m > 0,
                item.change_1h >= 0,
                item.taker_buy_ratio >= 0.55,
                item.depth_imbalance >= 0.08,
                item.sm_delta >= 0,
                item.volume_spike >= 1.3,
                self._current_wick_ratio(item) <= self._strict_low_fake_wick_threshold(),
            ]
        elif item.direction == "SHORT":
            checks = [
                item.change_5m < 0,
                item.change_15m < 0,
                item.change_1h <= 0,
                item.taker_sell_ratio >= 0.55,
                item.depth_imbalance <= -0.08,
                item.sm_delta <= 0,
                item.volume_spike >= 1.3,
                self._current_wick_ratio(item) <= self._strict_low_fake_wick_threshold(),
            ]
        else:
            return 0
        return sum(1 for ok in checks if ok)

    def _trade_top5_rank(self, item: RadarItem) -> tuple:
        confirms = self._direction_confirmations(item)
        full_fund = self._full_fund_confirm(item)
        partial_fund = item.fund_confirm_count >= min(2, item.fund_confirm_total)
        if full_fund and confirms >= 4:
            tier = 3
        elif partial_fund and confirms >= 4:
            tier = 2
        elif item.fund_confirm_count >= 1 and confirms >= 3:
            tier = 1
        else:
            tier = 0

        fake_bonus = {"LOW": 10.0, "MEDIUM": -6.0, "HIGH": -100.0}.get(item.fake_breakout_risk, -10.0)
        wick_penalty = max(0.0, self._current_wick_ratio(item) - self._strict_low_fake_wick_threshold()) * 45.0
        trap_penalty = 18.0 if "trap" in str(item.dealer_radar or "").lower() else 0.0
        fund_score = min(3, int(item.fund_confirm_count or 0)) * 12.0
        confirm_score = confirms * 7.0
        liquidity_score = min(20.0, max(0.0, float(item.volume_spike or 0.0)) * 4.0)
        depth_score = min(10.0, abs(float(item.depth_imbalance or 0.0)) * 40.0)
        raw_score = float(item.score or 0.0)
        trade_score = raw_score + fund_score + confirm_score + liquidity_score + depth_score + fake_bonus - wick_penalty - trap_penalty
        return (
            tier,
            round(trade_score, 4),
            raw_score,
            -int(getattr(item, "rank", 999) or 999),
        )

    def get_symbol(self, symbol: str):
        return next((x for x in self.top50 if x.symbol == symbol), None)


radar_engine = RadarEngine()
