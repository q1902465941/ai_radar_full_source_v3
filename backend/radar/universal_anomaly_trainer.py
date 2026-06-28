from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any

from backend.models import now_ms
from backend.radar.universal_anomaly_model import (
    UNKNOWN_SYMBOL_KEY,
    UNIVERSAL_ANOMALY_CATEGORICAL_FEATURE_NAMES,
    UNIVERSAL_ANOMALY_FEATURE_NAMES,
    UNIVERSAL_ANOMALY_NUMERIC_FEATURE_NAMES,
    universal_anomaly_model,
)
from backend.storage.db import db


class UniversalAnomalyClassifierTrainer:
    def __init__(self, database=None, artifact_path: str | Path | None = None, runtime_model=None):
        self.database = database or db
        self.artifact_path = Path(artifact_path or "data/universal_anomaly_model.joblib")
        self.runtime_model = runtime_model or universal_anomaly_model

    def train(
        self,
        *,
        horizon_minutes: int = 5,
        model_type: str = "auto",
        min_samples: int = 100,
        limit: int = 5000,
        activate: bool = True,
        artifact_path: str | Path | None = None,
    ) -> dict[str, Any]:
        target_path = Path(artifact_path or self.artifact_path)
        samples = self.database.list_universal_anomaly_samples(limit=max(1, int(limit)), horizon_minutes=max(1, int(horizon_minutes)))
        rows = self._training_rows(samples)
        rows.sort(key=lambda row: (int(row.get("source_ts_ms") or 0), int(row.get("created_at") or 0)))
        if len(rows) < int(min_samples):
            return {
                "ok": False,
                "error": "not_enough_samples",
                "sample_count": len(rows),
                "min_samples": int(min_samples),
                "horizon_minutes": int(horizon_minutes),
            }
        labels = [row["label"] for row in rows]
        class_counts = dict(Counter(labels))
        if len(class_counts) < 2:
            return {
                "ok": False,
                "error": "not_enough_classes",
                "sample_count": len(rows),
                "class_counts": class_counts,
                "horizon_minutes": int(horizon_minutes),
            }
        engine = self._select_engine(model_type)
        estimator = self._build_estimator(engine)
        records = [row["x"] for row in rows]
        x = records
        y = labels
        metrics = self._fit_and_score(estimator, x, y, class_counts)
        symbol_categories = sorted({str(row["x"].get("symbol_key") or UNKNOWN_SYMBOL_KEY) for row in rows})
        if UNKNOWN_SYMBOL_KEY not in symbol_categories:
            symbol_categories.append(UNKNOWN_SYMBOL_KEY)
        artifact = {
            "model_name": f"universal_anomaly_{engine}_v1",
            "engine": engine,
            "estimator": estimator,
            "feature_names": list(UNIVERSAL_ANOMALY_FEATURE_NAMES),
            "numeric_feature_names": list(UNIVERSAL_ANOMALY_NUMERIC_FEATURE_NAMES),
            "categorical_feature_names": list(UNIVERSAL_ANOMALY_CATEGORICAL_FEATURE_NAMES),
            "feature_vector_format": "dict",
            "symbol_categories": symbol_categories,
            "classes": [str(row) for row in getattr(estimator, "classes_", sorted(class_counts))],
            "horizon_minutes": int(horizon_minutes),
            "sample_count": len(rows),
            "class_counts": class_counts,
            "metrics": metrics,
            "trained_at": now_ms(),
        }
        target_path.parent.mkdir(parents=True, exist_ok=True)
        import joblib

        joblib.dump(artifact, target_path)
        if activate:
            self.runtime_model.activate_trained_artifact(artifact)
        return {
            "ok": True,
            "engine": engine,
            "model": artifact["model_name"],
            "artifact_path": str(target_path),
            "artifact": artifact,
            "feature_names": artifact["feature_names"],
            "numeric_feature_names": artifact["numeric_feature_names"],
            "categorical_feature_names": artifact["categorical_feature_names"],
            "horizon_minutes": artifact["horizon_minutes"],
            "sample_count": artifact["sample_count"],
            "class_counts": class_counts,
            "metrics": metrics,
            "activated": bool(activate),
        }

    def load_artifact(self, artifact_path: str | Path | None = None) -> dict[str, Any]:
        import joblib

        return joblib.load(Path(artifact_path or self.artifact_path))

    def activate_latest(self) -> dict[str, Any]:
        return self.runtime_model.activate_trained_artifact(self.load_artifact())

    def status(self) -> dict[str, Any]:
        artifact_exists = self.artifact_path.exists()
        runtime = self.runtime_model.trained_model_status()
        out = {
            "artifact_path": str(self.artifact_path),
            "artifact_exists": artifact_exists,
            "runtime": runtime,
        }
        if artifact_exists:
            try:
                artifact = self.load_artifact()
                out["artifact"] = {
                    "model": artifact.get("model_name"),
                    "engine": artifact.get("engine"),
                    "horizon_minutes": artifact.get("horizon_minutes"),
                    "sample_count": artifact.get("sample_count"),
                    "class_counts": artifact.get("class_counts", {}),
                    "metrics": artifact.get("metrics", {}),
                    "feature_names": artifact.get("feature_names", []),
                    "numeric_feature_names": artifact.get("numeric_feature_names", []),
                    "categorical_feature_names": artifact.get("categorical_feature_names", []),
                    "symbol_category_count": len(artifact.get("symbol_categories") or []),
                    "trained_at": artifact.get("trained_at", 0),
                }
            except Exception as exc:
                out["artifact_error"] = f"{type(exc).__name__}:{exc}"
        return out

    def predict_features(self, features: dict[str, Any], *, artifact: dict[str, Any] | None = None) -> dict[str, Any]:
        artifact = artifact or self.load_artifact()
        estimator = artifact["estimator"]
        feature_names = artifact["feature_names"]
        if artifact.get("feature_vector_format") == "dict":
            vector = [self._feature_record(features, artifact=artifact)]
        else:
            vector = [[self._float(features.get(name)) for name in feature_names]]
        raw = estimator.predict_proba(vector)[0]
        classes = [str(row) for row in getattr(estimator, "classes_", artifact.get("classes", []))]
        probabilities = {"LONG": 0.0, "SHORT": 0.0, "NEUTRAL": 0.0}
        for label, probability in zip(classes, raw):
            if label in probabilities:
                probabilities[label] = round(float(probability), 4)
        total = sum(probabilities.values())
        if total > 0:
            probabilities = {key: round(value / total, 4) for key, value in probabilities.items()}
        direction = max(probabilities.items(), key=lambda row: row[1])[0]
        return {
            "direction": direction,
            "probabilities": probabilities,
            "engine": artifact.get("engine", ""),
            "model": artifact.get("model_name", ""),
        }

    def _training_rows(self, samples: list[dict[str, Any]]) -> list[dict[str, Any]]:
        rows = []
        for sample in samples:
            features = self._sample_features(sample)
            label = str(sample.get("label_direction") or "").upper()
            if label not in {"LONG", "SHORT", "NEUTRAL"}:
                continue
            if "symbol" in features or "base_asset" in features:
                continue
            rows.append({
                "x": self._feature_record(features),
                "label": label,
                "source_ts_ms": int(self._float(sample.get("source_ts_ms"))),
                "created_at": int(self._float(sample.get("created_at"))),
            })
        return rows

    def _select_engine(self, model_type: str) -> str:
        requested = str(model_type or "auto").lower()
        if requested == "mlp":
            return "mlp"
        if requested == "lightgbm":
            try:
                import lightgbm  # noqa: F401
            except Exception as exc:
                raise RuntimeError(f"lightgbm_unavailable:{type(exc).__name__}:{exc}") from exc
            return "lightgbm"
        try:
            import lightgbm  # noqa: F401

            return "lightgbm"
        except Exception:
            return "mlp"

    def _build_estimator(self, engine: str):
        from sklearn.feature_extraction import DictVectorizer
        from sklearn.pipeline import make_pipeline

        if engine == "lightgbm":
            from lightgbm import LGBMClassifier

            return make_pipeline(
                DictVectorizer(sparse=False),
                LGBMClassifier(
                    objective="multiclass",
                    n_estimators=120,
                    learning_rate=0.05,
                    num_leaves=15,
                    min_child_samples=10,
                    subsample=0.9,
                    colsample_bytree=0.9,
                    random_state=42,
                    verbosity=-1,
                ),
            )
        from sklearn.neural_network import MLPClassifier
        from sklearn.preprocessing import StandardScaler

        return make_pipeline(
            DictVectorizer(sparse=False),
            StandardScaler(),
            MLPClassifier(
                hidden_layer_sizes=(16,),
                solver="lbfgs",
                alpha=0.0005,
                max_iter=1000,
                random_state=42,
            ),
        )

    def _fit_and_score(self, estimator, x: list[Any], y: list[str], class_counts: dict[str, int]) -> dict[str, Any]:
        can_split = len(x) >= 12 and min(class_counts.values()) >= 2
        if can_split:
            test_size = max(len(class_counts), int(round(len(x) * 0.25)))
            test_size = min(test_size, len(x) - 1)
            split_at = len(x) - test_size
            if test_size > 0 and split_at > 0:
                x_train, x_test = x[:split_at], x[split_at:]
                y_train, y_test = y[:split_at], y[split_at:]
                if len(set(y_train)) < 2:
                    estimator.fit(x, y)
                    return {
                        "train_accuracy": round(float(estimator.score(x, y)), 6),
                        "validation_accuracy": None,
                        "validation_samples": 0,
                        "validation_split": "chronological_tail_unavailable_train_class_gap",
                    }
                estimator.fit(x_train, y_train)
                metrics = {
                    "train_accuracy": round(float(estimator.score(x_train, y_train)), 6),
                    "validation_accuracy": round(float(estimator.score(x_test, y_test)), 6),
                    "validation_samples": len(x_test),
                    "validation_split": "chronological_tail",
                }
                estimator.fit(x, y)
                return metrics
        estimator.fit(x, y)
        return {
            "train_accuracy": round(float(estimator.score(x, y)), 6),
            "validation_accuracy": None,
            "validation_samples": 0,
            "validation_split": "none",
        }

    def _float(self, value: Any, default: float = 0.0) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    def _sample_features(self, sample: dict[str, Any]) -> dict[str, Any]:
        features = sample.get("features") if isinstance(sample.get("features"), dict) else {}
        out = dict(features)
        if not out.get("symbol_key"):
            out["symbol_key"] = self._symbol_key(sample.get("symbol"))
        return out

    def _feature_record(self, features: dict[str, Any], *, artifact: dict[str, Any] | None = None) -> dict[str, Any]:
        feature_names = list((artifact or {}).get("feature_names") or UNIVERSAL_ANOMALY_FEATURE_NAMES)
        categorical = set((artifact or {}).get("categorical_feature_names") or UNIVERSAL_ANOMALY_CATEGORICAL_FEATURE_NAMES)
        symbol_categories = set(str(row) for row in ((artifact or {}).get("symbol_categories") or []))
        record: dict[str, Any] = {}
        for name in feature_names:
            if name in categorical:
                value = self._symbol_key(features.get(name))
                if symbol_categories and value not in symbol_categories:
                    value = UNKNOWN_SYMBOL_KEY
                record[name] = value
            else:
                record[name] = self._float(features.get(name))
        return record

    def _symbol_key(self, value: Any) -> str:
        key = str(value or "").strip().upper()
        return key or UNKNOWN_SYMBOL_KEY


universal_anomaly_trainer = UniversalAnomalyClassifierTrainer()
