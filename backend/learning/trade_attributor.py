from __future__ import annotations

import time
from dataclasses import asdict, dataclass
from typing import Any

from backend.config import settings
from backend.learning.learning_data_audit import learning_data_audit
from backend.learning.replay_memory import replay_memory
from backend.learning.strategy_filter import direction_confirmations
from backend.learning.trade_memory import trade_memory
from backend.models import RadarItem, StrategyPlan
from backend.trading.trade_economics import round_trip_cost_pct


EXCLUDED_REASONS = {"RESTORED_STALE_RECONCILE", "PRICE_SOURCE_STALE_RECONCILE", "ACCEPTANCE_TP2"}


@dataclass
class CausalAttributionReport:
    enabled: bool
    current_pattern: str
    current_factors: list[str]
    matched_samples: int
    match_level: str
    win_rate: float
    profit_factor: float
    pnl: float
    avg_pnl: float
    paper_ok: bool
    live_ok: bool
    reasons: list[str]
    advice: list[str]
    matched_loss_causes: list[dict[str, Any]]
    matched_profit_drivers: list[dict[str, Any]]

    def asdict(self) -> dict[str, Any]:
        return asdict(self)


class TradeAttributor:
    def __init__(self) -> None:
        self._cache_until = 0.0
        self._sample_cache: list[dict[str, Any]] = []

    def summary(self) -> dict[str, Any]:
        samples = self._samples()
        metrics = self._metrics(samples)
        loss_causes = self._factor_summary(samples, positive=False)
        profit_drivers = self._factor_summary(samples, positive=True)
        return {
            "enabled": settings.trade_attribution_enabled,
            "sample_count": metrics["count"],
            "global_win_rate": round(metrics["win_rate"], 4),
            "global_profit_factor": round(metrics["profit_factor"], 4),
            "global_pnl": round(metrics["pnl"], 4),
            "avg_pnl": round(metrics["avg_pnl"], 6),
            "main_loss_causes": loss_causes,
            "main_profit_drivers": profit_drivers,
            "blocked_symbol_sides": self._blocked_symbol_sides(samples),
            "data_quality": learning_data_audit.compact(),
            "minimums": {
                "samples": settings.trade_attribution_min_samples,
                "block_win_rate": settings.trade_attribution_block_win_rate,
                "block_profit_factor": settings.trade_attribution_block_profit_factor,
                "live_win_rate": settings.trade_attribution_live_min_win_rate,
                "live_profit_factor": settings.trade_attribution_live_min_profit_factor,
            },
            "instruction": (
                "This is causal attribution from structured trade outcomes. "
                "Use it to avoid repeating physical loss structures, not as a standalone entry signal. "
                "Check data_quality before treating it as a hard production blocker."
            ),
        }

    def deep_analysis(self, trade_limit: int = 20) -> dict[str, Any]:
        samples = self._samples()
        metrics = self._metrics(samples)
        losses = [sample for sample in samples if _f(sample.get("pnl")) < 0]
        wins = [sample for sample in samples if _f(sample.get("pnl")) > 0]
        root_causes = self._root_cause_matrix(samples)
        profit_drivers = self._driver_matrix(samples)
        close_reasons = self._close_reason_breakdown(samples)
        return {
            "enabled": settings.trade_attribution_enabled,
            "sample_count": metrics["count"],
            "win_rate": round(metrics["win_rate"], 4),
            "profit_factor": round(metrics["profit_factor"], 4),
            "pnl": round(metrics["pnl"], 4),
            "avg_pnl": round(metrics["avg_pnl"], 6),
            "loss_count": len(losses),
            "win_count": len(wins),
            "root_causes": root_causes,
            "profit_drivers": profit_drivers,
            "close_reasons": close_reasons,
            "recent_loss_trades": [self.explain_trade(sample) for sample in losses[: max(1, trade_limit)]],
            "recent_win_trades": [self.explain_trade(sample) for sample in wins[: max(1, min(trade_limit, 10))]],
            "action_rules": self._action_rules(root_causes, close_reasons),
            "instruction": (
                "Deep attribution is built from structured historical trades and replay samples. "
                "A rule should affect trading only when sample count is sufficient and net PnL/PF are negative."
            ),
        }

    def compact_context(self, item: RadarItem | None = None) -> dict[str, Any]:
        context = self.summary()
        context["main_loss_causes"] = context["main_loss_causes"][:5]
        context["main_profit_drivers"] = context["main_profit_drivers"][:5]
        context["blocked_symbol_sides"] = context["blocked_symbol_sides"][:5]
        if item is not None:
            report = self.evaluate(item, None)
            context["current_signal_attribution"] = {
                "pattern": report.current_pattern,
                "factors": report.current_factors,
                "matched_samples": report.matched_samples,
                "match_level": report.match_level,
                "win_rate": report.win_rate,
                "profit_factor": report.profit_factor,
                "pnl": report.pnl,
                "paper_ok": report.paper_ok,
                "reasons": report.reasons,
                "advice": report.advice[:3],
            }
        return context

    def evaluate(self, item: RadarItem, plan: StrategyPlan | None) -> CausalAttributionReport:
        side = plan.side if plan and plan.side in {"LONG", "SHORT"} else item.direction
        row = item.asdict()
        row["side"] = side
        current_pattern = self._pattern_key(row, side, plan)
        current_factors = self._factors(row, side, plan)

        if not settings.trade_attribution_enabled or side not in {"LONG", "SHORT"}:
            return self._empty_report(current_pattern, current_factors, "disabled")

        matched, level = self._matched_samples(row, side, plan)
        metrics = self._metrics(matched)
        min_samples = max(1, int(settings.trade_attribution_min_samples))
        enough = metrics["count"] >= min_samples
        recent_recovered = self._recent_matched_recovered(matched, min_samples)
        reasons: list[str] = []
        advice: list[str] = []

        if not enough:
            reasons.append("trade_attribution_samples_low")
            advice.append("样本不足，只作为观察，不放大仓位。")
        elif recent_recovered:
            reasons.append("recent_matched_pattern_recovered")
            advice.append("最近同结构样本已恢复，历史亏损桶只降级为复核，不阻断纸面验证。")
        else:
            if metrics["pnl"] <= 0 and metrics["win_rate"] < settings.trade_attribution_block_win_rate:
                reasons.append("causal_pattern_win_rate_low")
                advice.append("当前结构历史胜率偏低，先等待更强确认。")
            if metrics["pnl"] <= 0 and metrics["profit_factor"] < settings.trade_attribution_block_profit_factor:
                reasons.append("causal_pattern_profit_factor_low")
                advice.append("当前结构历史盈亏比为负，不进入纸面开仓。")

        factor_reasons, factor_advice = self._factor_blockers(current_factors)
        positive_pattern = self._positive_pattern(metrics, enough)
        if positive_pattern:
            if factor_reasons:
                advice.append("matched positive physical pattern overrides factor-level loss warnings for paper execution.")
        else:
            reasons.extend(factor_reasons)
            advice.extend(factor_advice)
        blocking_reasons = [r for r in reasons if r not in {"trade_attribution_samples_low", "recent_matched_pattern_recovered"}]
        live_ok = (
            enough
            and not blocking_reasons
            and metrics["win_rate"] >= settings.trade_attribution_live_min_win_rate
            and metrics["profit_factor"] >= settings.trade_attribution_live_min_profit_factor
            and metrics["pnl"] > 0
        )

        return CausalAttributionReport(
            enabled=True,
            current_pattern=current_pattern,
            current_factors=current_factors,
            matched_samples=metrics["count"],
            match_level=level,
            win_rate=round(metrics["win_rate"], 4),
            profit_factor=round(metrics["profit_factor"], 4),
            pnl=round(metrics["pnl"], 4),
            avg_pnl=round(metrics["avg_pnl"], 6),
            paper_ok=not blocking_reasons,
            live_ok=live_ok,
            reasons=reasons[:8],
            advice=list(dict.fromkeys(advice))[:6],
            matched_loss_causes=self._factor_summary(matched, positive=False)[:5],
            matched_profit_drivers=self._factor_summary(matched, positive=True)[:5],
        )

    def explain_trade(self, sample: dict[str, Any]) -> dict[str, Any]:
        row = self._normalize_sample(sample)
        pnl = _f(row.get("pnl"))
        causes = self._root_causes_for(row)
        drivers = self._profit_drivers_for(row)
        return {
            "sample_id": row.get("sample_id"),
            "symbol": row.get("symbol"),
            "side": row.get("side"),
            "pnl": round(pnl, 4),
            "roi": round(_f(row.get("roi")), 4),
            "close_reason": row.get("close_reason"),
            "entry_price": _round_or_zero(row.get("entry_price"), 8),
            "exit_price": _round_or_zero(row.get("exit_price"), 8),
            "margin": round(_f(row.get("margin")), 4),
            "notional": round(_f(row.get("notional")), 4),
            "fee": round(_f(row.get("fee")), 6),
            "gross_pnl": round(_f(row.get("gross_pnl")), 4),
            "score": round(_f(row.get("score")), 2),
            "fund_confirm_count": int(_f(row.get("fund_confirm_count"))),
            "fake_breakout_risk": row.get("fake_breakout_risk"),
            "root_causes": causes if pnl < 0 else [],
            "profit_drivers": drivers if pnl > 0 else [],
            "lesson": self._trade_lesson(row, causes, drivers),
        }

    def _samples(self) -> list[dict[str, Any]]:
        now = time.time()
        if now < self._cache_until:
            return self._sample_cache

        limit = max(100, int(settings.trade_attribution_sample_limit))
        samples: list[dict[str, Any]] = []
        if settings.trade_attribution_use_replay and settings.replay_enabled:
            samples.extend(replay_memory.samples(limit=limit))
        samples.extend(trade_memory.samples(limit=limit, require_radar=True))
        samples = [self._normalize_sample(sample) for sample in samples if self._usable_sample(sample)]
        samples = _dedupe_recent(samples, limit)
        self._sample_cache = samples[:limit]
        self._cache_until = now + max(1, int(settings.event_calibration_ttl_seconds))
        return self._sample_cache

    def _usable_sample(self, sample: dict[str, Any]) -> bool:
        side = sample.get("side") or sample.get("direction")
        if side not in {"LONG", "SHORT"}:
            return False
        if str(sample.get("close_reason") or "") in EXCLUDED_REASONS:
            return False
        return _f(sample.get("pnl")) != 0.0

    def _normalize_sample(self, sample: dict[str, Any]) -> dict[str, Any]:
        radar = sample.get("radar") if isinstance(sample.get("radar"), dict) else {}
        row = {**radar, **{k: v for k, v in sample.items() if k != "radar"}}
        side = row.get("side") or row.get("direction")
        row["side"] = side
        row["direction"] = side
        row["factors"] = self._factors(row, side, None)
        row["pattern"] = self._pattern_key(row, side, None)
        return row

    def _matched_samples(self, row: dict[str, Any], side: str, plan: StrategyPlan | None) -> tuple[list[dict[str, Any]], str]:
        samples = self._samples()
        pattern = self._pattern_key(row, side, plan)
        exact = [sample for sample in samples if sample.get("pattern") == pattern]
        min_samples = max(1, int(settings.trade_attribution_min_samples))
        if len(exact) >= min_samples:
            return exact, "exact_physical_pattern"

        current_factors = set(self._factors(row, side, plan))
        core = {
            factor
            for factor in current_factors
            if factor.startswith(("fund_", "fake_", "dirconf_", "timeframe_", "taker_", "depth_", "wick_", "volume_", "cost_"))
        }
        relaxed = []
        for sample in samples:
            if sample.get("side") != side:
                continue
            sample_factors = set(sample.get("factors") or [])
            overlap = len(core & sample_factors)
            if overlap >= max(4, min(7, len(core))):
                relaxed.append(sample)
        if len(relaxed) >= min_samples:
            return relaxed, "relaxed_physical_structure"

        side_baseline = [sample for sample in samples if sample.get("side") == side]
        return side_baseline, "side_baseline"

    def _factor_blockers(self, current_factors: list[str]) -> tuple[list[str], list[str]]:
        samples = self._samples()
        min_samples = max(1, int(settings.trade_attribution_min_samples))
        reasons: list[str] = []
        advice: list[str] = []
        for factor in current_factors:
            if not _is_loss_factor(factor):
                continue
            if factor.startswith(("side_", "score_", "fund_confirm_3", "fake_low", "dirconf_strong")):
                continue
            bucket = [sample for sample in samples if factor in set(sample.get("factors") or [])]
            metrics = self._metrics(bucket)
            if metrics["count"] < min_samples:
                continue
            if metrics["pnl"] <= 0 and metrics["win_rate"] < settings.trade_attribution_block_win_rate:
                reasons.append(f"causal_factor_negative:{factor}")
                advice.append(_advice_for(factor))
            if len(reasons) >= 3:
                break
        return reasons, advice

    def _positive_pattern(self, metrics: dict[str, Any], enough: bool) -> bool:
        return (
            enough
            and metrics["pnl"] > 0
            and metrics["win_rate"] >= settings.trade_attribution_block_win_rate
            and metrics["profit_factor"] >= settings.trade_attribution_block_profit_factor
        )

    def _recent_matched_recovered(self, samples: list[dict[str, Any]], min_samples: int) -> bool:
        recent = sorted(samples, key=lambda row: int(row.get("close_time") or row.get("ts_ms") or 0), reverse=True)[:min_samples]
        if len(recent) < min_samples:
            return False
        metrics = self._metrics(recent)
        return metrics["pnl"] > 0 and metrics["win_rate"] >= max(0.50, float(settings.trade_attribution_block_win_rate or 0.42))

    def _root_cause_matrix(self, samples: list[dict[str, Any]]) -> list[dict[str, Any]]:
        buckets: dict[str, list[dict[str, Any]]] = {}
        for sample in samples:
            if _f(sample.get("pnl")) >= 0:
                continue
            for cause in self._root_causes_for(sample):
                buckets.setdefault(cause["code"], []).append(sample)
        return self._matrix_records(buckets, positive=False)

    def _driver_matrix(self, samples: list[dict[str, Any]]) -> list[dict[str, Any]]:
        buckets: dict[str, list[dict[str, Any]]] = {}
        for sample in samples:
            if _f(sample.get("pnl")) <= 0:
                continue
            for driver in self._profit_drivers_for(sample):
                buckets.setdefault(driver["code"], []).append(sample)
        return self._matrix_records(buckets, positive=True)

    def _matrix_records(self, buckets: dict[str, list[dict[str, Any]]], *, positive: bool) -> list[dict[str, Any]]:
        records = []
        min_bucket = max(3, min(int(settings.trade_attribution_min_samples), 20))
        for code, bucket in buckets.items():
            if len(bucket) < min_bucket:
                continue
            metrics = self._metrics(bucket)
            symbol_counts: dict[str, int] = {}
            for sample in bucket:
                symbol = str(sample.get("symbol") or "")
                if symbol:
                    symbol_counts[symbol] = symbol_counts.get(symbol, 0) + 1
            symbols = sorted(symbol_counts.items(), key=lambda x: x[1], reverse=True)[:5]
            records.append(
                {
                    "code": code,
                    "label": _label_for(code),
                    "samples": metrics["count"],
                    "win_rate": round(metrics["win_rate"], 4),
                    "profit_factor": round(metrics["profit_factor"], 4),
                    "pnl": round(metrics["pnl"], 4),
                    "avg_pnl": round(metrics["avg_pnl"], 6),
                    "symbols": [{"symbol": symbol, "count": count} for symbol, count in symbols],
                    "advice": _advice_for(code),
                }
            )
        if positive:
            records.sort(key=lambda x: (x["profit_factor"], x["win_rate"], x["pnl"]), reverse=True)
        else:
            records.sort(key=lambda x: (x["pnl"], x["samples"]))
        return records[:12]

    def _close_reason_breakdown(self, samples: list[dict[str, Any]]) -> list[dict[str, Any]]:
        buckets: dict[str, list[dict[str, Any]]] = {}
        for sample in samples:
            reason = str(sample.get("close_reason") or "UNKNOWN")
            buckets.setdefault(reason, []).append(sample)
        records = []
        for reason, bucket in buckets.items():
            metrics = self._metrics(bucket)
            records.append(
                {
                    "reason": reason,
                    "label": _close_reason_label(reason),
                    "samples": metrics["count"],
                    "win_rate": round(metrics["win_rate"], 4),
                    "profit_factor": round(metrics["profit_factor"], 4),
                    "pnl": round(metrics["pnl"], 4),
                    "avg_pnl": round(metrics["avg_pnl"], 6),
                    "advice": _close_reason_advice(reason),
                }
            )
        return sorted(records, key=lambda x: x["pnl"])[:12]

    def _root_causes_for(self, sample: dict[str, Any]) -> list[dict[str, str]]:
        row = sample if sample.get("factors") else self._normalize_sample(sample)
        factors = set(row.get("factors") or [])
        causes: list[str] = []
        for factor in factors:
            if _is_loss_factor(factor) and not factor.startswith("exit_"):
                causes.append(factor)
        close_reason = str(row.get("close_reason") or "")
        if close_reason in {"SL", "REPLAY_SL"}:
            causes.append("stop_loss_hit")
        elif close_reason in {"SIGNAL_DECAY_EXIT", "ADVERSE_SIGNAL_CUT"}:
            causes.append("signal_decay")
        elif close_reason in {"ADVERSE_REVERSE_CUT", "REVERSE_SIGNAL_EXIT"}:
            causes.append("reverse_signal")
        elif close_reason in {"ADVERSE_FAKE_CUT", "FAKE_BREAKOUT_EXIT"}:
            causes.append("fake_breakout_exit")
        elif close_reason in {"MAX_HOLD_TIMEOUT", "REPLAY_TIMEOUT"}:
            causes.append("timeout_no_followthrough")

        fee = _f(row.get("fee"))
        gross = abs(_f(row.get("gross_pnl")))
        if fee > 0 and gross > 0 and fee / gross >= 0.25:
            causes.append("fee_drag_high")
        if _f(row.get("notional")) > 0 and _f(row.get("notional")) < float(settings.trade_min_notional_usdt):
            causes.append("small_notional")
        if _f(row.get("margin")) > 0 and _f(row.get("margin")) < float(settings.trade_min_margin_usdt):
            causes.append("small_margin")
        return [{"code": code, "label": _label_for(code), "advice": _advice_for(code)} for code in _unique(causes)]

    def _profit_drivers_for(self, sample: dict[str, Any]) -> list[dict[str, str]]:
        row = sample if sample.get("factors") else self._normalize_sample(sample)
        factors = set(row.get("factors") or [])
        drivers = [factor for factor in factors if _is_profit_factor(factor)]
        close_reason = str(row.get("close_reason") or "")
        if close_reason in {"TP2", "REPLAY_TP2"}:
            drivers.append("tp2_followthrough")
        if _f(row.get("gross_pnl")) > 0 and _f(row.get("fee")) > 0 and _f(row.get("gross_pnl")) > _f(row.get("fee")) * 3:
            drivers.append("profit_covers_cost")
        return [{"code": code, "label": _label_for(code), "advice": _advice_for(code)} for code in _unique(drivers)]

    def _trade_lesson(self, row: dict[str, Any], causes: list[dict[str, str]], drivers: list[dict[str, str]]) -> str:
        pnl = _f(row.get("pnl"))
        if pnl < 0 and causes:
            labels = "、".join(cause["label"] for cause in causes[:3])
            return f"亏损结构：{labels}。以后同类信号需要等待更强确认，或直接禁止进入候选。"
        if pnl > 0 and drivers:
            labels = "、".join(driver["label"] for driver in drivers[:3])
            return f"盈利结构：{labels}。同类信号可作为正样本，但仍需通过成本和回撤约束。"
        return "样本信息不足，暂不作为强规则。"

    def _action_rules(self, root_causes: list[dict[str, Any]], close_reasons: list[dict[str, Any]]) -> list[dict[str, Any]]:
        rules: list[dict[str, Any]] = []
        for cause in root_causes[:8]:
            if cause["samples"] < settings.trade_attribution_min_samples:
                continue
            severity = "BLOCK" if cause["pnl"] < 0 and cause["profit_factor"] < settings.trade_attribution_block_profit_factor else "DOWN_WEIGHT"
            rules.append(
                {
                    "severity": severity,
                    "factor": cause["code"],
                    "label": cause["label"],
                    "condition": f"{cause['label']} 且历史 PF={cause['profit_factor']:.2f}, 胜率={cause['win_rate']:.1%}",
                    "action": cause["advice"],
                }
            )
        for reason in close_reasons[:4]:
            if reason["pnl"] >= 0 or reason["samples"] < settings.trade_attribution_min_samples:
                continue
            rules.append(
                {
                    "severity": "REVIEW_EXIT",
                    "factor": reason["reason"],
                    "label": reason["label"],
                    "condition": f"平仓原因 {reason['label']} 累计 PnL={reason['pnl']:.2f}",
                    "action": reason["advice"],
                }
            )
        return rules[:10]

    def _factors(self, row: dict[str, Any], side: str, plan: StrategyPlan | None) -> list[str]:
        factors = [f"side_{side}"]
        contract = row.get("strategy_contract") if isinstance(row.get("strategy_contract"), dict) else {}
        if not contract and plan is not None and isinstance(plan.raw.get("strategy_contract"), dict):
            contract = plan.raw.get("strategy_contract") or {}
        strategy_kind = str(contract.get("strategy_kind") or "")
        if strategy_kind:
            factors.append(f"strategy_kind_{_factor_token(strategy_kind)}")
        score = _f(row.get("score"))
        factors.append(f"score_{int(score // 10 * 10)}_{int(score // 10 * 10 + 9)}")

        fund = int(_f(row.get("fund_confirm_count")))
        factors.append("fund_confirm_3" if fund >= 3 else "fund_confirm_lt3")
        fake = str(row.get("fake_breakout_risk") or "NA").lower()
        factors.append(f"fake_{fake}")

        confirms = direction_confirmations(row, side)
        if confirms >= 6:
            factors.append("dirconf_strong")
        elif confirms >= 4:
            factors.append("dirconf_mid")
        else:
            factors.append("dirconf_weak")

        factors.append("timeframe_aligned" if _timeframes_aligned(row, side) else "timeframe_not_aligned")
        factors.append("taker_aligned" if _taker_aligned(row, side) else "taker_not_aligned")
        factors.append("depth_aligned" if _depth_aligned(row, side) else "depth_not_aligned")
        factors.append("sm_aligned" if _sm_aligned(row, side) else "sm_not_aligned")
        factors.append("oi_positive" if _f(row.get("oi_change")) >= 0 else "oi_negative")

        volume = _f(row.get("volume_spike"))
        if volume >= 1.8:
            factors.append("volume_hot")
        elif volume >= 1.2:
            factors.append("volume_ok")
        else:
            factors.append("volume_weak")

        wick = _f(row.get("wick_ratio"))
        if wick <= 0.45:
            factors.append("wick_low")
        elif wick <= 0.55:
            factors.append("wick_mid")
        else:
            factors.append("wick_high")

        risk_pct = self._risk_pct(row, plan)
        cost_r = _f(row.get("cost_r"))
        if cost_r <= 0 and risk_pct > 0:
            cost_r = round_trip_cost_pct() / max(risk_pct, 0.0001)
        factors.append("cost_drag_high" if cost_r > 0.35 else "cost_drag_ok")

        notional = _f(row.get("notional"))
        margin = _f(row.get("margin"))
        if notional > 0 and notional < float(settings.trade_min_notional_usdt):
            factors.append("small_notional")
        if margin > 0 and margin < float(settings.trade_min_margin_usdt):
            factors.append("small_margin")

        close_reason = str(row.get("close_reason") or "")
        if close_reason:
            factors.append(f"exit_{close_reason.lower()}")

        return factors

    def _pattern_key(self, row: dict[str, Any], side: str, plan: StrategyPlan | None) -> str:
        factors = set(self._factors(row, side, plan))
        parts = [
            f"side={side}",
            "fund=3" if "fund_confirm_3" in factors else "fund<3",
            next((f for f in factors if f.startswith("fake_")), "fake_na"),
            next((f for f in factors if f.startswith("dirconf_")), "dirconf_na"),
            "tf=ok" if "timeframe_aligned" in factors else "tf=bad",
            "taker=ok" if "taker_aligned" in factors else "taker=bad",
            "depth=ok" if "depth_aligned" in factors else "depth=bad",
            next((f for f in factors if f.startswith("wick_")), "wick_na"),
            next((f for f in factors if f.startswith("volume_")), "volume_na"),
            "cost=high" if "cost_drag_high" in factors else "cost=ok",
        ]
        return "|".join(parts)

    def _risk_pct(self, row: dict[str, Any], plan: StrategyPlan | None) -> float:
        if plan is not None and plan.ideal_entry_price > 0:
            return abs(float(plan.ideal_entry_price) - float(plan.stop_loss or 0.0)) / float(plan.ideal_entry_price)
        risk_pct = _f(row.get("risk_pct")) / 100.0
        if risk_pct > 0:
            return risk_pct
        atr_pct = _f(row.get("atr_pct")) / 100.0
        if atr_pct > 0:
            return min(max(atr_pct * max(0.1, float(settings.replay_atr_risk_mult)), float(settings.replay_min_risk_pct)), float(settings.replay_max_risk_pct))
        return 0.0

    def _metrics(self, samples: list[dict[str, Any]]) -> dict[str, float]:
        pnls = [_f(sample.get("pnl")) for sample in samples]
        wins = [pnl for pnl in pnls if pnl > 0]
        losses = [pnl for pnl in pnls if pnl < 0]
        gross_profit = sum(wins)
        gross_loss = abs(sum(losses))
        return {
            "count": len(pnls),
            "win_rate": len(wins) / max(1, len(wins) + len(losses)),
            "profit_factor": gross_profit / gross_loss if gross_loss > 0 else (999.0 if gross_profit > 0 else 0.0),
            "pnl": sum(pnls),
            "avg_pnl": sum(pnls) / len(pnls) if pnls else 0.0,
        }

    def _factor_summary(self, samples: list[dict[str, Any]], *, positive: bool) -> list[dict[str, Any]]:
        buckets: dict[str, list[dict[str, Any]]] = {}
        for sample in samples:
            for factor in sample.get("factors") or []:
                if factor.startswith(("side_", "score_", "exit_")):
                    continue
                buckets.setdefault(factor, []).append(sample)

        records = []
        min_bucket = max(3, min(int(settings.trade_attribution_min_samples), 20))
        for factor, bucket in buckets.items():
            if positive and not _is_profit_factor(factor):
                continue
            if not positive and not _is_loss_factor(factor):
                continue
            if len(bucket) < min_bucket:
                continue
            metrics = self._metrics(bucket)
            if positive and metrics["pnl"] <= 0:
                continue
            if not positive and metrics["pnl"] >= 0:
                continue
            records.append(
                {
                    "factor": factor,
                    "label": _label_for(factor),
                    "samples": metrics["count"],
                    "win_rate": round(metrics["win_rate"], 4),
                    "profit_factor": round(metrics["profit_factor"], 4),
                    "pnl": round(metrics["pnl"], 4),
                    "avg_pnl": round(metrics["avg_pnl"], 6),
                    "advice": _advice_for(factor),
                }
            )
        if positive:
            records.sort(key=lambda x: (x["profit_factor"], x["win_rate"], x["pnl"]), reverse=True)
        else:
            records.sort(key=lambda x: (x["pnl"], x["profit_factor"], x["win_rate"]))
        return records[:10]

    def _blocked_symbol_sides(self, samples: list[dict[str, Any]]) -> list[dict[str, Any]]:
        buckets: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for sample in samples:
            symbol = str(sample.get("symbol") or "")
            side = str(sample.get("side") or "")
            if symbol and side:
                buckets.setdefault((symbol, side), []).append(sample)
        records = []
        min_bucket = max(2, int(settings.strategy_block_symbol_side_min_trades))
        for (symbol, side), bucket in buckets.items():
            if len(bucket) < min_bucket:
                continue
            metrics = self._metrics(bucket)
            if metrics["pnl"] < 0 and metrics["win_rate"] < settings.strategy_block_symbol_side_win_rate:
                records.append(
                    {
                        "symbol": symbol,
                        "side": side,
                        "samples": metrics["count"],
                        "win_rate": round(metrics["win_rate"], 4),
                        "pnl": round(metrics["pnl"], 4),
                    }
                )
        return sorted(records, key=lambda x: x["pnl"])[:10]

    def _empty_report(self, pattern: str, factors: list[str], reason: str) -> CausalAttributionReport:
        return CausalAttributionReport(
            enabled=False,
            current_pattern=pattern,
            current_factors=factors,
            matched_samples=0,
            match_level=reason,
            win_rate=0.0,
            profit_factor=0.0,
            pnl=0.0,
            avg_pnl=0.0,
            paper_ok=True,
            live_ok=False,
            reasons=[f"trade_attribution_{reason}"],
            advice=[],
            matched_loss_causes=[],
            matched_profit_drivers=[],
        )


def _timeframes_aligned(row: dict[str, Any], side: str) -> bool:
    if side == "LONG":
        return _f(row.get("change_5m")) > 0 and _f(row.get("change_15m")) > 0 and _f(row.get("change_1h")) >= 0
    return _f(row.get("change_5m")) < 0 and _f(row.get("change_15m")) < 0 and _f(row.get("change_1h")) <= 0


def _taker_aligned(row: dict[str, Any], side: str) -> bool:
    return _f(row.get("taker_buy_ratio"), 0.5) >= 0.58 if side == "LONG" else _f(row.get("taker_sell_ratio"), 0.5) >= 0.58


def _depth_aligned(row: dict[str, Any], side: str) -> bool:
    return _f(row.get("depth_imbalance")) >= 0.12 if side == "LONG" else _f(row.get("depth_imbalance")) <= -0.12


def _sm_aligned(row: dict[str, Any], side: str) -> bool:
    return _f(row.get("sm_delta")) >= 0 if side == "LONG" else _f(row.get("sm_delta")) <= 0


def _label_for(factor: str) -> str:
    labels = {
        "fund_confirm_lt3": "资金确认不足",
        "fake_medium": "假突破中风险",
        "fake_high": "假突破高风险",
        "dirconf_weak": "方向确认不足",
        "timeframe_not_aligned": "周期方向不一致",
        "taker_not_aligned": "主动买卖不一致",
        "depth_not_aligned": "盘口深度不一致",
        "sm_not_aligned": "聪明钱方向不一致",
        "oi_negative": "OI 负确认",
        "volume_weak": "量能不足",
        "wick_high": "影线风险高",
        "cost_drag_high": "手续费/滑点拖拽高",
        "small_notional": "名义金额过小",
        "small_margin": "保证金过小",
    }
    return labels.get(factor, factor)


def _factor_token(value: str) -> str:
    out = []
    for ch in str(value).lower():
        out.append(ch if ch.isalnum() else "_")
    token = "".join(out).strip("_")
    while "__" in token:
        token = token.replace("__", "_")
    return token or "unknown"


def _is_loss_factor(factor: str) -> bool:
    return (
        factor in {
            "fund_confirm_lt3",
            "fake_medium",
            "fake_high",
            "dirconf_weak",
            "timeframe_not_aligned",
            "taker_not_aligned",
            "depth_not_aligned",
            "sm_not_aligned",
            "oi_negative",
            "volume_weak",
            "wick_high",
            "cost_drag_high",
            "small_notional",
            "small_margin",
        }
        or factor.startswith("exit_")
    )


def _is_profit_factor(factor: str) -> bool:
    return factor in {
        "fund_confirm_3",
        "fake_low",
        "dirconf_strong",
        "timeframe_aligned",
        "taker_aligned",
        "depth_aligned",
        "sm_aligned",
        "oi_positive",
        "volume_hot",
        "wick_low",
        "cost_drag_ok",
    }


def _advice_for(factor: str) -> str:
    advice = {
        "fund_confirm_lt3": "等待资金确认满 3/3。",
        "fake_medium": "假突破风险未降到 LOW 前只观察。",
        "fake_high": "高假突破风险禁止开仓。",
        "dirconf_weak": "要求更多方向因子共振。",
        "timeframe_not_aligned": "等待 5m/15m/1h 同向。",
        "taker_not_aligned": "等待主动买卖方向跟随。",
        "depth_not_aligned": "等待盘口深度转向同侧。",
        "sm_not_aligned": "等待聪明钱增量同向。",
        "oi_negative": "OI 不支持时降低权重或等待。",
        "volume_weak": "量能不足不追单。",
        "wick_high": "影线过高时放弃追单或扩大确认。",
        "cost_drag_high": "扩大止盈空间或放弃成本拖拽过高的结构。",
        "small_notional": "提高名义金额，否则目标收益覆盖不了成本。",
        "small_margin": "提高保证金下限或放弃小仓噪音交易。",
    }
    return advice.get(factor, "该结构历史表现偏弱，等待更强确认。")


def _f(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


def _sample_time(sample: dict[str, Any]) -> int:
    return int(_f(sample.get("close_time") or sample.get("ts_ms") or 0))


def _sample_key(sample: dict[str, Any]) -> str:
    raw = sample.get("sample_id") or sample.get("position_id")
    if raw:
        return str(raw)
    return "|".join(
        str(sample.get(key) or "")
        for key in ("symbol", "side", "direction", "open_time", "close_time", "pnl")
    )


def _dedupe_recent(samples: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for sample in sorted(samples, key=_sample_time, reverse=True):
        key = _sample_key(sample)
        if key in seen:
            continue
        seen.add(key)
        out.append(sample)
        if len(out) >= limit:
            break
    return out


trade_attributor = TradeAttributor()


def _label_for(factor: str) -> str:
    labels = {
        "fund_confirm_lt3": "资金确认不足",
        "fake_medium": "假突破中风险",
        "fake_high": "假突破高风险",
        "dirconf_weak": "方向确认不足",
        "timeframe_not_aligned": "周期方向不一致",
        "taker_not_aligned": "主动买卖不一致",
        "depth_not_aligned": "盘口深度不一致",
        "sm_not_aligned": "聪明钱方向不一致",
        "oi_negative": "OI 负确认",
        "volume_weak": "量能不足",
        "wick_high": "影线风险高",
        "cost_drag_high": "手续费/滑点拖拽高",
        "small_notional": "名义金额过小",
        "small_margin": "保证金过小",
        "stop_loss_hit": "止损触发",
        "signal_decay": "信号衰减",
        "reverse_signal": "反向信号",
        "fake_breakout_exit": "假突破退出",
        "timeout_no_followthrough": "超时无延续",
        "fee_drag_high": "手续费占比过高",
        "tp2_followthrough": "TP2 延续",
        "profit_covers_cost": "利润覆盖成本",
    }
    return labels.get(factor, factor)


def _advice_for(factor: str) -> str:
    advice = {
        "fund_confirm_lt3": "等待资金确认满 3/3。",
        "fake_medium": "假突破风险未降到 LOW 前只观察。",
        "fake_high": "高假突破风险禁止开仓。",
        "dirconf_weak": "要求更多方向因子共振。",
        "timeframe_not_aligned": "等待 5m/15m/1h 同向。",
        "taker_not_aligned": "等待主动买卖方向跟随。",
        "depth_not_aligned": "等待盘口深度转向同侧。",
        "sm_not_aligned": "等待聪明钱增量同向。",
        "oi_negative": "OI 不支持时降低权重或等待。",
        "volume_weak": "量能不足不追单。",
        "wick_high": "影线过高时放弃追单或扩大确认。",
        "cost_drag_high": "扩大止盈空间或放弃成本拖拽过高的结构。",
        "small_notional": "提高名义金额，否则目标收益覆盖不了成本。",
        "small_margin": "提高保证金下限或放弃小仓噪音交易。",
        "stop_loss_hit": "检查入场是否追晚、止损是否过近、是否缺少方向确认。",
        "signal_decay": "信号衰减快的结构需要缩短等待或直接过滤。",
        "reverse_signal": "出现反向强确认时，降低同方向再入场权重。",
        "fake_breakout_exit": "假突破退出频繁时，只允许 LOW 风险且确认 3/3。",
        "timeout_no_followthrough": "无延续结构减少持仓时长或提高 TP/确认要求。",
        "fee_drag_high": "提高目标净收益和名义金额，避免利润被成本吞掉。",
        "tp2_followthrough": "保留这种延续结构，但仍需经过回放和归因验证。",
        "profit_covers_cost": "优先选择利润空间能稳定覆盖手续费/滑点的结构。",
    }
    return advice.get(factor, "该结构历史表现偏弱，等待更强确认。")


def _close_reason_label(reason: str) -> str:
    labels = {
        "SL": "止损",
        "REPLAY_SL": "回放止损",
        "TP2": "TP2",
        "REPLAY_TP2": "回放 TP2",
        "LOCKED_STOP": "锁盈/保本止损",
        "SIGNAL_DECAY_EXIT": "信号衰减退出",
        "ADVERSE_SIGNAL_CUT": "不利信号切仓",
        "ADVERSE_REVERSE_CUT": "反向信号切仓",
        "REVERSE_SIGNAL_EXIT": "反向信号退出",
        "ADVERSE_FAKE_CUT": "假突破切仓",
        "FAKE_BREAKOUT_EXIT": "假突破退出",
        "MAX_HOLD_TIMEOUT": "最大持仓超时",
        "REPLAY_TIMEOUT": "回放超时",
    }
    return labels.get(reason, reason)


def _close_reason_advice(reason: str) -> str:
    advice = {
        "SL": "重点复查入场是否追晚、止损是否过近、方向确认是否不足。",
        "REPLAY_SL": "回放止损多说明信号结构本身不稳定，需要提高入场过滤。",
        "SIGNAL_DECAY_EXIT": "信号衰减退出多说明异动持续性弱，应提高延续确认。",
        "ADVERSE_SIGNAL_CUT": "不利信号切仓多说明入场质量不足，应提前拦截。",
        "ADVERSE_REVERSE_CUT": "反向信号频繁出现时，降低原方向权重。",
        "REVERSE_SIGNAL_EXIT": "反向信号退出多时，应增加反向强度预警。",
        "ADVERSE_FAKE_CUT": "假突破切仓多时，只允许 LOW 假突破风险。",
        "FAKE_BREAKOUT_EXIT": "假突破退出多时，强化影线和盘口确认。",
        "MAX_HOLD_TIMEOUT": "超时无延续时，缩短持仓窗口或提高趋势确认。",
        "REPLAY_TIMEOUT": "回放超时多说明 TP 结构或持仓周期需要重校准。",
    }
    return advice.get(reason, "复查该平仓类型下的入场结构和持仓规则。")


def _unique(values: list[str]) -> list[str]:
    out: list[str] = []
    seen = set()
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        out.append(value)
    return out


def _round_or_zero(value: Any, digits: int) -> float:
    return round(_f(value), digits)
