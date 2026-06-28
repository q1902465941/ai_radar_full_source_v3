import time

from backend.config import settings
from backend.learning.ai_strategy_feedback import ai_strategy_feedback
from backend.learning.event_calibrator import event_calibrator
from backend.learning.trade_attributor import trade_attributor
from backend.models import RadarItem
from backend.radar.candidate_feature_enhancer import candidate_feature_enhancer

class ContextCompressor:
    def build_strategy_context(self, item: RadarItem, position_context: dict | None = None) -> dict:
        cyqnt_report = candidate_feature_enhancer.evaluate(item).asdict()
        position_context = position_context or {}
        candidate_selection = position_context.get("candidate_selection") if isinstance(position_context.get("candidate_selection"), dict) else {}
        pre_ai_refresh = candidate_selection.get("pre_ai_market_refresh") if isinstance(candidate_selection.get("pre_ai_market_refresh"), dict) else {}
        geometry_sample = position_context.get("strategy_geometry_sample") if isinstance(position_context.get("strategy_geometry_sample"), dict) else {}
        score_features = item.score_features if isinstance(item.score_features, dict) else {}
        universal_model = score_features.get("universal_anomaly_model") if isinstance(score_features.get("universal_anomaly_model"), dict) else {}
        return {
            "task": "generate_trade_plan",
            "symbol": item.symbol,
            "side_bias": item.direction,
            "current_price": item.price,
            "market_freshness": {
                "latest_market_required": bool(candidate_selection.get("latest_market_required")),
                "item_ts_ms": item.ts_ms,
                "item_age_seconds": round(max(0, time.time() * 1000 - int(item.ts_ms or 0)) / 1000, 3),
                "pre_ai_market_refresh": pre_ai_refresh,
                "instruction": (
                    "Generate only from this latest refreshed radar snapshot. If the snapshot is missing, stale, "
                    "or conflicts with side_bias/current_price, return WAIT rather than reusing older direction geometry."
                ),
            },
            "radar": {
                "score": item.score,
                "rank": item.rank,
                "stage": item.stage,
                "trigger_mode": item.trigger_mode,
                "fund_confirm": f"{item.fund_confirm_count}/{item.fund_confirm_total}",
                "fake_breakout_risk": item.fake_breakout_risk,
                "dealer_radar": item.dealer_radar,
                "sm_position": item.sm_position,
                "sm_delta": item.sm_delta,
                "heat_slope": item.heat_slope,
                "slope_score": item.slope_score,
                "score_history": item.score_history[-8:],
                "score_explain": {
                    "top_positive": (item.score_explain or {}).get("top_positive", [])[:4],
                    "top_penalty": (item.score_explain or {}).get("top_penalty", [])[:4],
                    "calibration": (item.score_explain or {}).get("calibration", {}),
                    "caveat": (item.score_explain or {}).get("caveat", ""),
                },
            },
            "market_changes": {
                "change_5m": item.change_5m,
                "change_15m": item.change_15m,
                "change_1h": item.change_1h,
                "oi_change": item.oi_change,
                "volume_spike": item.volume_spike,
                "funding_rate": item.funding_rate,
                "taker_buy_ratio": item.taker_buy_ratio,
                "taker_sell_ratio": item.taker_sell_ratio,
                "depth_imbalance": item.depth_imbalance,
                "atr_pct": item.atr_pct,
                "wick_ratio": item.wick_ratio,
            },
            "local_quality_gate": {
                "paper_min_estimated_win_rate": settings.strategy_min_paper_win_rate,
                "live_min_estimated_win_rate": settings.strategy_min_live_win_rate,
                "min_expected_r": settings.strategy_min_expected_r,
                "min_tp2_r": settings.strategy_min_tp2_r,
                "paper_closed_loop": not (settings.trade_mode == "live" and settings.live_trading_enabled),
                "candidate_selection": candidate_selection,
                "paper_probe_policy": {
                    "purpose": "collect controlled paper-only samples so the system can learn; this is not live permission",
                    "candidate_scope": "Top5 radar movers enter AI review first; balanced Top20 backups may enter when Top5 is blocked by soft risk and hard-risk filters still pass",
                    "max_wick_ratio": settings.paper_probe_max_wick_ratio,
                    "hard_current_wick_ratio": 0.88,
                    "balanced_current_wick_ratio": round(min(0.75, max(0.65, float(settings.paper_probe_max_wick_ratio or 0.55) + 0.10)), 6),
                    "noise_budget_role": "current extreme wick is a hard gate; older historical wick noise is a soft risk that may be balanced by stronger score, fund, direction, and cost evidence",
                    "allowed_when": [
                        "candidate is prefiltered by Top5 priority scope or balanced Top20 backup scope",
                        "fund_confirm has at least 3 current-market confirmations for formal paper trade or at least partial confirmation for paper-only validation",
                        "fake_breakout_risk is not HIGH",
                        "current_wick_ratio is not extreme and any old historical wick spike is compensated by clean current structure",
                        "flow, depth, and timeframe are not conflicting",
                        "fees and slippage are covered by TP targets",
                    ],
                    "not_allowed_when": [
                        "current_wick_ratio is extreme",
                        "recent max wick spike is unresolved or average wick noise remains high",
                        "the setup is only an indicator direction without market mechanism",
                    ],
                },
                "fund_confirm_required_for_order": ">=3 current-market confirmations",
                "round_trip_cost_model": {
                    "taker_fee_each_side": settings.paper_taker_fee_rate,
                    "slippage_each_side": settings.paper_slippage_pct,
                    "instruction": "Expected R must be positive after fees and slippage. Tight stops with high cost drag should return WAIT.",
                },
                "recovery_mode_gate": {
                    "min_score": settings.strategy_recovery_min_score,
                    "min_confidence": settings.strategy_recovery_min_confidence,
                    "min_expected_r": settings.strategy_recovery_min_expected_r,
                    "min_tp2_r": settings.strategy_recovery_min_tp2_r,
                    "instruction": "When position_context.performance_guard.recovery_mode is true, return OPEN only for exceptional setups meeting these stricter gates.",
                },
                "instruction": "For live candidates, require the strict gates. For paper_closed_loop, a clean Top5 or strict_review validation plan may OPEN only as paper-only learning; live permission must remain false. If candidate_selection.source is strict, rank is diagnostic and not an automatic veto after local strict selection has already passed. If historical attribution, event calibration, market backtest, or cyqnt failure_risks show negative expectancy or insufficient support, return WAIT instead of creating a low-quality learning sample. Downstream quality gates, risk_model, and live_readiness still own final execution permission.",
            },
            "cyqnt_feature_enhancement": {
                **cyqnt_report,
                "role": "local evidence layer for candidate quality; it is not exchange execution permission",
                "must_use_for": [
                    "decide whether the setup has enough edge to produce an OPEN plan",
                    "set confidence according to feature_score, estimated_win_rate, and noise contributions",
                    "define invalidation and hold logic around the strongest positive and negative feature contributions",
                ],
                "wait_bias_if": [
                    "estimated_win_rate is below local_quality_gate.paper_min_estimated_win_rate",
                    "noise or funding contribution is strongly negative",
                    "feature score is weak and current-market attribution/event samples are low",
                ],
            },
            "universal_anomaly_model": {
                **universal_model,
                "role": "coin-agnostic microstructure direction evidence; use it as confirmation, not as standalone order permission",
                "must_check": [
                    "direction probability agrees with side_bias",
                    "microstructure features support the entry instead of only ticker rank",
                    "NEUTRAL or opposite direction lowers confidence and should usually return WAIT unless stronger local evidence overrides it",
                ],
            },
            "event_calibration": event_calibrator.compact_context(item),
            "trade_attribution": trade_attributor.compact_context(item),
            "strategy_geometry_sample": {
                **geometry_sample,
                "role": (
                    "mandatory local kline first-touch evidence for TP/SL geometry; "
                    "OPEN plans should use selected_geometry when status is ok, and should not claim live-quality edge when weak"
                ),
            },
            "ai_strategy_quality_feedback": ai_strategy_feedback.compact_context(item),
            "position_context": position_context,
            "context_budget": {
                "policy": "Use compact trading lessons instead of raw history. Do not request larger context.",
                "priority_order": [
                    "current candidate evidence",
                    "hard avoid_repeating constraints",
                    "review_lessons and candidate_learning_delta",
                    "paper-only learning rules",
                    "schema-required strategy_contract",
                ],
            },
            "required_output": {
                "schema": "StrategyPlan JSON only; schema and full contract shape are provided outside this compact context.",
                "open_requires": "complete strategy_contract with signal, risk, execution, lifecycle, hold/reduce/add/exit, invalidation, cost, learning_tags, allowed_stages, research_review",
                "wait_requires": "wait_type, expire_after_seconds, upgrade_condition",
            }
        }

context_compressor = ContextCompressor()
