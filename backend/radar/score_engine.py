from typing import Any

from backend.models import MarketSnapshot, Direction

def clamp(v, lo=0, hi=100): return max(lo, min(hi, v))

def norm_abs(x, scale): return clamp(abs(x)/scale*100)

SCORE_WEIGHTS = {
    "trend_score": 0.15,
    "volume_score": 0.15,
    "volatility_score": 0.10,
    "oi_score": 0.15,
    "taker_score": 0.10,
    "timeframe_score": 0.10,
    "sm_score": 0.10,
    "heat_score": 0.10,
    "fake_penalty": -0.15,
}

def direction(snapshot: MarketSnapshot) -> Direction:
    long_score = 0.0
    short_score = 0.0
    if snapshot.change_5m > 0:
        long_score += 10
    elif snapshot.change_5m < 0:
        short_score += 10
    if snapshot.change_15m > 0:
        long_score += 10
    elif snapshot.change_15m < 0:
        short_score += 10
    if snapshot.change_1h > 0:
        long_score += 6
    elif snapshot.change_1h < 0:
        short_score += 6
    if snapshot.oi_change > 0 and snapshot.change_5m > 0: long_score += 15
    if snapshot.oi_change > 0 and snapshot.change_5m < 0: short_score += 15
    if snapshot.taker_buy_ratio > 0.55: long_score += 10
    if snapshot.taker_sell_ratio > 0.55: short_score += 10
    if snapshot.depth_imbalance > 0.15: long_score += 6
    if snapshot.depth_imbalance < -0.15: short_score += 6
    if long_score - short_score >= 12: return "LONG"
    if short_score - long_score >= 12: return "SHORT"
    return "NEUTRAL"

class ScoreEngine:
    def feature_scores(self, s: MarketSnapshot, sm_score: float, heat_score: float, fake_penalty: float) -> dict:
        trend_score = clamp((abs(s.change_5m)*20 + abs(s.change_15m)*12 + abs(s.change_1h)*6))
        volume_score = clamp((s.volume_spike-0.5)/3*100)
        volatility_score = clamp(s.atr_pct/1.6*100)
        oi_score = norm_abs(s.oi_change, 2.0)
        taker_score = norm_abs(s.taker_buy_ratio-0.5, 0.18)
        timeframe_score = 80 if (s.change_5m*s.change_15m>0 and s.change_15m*s.change_1h>=0) else 45
        return dict(trend_score=trend_score, volume_score=volume_score, volatility_score=volatility_score, oi_score=oi_score, taker_score=taker_score, timeframe_score=timeframe_score, sm_score=sm_score, heat_score=heat_score, fake_penalty=fake_penalty)

    def total(self, features: dict, weights: dict[str, float] | None = None) -> float:
        effective_weights = self._effective_weights(weights)
        score = sum(float(features.get(key, 0.0) or 0.0) * weight for key, weight in effective_weights.items())
        return round(clamp(score), 2)

    def explain(
        self,
        features: dict,
        weights: dict[str, float] | None = None,
        calibration: dict[str, Any] | None = None,
    ) -> dict:
        effective_weights = self._effective_weights(weights)
        components = {}
        total_before_clamp = 0.0
        for key, weight in effective_weights.items():
            raw = float(features.get(key, 0.0) or 0.0)
            contribution = raw * weight
            total_before_clamp += contribution
            components[key] = {
                "raw": round(raw, 4),
                "weight": weight,
                "contribution": round(contribution, 4),
                "role": "penalty" if weight < 0 else "positive",
            }
        positives = sorted(
            [dict(name=key, **value) for key, value in components.items() if value["contribution"] > 0],
            key=lambda row: row["contribution"],
            reverse=True,
        )
        penalties = sorted(
            [dict(name=key, **value) for key, value in components.items() if value["contribution"] < 0],
            key=lambda row: row["contribution"],
        )
        return {
            "score": round(clamp(total_before_clamp), 2),
            "total_before_clamp": round(total_before_clamp, 4),
            "weights": effective_weights,
            "components": components,
            "top_positive": positives[:4],
            "top_penalty": penalties[:4],
            "calibration": calibration or {"active": False, "reason": "default_weights"},
            "market_logic": {
                "trend_score": "absolute short-term price displacement; heat, not expectancy",
                "volume_score": "recent quote volume expansion versus recent baseline",
                "volatility_score": "ATR percent; useful for movement but can reward noise",
                "oi_score": "absolute open-interest expansion/contraction pressure",
                "taker_score": "active buy/sell imbalance magnitude",
                "timeframe_score": "5m/15m/1h directional consistency",
                "sm_score": "synthetic abnormal-flow score, not true insider identification",
                "heat_score": "recent score acceleration from in-memory scan history",
                "fake_penalty": "fake-breakout and wick/funding risk deduction",
            },
            "caveat": "radar_score is an anomaly score, not a direct win-rate score",
        }

    def _effective_weights(self, weights: dict[str, float] | None = None) -> dict[str, float]:
        if not weights:
            return dict(SCORE_WEIGHTS)
        return {
            key: float(weights.get(key, default_weight))
            for key, default_weight in SCORE_WEIGHTS.items()
        }

score_engine = ScoreEngine()
