from __future__ import annotations

import math
import warnings
from pathlib import Path
from typing import Any

from backend.models import RadarItem


UNKNOWN_SYMBOL_KEY = "__UNKNOWN__"

UNIVERSAL_ANOMALY_NUMERIC_FEATURE_NAMES = [
    "change_5m",
    "change_15m",
    "change_1h",
    "volume_spike",
    "oi_change",
    "funding_rate",
    "taker_imbalance",
    "depth_imbalance",
    "atr_pct",
    "current_wick_ratio",
    "current_body_ratio",
    "range_position",
    "range_width_pct",
    "distance_to_resistance_pct",
    "distance_to_support_pct",
    "breakout_up",
    "breakout_down",
    "btc_relative_5m",
    "eth_relative_5m",
]

UNIVERSAL_ANOMALY_CATEGORICAL_FEATURE_NAMES = [
    "symbol_key",
]

UNIVERSAL_ANOMALY_FEATURE_NAMES = [
    *UNIVERSAL_ANOMALY_NUMERIC_FEATURE_NAMES,
    *UNIVERSAL_ANOMALY_CATEGORICAL_FEATURE_NAMES,
]


class UniversalAnomalyModel:
    model_name = "universal_anomaly_v0_microstructure_rules"
    latency_budget_ms = 1.0

    def __init__(self) -> None:
        self._trained_artifact: dict[str, Any] | None = None

    def extract_features(self, item: RadarItem) -> dict[str, Any]:
        metrics = self._structure_metrics(item)
        taker_buy = _f(item.taker_buy_ratio, 0.5)
        taker_sell = _f(item.taker_sell_ratio, 0.5)
        numeric_features = {
            "change_5m": _clip(_f(item.change_5m), -12.0, 12.0),
            "change_15m": _clip(_f(item.change_15m), -18.0, 18.0),
            "change_1h": _clip(_f(item.change_1h), -30.0, 30.0),
            "volume_spike": _clip(_f(item.volume_spike, 1.0), 0.0, 20.0),
            "oi_change": _clip(_f(item.oi_change), -20.0, 20.0),
            "funding_rate": _clip(_f(item.funding_rate), -0.02, 0.02),
            "taker_imbalance": _clip(taker_buy - taker_sell, -1.0, 1.0),
            "depth_imbalance": _clip(_f(item.depth_imbalance), -1.0, 1.0),
            "atr_pct": _clip(_f(item.atr_pct), 0.0, 30.0),
            "current_wick_ratio": _clip(_f(metrics.get("current_wick_ratio"), _f(item.wick_ratio)), 0.0, 1.0),
            "current_body_ratio": _clip(_f(metrics.get("current_body_ratio")), 0.0, 1.0),
            "range_position": _clip(_f(metrics.get("range_position"), 0.5), 0.0, 1.0),
            "range_width_pct": _clip(_f(metrics.get("range_width_pct")), 0.0, 80.0),
            "distance_to_resistance_pct": _clip(_f(metrics.get("distance_to_resistance_pct")), -20.0, 20.0),
            "distance_to_support_pct": _clip(_f(metrics.get("distance_to_support_pct")), -20.0, 20.0),
            "breakout_up": 1.0 if bool(metrics.get("breakout_up")) else 0.0,
            "breakout_down": 1.0 if bool(metrics.get("breakout_down")) else 0.0,
            "btc_relative_5m": self._market_relative_feature(item, "btc_change_5m"),
            "eth_relative_5m": self._market_relative_feature(item, "eth_change_5m"),
        }
        features = {key: round(value, 8) for key, value in numeric_features.items()}
        features["symbol_key"] = self._symbol_key(getattr(item, "symbol", ""))
        return features

    def predict(self, item: RadarItem) -> dict[str, Any]:
        features = self.extract_features(item)
        trained_prediction = self._predict_with_trained_artifact(features)
        if trained_prediction:
            return trained_prediction
        direction_score = self._direction_score(features)
        quality_score = self._quality_score(features)
        long_raw = _sigmoid(direction_score) * quality_score
        short_raw = _sigmoid(-direction_score) * quality_score
        neutral_raw = max(0.06, (1.0 - quality_score) * 0.45)
        total = max(long_raw + short_raw + neutral_raw, 1e-9)
        probabilities = {
            "LONG": round(long_raw / total, 4),
            "SHORT": round(short_raw / total, 4),
            "NEUTRAL": round(neutral_raw / total, 4),
        }
        direction = max(probabilities.items(), key=lambda row: row[1])[0]
        confidence = round(max(probabilities.values()) * 100.0, 2)
        return {
            "model": self.model_name,
            "direction": direction,
            "confidence": confidence,
            "probabilities": probabilities,
            "features": features,
            "latency_budget_ms": self.latency_budget_ms,
            "evidence": [
                f"micro_direction_score={round(direction_score, 6)}",
                f"micro_quality_score={round(quality_score, 6)}",
                f"taker_imbalance={features['taker_imbalance']}",
                f"depth_imbalance={features['depth_imbalance']}",
                f"current_wick_ratio={features['current_wick_ratio']}",
            ],
        }

    def activate_trained_artifact(self, artifact_or_path: dict[str, Any] | str | Path) -> dict[str, Any]:
        if isinstance(artifact_or_path, dict):
            artifact = artifact_or_path
        else:
            try:
                import joblib

                artifact = joblib.load(Path(artifact_or_path))
            except Exception as exc:
                self._trained_artifact = None
                return {"active": False, "error": f"{type(exc).__name__}:{exc}"}
        if not self._valid_trained_artifact(artifact):
            self._trained_artifact = None
            return {"active": False, "error": "invalid_trained_artifact"}
        self._trained_artifact = artifact
        return self.trained_model_status()

    def clear_trained_artifact(self) -> None:
        self._trained_artifact = None

    def trained_model_status(self) -> dict[str, Any]:
        artifact = self._trained_artifact if isinstance(self._trained_artifact, dict) else {}
        return {
            "active": bool(artifact),
            "model": artifact.get("model_name", ""),
            "engine": artifact.get("engine", ""),
            "horizon_minutes": artifact.get("horizon_minutes"),
            "sample_count": artifact.get("sample_count", 0),
            "metrics": artifact.get("metrics", {}),
            "trained_at": artifact.get("trained_at", 0),
        }

    def _predict_with_trained_artifact(self, features: dict[str, Any]) -> dict[str, Any] | None:
        artifact = self._trained_artifact if isinstance(self._trained_artifact, dict) else None
        if not artifact or not self._valid_trained_artifact(artifact):
            return None
        estimator = artifact["estimator"]
        feature_names = list(artifact["feature_names"])
        if artifact.get("feature_vector_format") == "dict":
            vector = [self._prediction_record(features, artifact)]
        else:
            vector = [[_f(features.get(name)) for name in feature_names]]
        try:
            with warnings.catch_warnings():
                warnings.filterwarnings(
                    "ignore",
                    message="X does not have valid feature names.*",
                    category=UserWarning,
                )
                raw_probabilities = estimator.predict_proba(vector)[0]
        except Exception:
            return None
        classes = [str(row) for row in getattr(estimator, "classes_", artifact.get("classes", []))]
        probabilities = {"LONG": 0.0, "SHORT": 0.0, "NEUTRAL": 0.0}
        for label, probability in zip(classes, raw_probabilities):
            if label in probabilities:
                probabilities[label] = round(float(probability), 4)
        total = sum(probabilities.values())
        if total > 0:
            probabilities = {key: round(value / total, 4) for key, value in probabilities.items()}
        direction = max(probabilities.items(), key=lambda row: row[1])[0]
        confidence = round(max(probabilities.values()) * 100.0, 2)
        engine = str(artifact.get("engine") or "trained")
        return {
            "model": artifact.get("model_name", f"universal_anomaly_{engine}_v1"),
            "direction": direction,
            "confidence": confidence,
            "probabilities": probabilities,
            "features": features,
            "latency_budget_ms": self.latency_budget_ms,
            "trained": True,
            "evidence": [
                f"trained_model_engine={engine}",
                f"trained_model_horizon={artifact.get('horizon_minutes')}",
                f"trained_model_samples={artifact.get('sample_count')}",
                f"trained_validation_accuracy={(artifact.get('metrics') or {}).get('validation_accuracy')}",
            ],
        }

    def _valid_trained_artifact(self, artifact: dict[str, Any]) -> bool:
        return (
            isinstance(artifact, dict)
            and artifact.get("estimator") is not None
            and isinstance(artifact.get("feature_names"), list)
            and bool(artifact.get("feature_names"))
        )

    def _prediction_record(self, features: dict[str, Any], artifact: dict[str, Any]) -> dict[str, Any]:
        record: dict[str, Any] = {}
        categorical = set(artifact.get("categorical_feature_names") or UNIVERSAL_ANOMALY_CATEGORICAL_FEATURE_NAMES)
        symbol_categories = set(str(row) for row in (artifact.get("symbol_categories") or []))
        for name in artifact.get("feature_names") or UNIVERSAL_ANOMALY_FEATURE_NAMES:
            if name in categorical:
                value = self._symbol_key(features.get(name))
                if symbol_categories and value not in symbol_categories:
                    value = UNKNOWN_SYMBOL_KEY
                record[name] = value
            else:
                record[name] = _f(features.get(name))
        return record

    def training_row(self, item: RadarItem, *, future_return_pct: float, horizon_minutes: int) -> dict[str, Any]:
        future = _f(future_return_pct)
        if future > 0:
            label = "LONG"
        elif future < 0:
            label = "SHORT"
        else:
            label = "NEUTRAL"
        return {
            "features": self.extract_features(item),
            "label_return_pct": round(future, 8),
            "label_direction": label,
            "horizon_minutes": int(horizon_minutes),
        }

    def _direction_score(self, features: dict[str, float]) -> float:
        price_impulse = features["change_5m"] * 0.55 + features["change_15m"] * 0.22 + features["change_1h"] * 0.08
        flow = features["taker_imbalance"] * 3.2 + features["depth_imbalance"] * 2.4
        oi = max(0.0, features["oi_change"]) * _sign(price_impulse) * 0.28
        breakout = features["breakout_up"] * 1.25 - features["breakout_down"] * 1.25
        relative = features["btc_relative_5m"] * 0.14 + features["eth_relative_5m"] * 0.10
        wick_drag = features["current_wick_ratio"] * _sign(price_impulse) * -0.55
        return price_impulse + flow + oi + breakout + relative + wick_drag

    def _quality_score(self, features: dict[str, float]) -> float:
        volume = _sigmoid((features["volume_spike"] - 1.2) * 0.9)
        body = 0.55 + min(0.35, features["current_body_ratio"] * 0.35)
        wick = max(0.25, 1.0 - features["current_wick_ratio"] * 0.85)
        volatility = 0.55 + min(0.25, features["atr_pct"] / 20.0)
        return _clip(volume * body * wick * volatility + 0.18, 0.05, 0.95)

    def _structure_metrics(self, item: RadarItem) -> dict[str, Any]:
        features = item.score_features if isinstance(item.score_features, dict) else {}
        metrics = features.get("structure_metrics") if isinstance(features, dict) else {}
        return metrics if isinstance(metrics, dict) else {}

    def _market_relative_feature(self, item: RadarItem, key: str) -> float:
        features = item.score_features if isinstance(item.score_features, dict) else {}
        context = features.get("market_context") if isinstance(features, dict) else {}
        if not isinstance(context, dict):
            return 0.0
        return _clip(_f(item.change_5m) - _f(context.get(key)), -20.0, 20.0)

    def _symbol_key(self, value: Any) -> str:
        key = str(value or "").strip().upper()
        return key or UNKNOWN_SYMBOL_KEY


def _sigmoid(value: float) -> float:
    value = _clip(value, -60.0, 60.0)
    return 1.0 / (1.0 + math.exp(-value))


def _sign(value: float) -> float:
    if value > 0:
        return 1.0
    if value < 0:
        return -1.0
    return 0.0


def _clip(value: float, low: float, high: float) -> float:
    return min(high, max(low, float(value)))


def _f(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


universal_anomaly_model = UniversalAnomalyModel()
