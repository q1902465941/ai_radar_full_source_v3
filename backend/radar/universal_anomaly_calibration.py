from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any

from backend.radar.universal_anomaly_model import (
    UNKNOWN_SYMBOL_KEY,
    UNIVERSAL_ANOMALY_NUMERIC_FEATURE_NAMES,
)
from backend.storage.db import db


class UniversalAnomalySampleCalibrator:
    def __init__(self, database=None):
        self.database = database or db

    def calibrate(
        self,
        *,
        horizon_minutes: int = 5,
        limit: int = 5000,
        repair: bool = False,
        min_symbol_samples: int = 5,
        neutral_rate_warn: float = 0.75,
        dominance_warn: float = 0.85,
    ) -> dict[str, Any]:
        horizon = max(1, int(horizon_minutes))
        sample_limit = max(1, int(limit))
        symbol_floor = max(1, int(min_symbol_samples))
        neutral_warn = min(1.0, max(0.0, float(neutral_rate_warn)))
        dominance_threshold = min(1.0, max(0.0, float(dominance_warn)))
        samples = self.database.list_universal_anomaly_samples(limit=sample_limit, horizon_minutes=horizon)

        label_counts: Counter[str] = Counter()
        feature_missing_counts: Counter[str] = Counter()
        symbol_key_missing = 0
        symbol_key_mismatch = 0
        identifier_leak_count = 0
        invalid_label_count = 0
        zero_return_neutral = 0
        repaired_samples: list[dict[str, Any]] = []
        by_symbol: dict[str, dict[str, Any]] = defaultdict(
            lambda: {
                "sample_count": 0,
                "label_counts": Counter(),
                "missing_feature_count": 0,
                "symbol_key_issues": 0,
                "identifier_leaks": 0,
                "invalid_labels": 0,
            }
        )

        for sample in samples:
            symbol = self._symbol(sample.get("symbol"))
            features = sample.get("features") if isinstance(sample.get("features"), dict) else {}
            expected_symbol_key = symbol
            actual_symbol_key = self._symbol(features.get("symbol_key"))
            symbol_issue = actual_symbol_key != expected_symbol_key
            if not features.get("symbol_key"):
                symbol_key_missing += 1
            if symbol_issue:
                symbol_key_mismatch += 1
                by_symbol[symbol]["symbol_key_issues"] += 1
                if repair:
                    patched = dict(sample)
                    patched_features = dict(features)
                    patched_features["symbol_key"] = expected_symbol_key
                    patched["features"] = patched_features
                    repaired_samples.append(patched)

            missing_for_sample = 0
            for name in UNIVERSAL_ANOMALY_NUMERIC_FEATURE_NAMES:
                if name not in features or not self._is_number(features.get(name)):
                    feature_missing_counts[name] += 1
                    missing_for_sample += 1
            if "symbol" in features or "base_asset" in features:
                identifier_leak_count += 1
                by_symbol[symbol]["identifier_leaks"] += 1

            label = str(sample.get("label_direction") or "").upper()
            if label not in {"LONG", "SHORT", "NEUTRAL"}:
                invalid_label_count += 1
                by_symbol[symbol]["invalid_labels"] += 1
            else:
                label_counts[label] += 1
                by_symbol[symbol]["label_counts"][label] += 1
                if label == "NEUTRAL" and abs(self._float(sample.get("label_return_pct"))) <= 1e-12:
                    zero_return_neutral += 1

            by_symbol[symbol]["sample_count"] += 1
            by_symbol[symbol]["missing_feature_count"] += missing_for_sample

        repaired_symbol_key = 0
        if repair and repaired_samples:
            update = getattr(self.database, "update_universal_anomaly_sample_payloads", None)
            if callable(update):
                repaired_symbol_key = int(update(repaired_samples))

        symbol_reports = self._symbol_reports(
            by_symbol=by_symbol,
            min_symbol_samples=symbol_floor,
            neutral_rate_warn=neutral_warn,
            dominance_warn=dominance_threshold,
        )
        warnings = self._top_level_warnings(
            total=len(samples),
            symbol_key_mismatch=symbol_key_mismatch,
            feature_missing_counts=feature_missing_counts,
            identifier_leak_count=identifier_leak_count,
            invalid_label_count=invalid_label_count,
            symbol_reports=symbol_reports,
        )
        return {
            "ok": True,
            "horizon_minutes": horizon,
            "limit": sample_limit,
            "repair": bool(repair),
            "total": len(samples),
            "label_counts": dict(label_counts),
            "symbol_count": len(symbol_reports),
            "symbol_key_missing": symbol_key_missing,
            "symbol_key_mismatch": symbol_key_mismatch,
            "repaired_symbol_key": repaired_symbol_key,
            "feature_missing_counts": dict(feature_missing_counts),
            "identifier_leak_count": identifier_leak_count,
            "invalid_label_count": invalid_label_count,
            "zero_return_neutral": zero_return_neutral,
            "warnings": warnings,
            "symbol_reports": symbol_reports,
            "worst_symbols": self._worst_symbols(symbol_reports),
        }

    def _symbol_reports(
        self,
        *,
        by_symbol: dict[str, dict[str, Any]],
        min_symbol_samples: int,
        neutral_rate_warn: float,
        dominance_warn: float,
    ) -> dict[str, dict[str, Any]]:
        reports: dict[str, dict[str, Any]] = {}
        for symbol in sorted(by_symbol):
            data = by_symbol[symbol]
            count = int(data["sample_count"])
            labels = dict(data["label_counts"])
            valid_label_total = sum(labels.values())
            neutral_rate = (labels.get("NEUTRAL", 0) / valid_label_total) if valid_label_total else 0.0
            dominant_label = ""
            dominant_rate = 0.0
            if valid_label_total:
                dominant_label, dominant_count = max(labels.items(), key=lambda row: row[1])
                dominant_rate = dominant_count / valid_label_total
            warnings: list[str] = []
            if count < min_symbol_samples:
                warnings.append("sample_count_below_floor")
            if valid_label_total >= min_symbol_samples and neutral_rate >= neutral_rate_warn:
                warnings.append("neutral_rate_high")
            if valid_label_total >= min_symbol_samples and dominant_rate >= dominance_warn:
                warnings.append("label_dominance_high")
            if data["symbol_key_issues"]:
                warnings.append("symbol_key_mismatch")
            if data["missing_feature_count"]:
                warnings.append("missing_numeric_features")
            if data["identifier_leaks"]:
                warnings.append("identifier_leak")
            if data["invalid_labels"]:
                warnings.append("invalid_label")
            reports[symbol] = {
                "sample_count": count,
                "label_counts": labels,
                "neutral_rate": round(neutral_rate, 6),
                "dominant_label": dominant_label,
                "dominant_rate": round(dominant_rate, 6),
                "missing_feature_count": int(data["missing_feature_count"]),
                "symbol_key_issues": int(data["symbol_key_issues"]),
                "identifier_leaks": int(data["identifier_leaks"]),
                "invalid_labels": int(data["invalid_labels"]),
                "warnings": warnings,
            }
        return reports

    def _top_level_warnings(
        self,
        *,
        total: int,
        symbol_key_mismatch: int,
        feature_missing_counts: Counter[str],
        identifier_leak_count: int,
        invalid_label_count: int,
        symbol_reports: dict[str, dict[str, Any]],
    ) -> list[str]:
        warnings: list[str] = []
        if total == 0:
            warnings.append("no_samples")
        if symbol_key_mismatch:
            warnings.append("symbol_key_mismatch")
        if feature_missing_counts:
            warnings.append("missing_numeric_features")
        if identifier_leak_count:
            warnings.append("identifier_leak")
        if invalid_label_count:
            warnings.append("invalid_label")
        if any("sample_count_below_floor" in row["warnings"] for row in symbol_reports.values()):
            warnings.append("thin_symbol_groups")
        if any("neutral_rate_high" in row["warnings"] for row in symbol_reports.values()):
            warnings.append("neutral_heavy_symbols")
        if any("label_dominance_high" in row["warnings"] for row in symbol_reports.values()):
            warnings.append("label_dominant_symbols")
        return warnings

    def _worst_symbols(self, symbol_reports: dict[str, dict[str, Any]], limit: int = 20) -> list[dict[str, Any]]:
        rows = [
            {"symbol": symbol, **report}
            for symbol, report in symbol_reports.items()
            if report.get("warnings")
        ]
        rows.sort(key=lambda row: (-len(row["warnings"]), row["sample_count"], row["symbol"]))
        return rows[: max(1, int(limit))]

    def _symbol(self, value: Any) -> str:
        symbol = str(value or "").strip().upper()
        return symbol or UNKNOWN_SYMBOL_KEY

    def _float(self, value: Any) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return 0.0

    def _is_number(self, value: Any) -> bool:
        try:
            float(value)
            return True
        except (TypeError, ValueError):
            return False


universal_anomaly_sample_calibrator = UniversalAnomalySampleCalibrator()
