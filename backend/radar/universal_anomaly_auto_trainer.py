from __future__ import annotations

from pathlib import Path
import time
from typing import Any

from backend.config import settings
from backend.models import now_ms
from backend.radar.universal_anomaly_calibration import universal_anomaly_sample_calibrator
from backend.radar.universal_anomaly_trainer import universal_anomaly_trainer
from backend.radar.universal_anomaly_training import universal_anomaly_training


class UniversalAnomalyAutoTrainer:
    state_key = "universal_anomaly_auto_trainer.state"

    def __init__(self, training=None, trainer=None, calibrator=None):
        self.training = training or universal_anomaly_training
        self.trainer = trainer or universal_anomaly_trainer
        self.calibrator = calibrator or universal_anomaly_sample_calibrator
        self._state: dict[str, Any] = {}
        self.last_result: dict[str, Any] = {}

    def step(
        self,
        *,
        now_ms_value: int | None = None,
        enabled: bool | None = None,
        collect_interval_seconds: int | None = None,
        train_interval_seconds: int | None = None,
        horizon_minutes: int | None = None,
        collect_limit: int | None = None,
        train_limit: int | None = None,
        model_type: str | None = None,
        min_samples: int | None = None,
        min_class_samples: int | None = None,
        min_new_samples: int | None = None,
        min_validation_accuracy: float | None = None,
        min_accuracy_delta: float | None = None,
        max_samples: int | None = None,
        retention_days: int | None = None,
    ) -> dict[str, Any]:
        now_value = int(now_ms_value or now_ms())
        cfg = self._config(
            enabled=enabled,
            collect_interval_seconds=collect_interval_seconds,
            train_interval_seconds=train_interval_seconds,
            horizon_minutes=horizon_minutes,
            collect_limit=collect_limit,
            train_limit=train_limit,
            model_type=model_type,
            min_samples=min_samples,
            min_class_samples=min_class_samples,
            min_new_samples=min_new_samples,
            min_validation_accuracy=min_validation_accuracy,
            min_accuracy_delta=min_accuracy_delta,
            max_samples=max_samples,
            retention_days=retention_days,
        )
        state = self._load_state()
        result: dict[str, Any] = {
            "ok": True,
            "enabled": bool(cfg["enabled"]),
            "now_ms": now_value,
            "collected": False,
            "trained": False,
            "accepted": False,
        }
        if not cfg["enabled"]:
            result["skip_reason"] = "disabled"
            return self._finish(result, state)

        last_collect_ms = int(state.get("last_collect_ms") or 0)
        collect_due = now_value - last_collect_ms >= int(cfg["collect_interval_seconds"]) * 1000
        if collect_due:
            collect_report = self.training.collect(horizon_minutes=cfg["horizon_minutes"], limit=cfg["collect_limit"])
            state["last_collect_ms"] = now_value
            result["collected"] = True
            result["collect_report"] = self._compact_report(collect_report)
            summary = collect_report.get("summary") if isinstance(collect_report, dict) else None
        else:
            result["collect_skip_reason"] = "collect_interval_not_due"
            summary = None
        prune_report = self._prune_samples(cfg, now_value)
        if prune_report:
            result["prune_report"] = prune_report
            if int(prune_report.get("deleted") or 0) > 0:
                summary = None
        if not isinstance(summary, dict):
            summary = self.training.summary()
        result["sample_summary"] = summary

        last_train_ms = int(state.get("last_train_ms") or 0)
        if now_value - last_train_ms < int(cfg["train_interval_seconds"]) * 1000:
            result["train_skip_reason"] = "train_interval_not_due"
            return self._finish(result, state)

        gate_ok, gate_reason, gate_context = self._training_gate(summary, cfg)
        result["gate"] = gate_context
        if not gate_ok:
            result["train_skip_reason"] = gate_reason
            return self._finish(result, state)

        trainer_status = self.trainer.status()
        runtime = trainer_status.get("runtime") if isinstance(trainer_status.get("runtime"), dict) else {}
        current_model = self._current_model_context(trainer_status, runtime)
        current_samples = int(runtime.get("sample_count") or state.get("last_trained_sample_count") or 0)
        total_samples = int(summary.get("total") or 0)
        new_samples = total_samples - current_samples
        result["new_samples_since_runtime"] = new_samples
        if current_samples > 0 and new_samples < int(cfg["min_new_samples"]):
            result["train_skip_reason"] = "not_enough_new_samples"
            return self._finish(result, state)

        calibration_report = self._calibrate_samples(cfg)
        if calibration_report:
            result["calibration_report"] = self._compact_report(calibration_report)

        candidate_path = self._candidate_artifact_path()
        train_report = self.trainer.train(
            horizon_minutes=cfg["horizon_minutes"],
            model_type=cfg["model_type"],
            min_samples=cfg["min_samples"],
            limit=cfg["train_limit"],
            activate=False,
            artifact_path=candidate_path,
        )
        state["last_train_ms"] = now_value
        result["trained"] = True
        result["train_report"] = self._compact_report(train_report)
        if not train_report.get("ok"):
            result["accepted"] = False
            result["reject_reason"] = str(train_report.get("error") or "train_failed")
            return self._finish(result, state)

        accepted, reject_reason = self._accept_candidate(train_report, current_model, cfg)
        if not accepted:
            result["accepted"] = False
            result["reject_reason"] = reject_reason
            return self._finish(result, state)

        artifact = train_report.get("artifact")
        if not isinstance(artifact, dict):
            artifact = self.trainer.load_artifact(candidate_path)
        self._promote_artifact(artifact)
        state["last_trained_sample_count"] = int(train_report.get("sample_count") or total_samples)
        state["last_accepted_ms"] = now_value
        result["accepted"] = True
        result["activated"] = True
        return self._finish(result, state)

    def status(self) -> dict[str, Any]:
        return {
            "state": self._load_state(),
            "last_result": self.last_result,
            "training_summary": self._status_training_summary(),
            "trainer": self.trainer.status(),
            "config": self._config(),
        }

    def _status_training_summary(self) -> dict[str, Any]:
        try:
            return self.training.summary()
        except Exception as exc:
            return {
                "ok": False,
                "error": f"{type(exc).__name__}:{exc}",
                "old_model_kept": True,
                "ts_ms": now_ms(),
            }

    def run_loop(
        self,
        *,
        stop_event=None,
        sleep_seconds: float | None = None,
        max_iterations: int | None = None,
    ) -> None:
        iterations = 0
        while True:
            if stop_event is not None and stop_event.is_set():
                return
            try:
                result = self.step()
                if isinstance(result, dict):
                    self.last_result = self._compact_report(result)
            except Exception as exc:
                self.last_result = {
                    "ok": False,
                    "loop_error": f"{type(exc).__name__}:{exc}",
                    "old_model_kept": True,
                    "ts_ms": now_ms(),
                }
            iterations += 1
            if max_iterations is not None and iterations >= int(max_iterations):
                return
            wait_seconds = self._loop_sleep_seconds(sleep_seconds)
            if stop_event is not None:
                if stop_event.wait(wait_seconds):
                    return
            elif wait_seconds > 0:
                time.sleep(wait_seconds)

    def _training_gate(self, summary: dict[str, Any], cfg: dict[str, Any]) -> tuple[bool, str, dict[str, Any]]:
        total = int(summary.get("total") or 0)
        by_label = summary.get("by_label") if isinstance(summary.get("by_label"), dict) else {}
        class_counts = {label: int(by_label.get(label) or 0) for label in ("LONG", "SHORT", "NEUTRAL")}
        context = {"total": total, "class_counts": class_counts}
        if total < int(cfg["min_samples"]):
            return False, "not_enough_samples", context
        weak = [label for label, count in class_counts.items() if count < int(cfg["min_class_samples"])]
        if weak:
            context["weak_classes"] = weak
            return False, "not_enough_class_samples", context
        return True, "", context

    def _accept_candidate(self, report: dict[str, Any], current_model: dict[str, Any], cfg: dict[str, Any]) -> tuple[bool, str]:
        metrics = report.get("metrics") if isinstance(report.get("metrics"), dict) else {}
        new_accuracy = self._validation_score(metrics)
        if new_accuracy is None:
            return False, "validation_accuracy_missing"
        if new_accuracy < float(cfg["min_validation_accuracy"]):
            return False, "validation_accuracy_below_floor"
        current_metrics = current_model.get("metrics") if isinstance(current_model.get("metrics"), dict) else {}
        current_accuracy = self._validation_score(current_metrics)
        if self._has_balanced_validation_score(metrics) and not self._has_balanced_validation_score(current_metrics):
            if self._is_imbalanced_current_model(current_model):
                current_accuracy = None
        if current_accuracy is not None and new_accuracy < current_accuracy + float(cfg["min_accuracy_delta"]):
            return False, "validation_accuracy_below_current"
        return True, ""

    def _current_model_context(self, trainer_status: dict[str, Any], runtime: dict[str, Any]) -> dict[str, Any]:
        context = dict(runtime)
        artifact = trainer_status.get("artifact") if isinstance(trainer_status.get("artifact"), dict) else {}
        if artifact:
            context["artifact"] = artifact
            if not isinstance(context.get("metrics"), dict) and isinstance(artifact.get("metrics"), dict):
                context["metrics"] = artifact["metrics"]
            if "class_counts" not in context and isinstance(artifact.get("class_counts"), dict):
                context["class_counts"] = artifact["class_counts"]
        return context

    def _validation_score(self, metrics: dict[str, Any]) -> float | None:
        balanced = self._optional_float(metrics.get("validation_balanced_accuracy"))
        if balanced is not None:
            return balanced
        return self._optional_float(metrics.get("validation_accuracy"))

    def _has_balanced_validation_score(self, metrics: dict[str, Any]) -> bool:
        return self._optional_float(metrics.get("validation_balanced_accuracy")) is not None

    def _is_imbalanced_current_model(self, current_model: dict[str, Any]) -> bool:
        class_counts = current_model.get("class_counts") if isinstance(current_model.get("class_counts"), dict) else {}
        if not class_counts:
            artifact = current_model.get("artifact") if isinstance(current_model.get("artifact"), dict) else {}
            class_counts = artifact.get("class_counts") if isinstance(artifact.get("class_counts"), dict) else {}
        counts = [int(value or 0) for value in class_counts.values()]
        total = sum(counts)
        if total <= 0:
            return False
        return max(counts) / total >= 0.90

    def _promote_artifact(self, artifact: dict[str, Any]) -> None:
        import joblib

        path = Path(self.trainer.artifact_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        joblib.dump(artifact, path)
        self.trainer.runtime_model.activate_trained_artifact(artifact)

    def _candidate_artifact_path(self) -> Path:
        path = Path(self.trainer.artifact_path)
        return path.with_name(path.stem + ".candidate" + path.suffix)

    def _config(self, **overrides) -> dict[str, Any]:
        cfg = {
            "enabled": settings.universal_anomaly_auto_train_enabled,
            "collect_interval_seconds": settings.universal_anomaly_collect_interval_seconds,
            "train_interval_seconds": settings.universal_anomaly_train_interval_seconds,
            "horizon_minutes": settings.universal_anomaly_horizon_minutes,
            "collect_limit": settings.universal_anomaly_collect_limit,
            "train_limit": settings.universal_anomaly_train_limit,
            "model_type": settings.universal_anomaly_model_type,
            "min_samples": settings.universal_anomaly_min_samples,
            "min_class_samples": settings.universal_anomaly_min_class_samples,
            "min_new_samples": settings.universal_anomaly_min_new_samples,
            "min_validation_accuracy": settings.universal_anomaly_min_validation_accuracy,
            "min_accuracy_delta": settings.universal_anomaly_min_accuracy_delta,
            "max_samples": settings.universal_anomaly_max_samples,
            "retention_days": settings.universal_anomaly_retention_days,
        }
        for key, value in overrides.items():
            if value is not None:
                cfg[key] = value
        cfg["enabled"] = bool(cfg["enabled"])
        cfg["collect_interval_seconds"] = max(0, int(cfg["collect_interval_seconds"]))
        cfg["train_interval_seconds"] = max(0, int(cfg["train_interval_seconds"]))
        cfg["horizon_minutes"] = max(1, int(cfg["horizon_minutes"]))
        cfg["collect_limit"] = max(1, int(cfg["collect_limit"]))
        cfg["train_limit"] = max(1, int(cfg["train_limit"]))
        cfg["min_samples"] = max(1, int(cfg["min_samples"]))
        cfg["min_class_samples"] = max(1, int(cfg["min_class_samples"]))
        cfg["min_new_samples"] = max(0, int(cfg["min_new_samples"]))
        cfg["min_validation_accuracy"] = float(cfg["min_validation_accuracy"])
        cfg["min_accuracy_delta"] = float(cfg["min_accuracy_delta"])
        cfg["max_samples"] = max(1, int(cfg["max_samples"]))
        cfg["retention_days"] = max(0, int(cfg["retention_days"]))
        cfg["model_type"] = str(cfg["model_type"] or "auto")
        return cfg

    def _prune_samples(self, cfg: dict[str, Any], now_value: int) -> dict[str, Any]:
        database = self._database()
        prune = getattr(database, "prune_universal_anomaly_samples", None)
        if not callable(prune):
            return {}
        try:
            return prune(
                max_samples=cfg["max_samples"],
                retention_days=cfg["retention_days"],
                now_ms_value=now_value,
            )
        except Exception as exc:
            return {"ok": False, "error": f"{type(exc).__name__}:{exc}", "old_model_kept": True}

    def _calibrate_samples(self, cfg: dict[str, Any]) -> dict[str, Any]:
        calibrate = getattr(self.calibrator, "calibrate", None)
        if not callable(calibrate):
            return {}
        try:
            return calibrate(
                horizon_minutes=cfg["horizon_minutes"],
                limit=cfg["train_limit"],
                repair=True,
            )
        except Exception as exc:
            return {"ok": False, "error": f"{type(exc).__name__}:{exc}", "old_model_kept": True}

    def _loop_sleep_seconds(self, override: float | None) -> float:
        if override is not None:
            return max(0.0, float(override))
        cfg = self._config()
        return max(1.0, float(cfg["collect_interval_seconds"]))

    def _load_state(self) -> dict[str, Any]:
        database = self._database()
        if database is not None:
            try:
                state = database.get_kv(self.state_key, {})
                if isinstance(state, dict):
                    self._state = dict(state)
            except Exception:
                pass
        return dict(self._state)

    def _save_state(self, state: dict[str, Any]) -> None:
        self._state = dict(state)
        database = self._database()
        if database is not None:
            try:
                database.set_kv(self.state_key, self._state)
            except Exception:
                pass

    def _database(self):
        return getattr(self.training, "database", None) or getattr(self.trainer, "database", None)

    def _finish(self, result: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        self._save_state(state)
        self.last_result = self._compact_report(result)
        return self.last_result

    def _compact_report(self, report: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(report, dict):
            return {}
        return {key: value for key, value in report.items() if key != "artifact"}

    def _optional_float(self, value: Any) -> float | None:
        try:
            return float(value)
        except (TypeError, ValueError):
            return None


universal_anomaly_auto_trainer = UniversalAnomalyAutoTrainer()


def run_auto_train_loop(stop_event=None) -> None:
    universal_anomaly_auto_trainer.run_loop(stop_event=stop_event)
