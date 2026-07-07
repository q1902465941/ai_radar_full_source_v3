from __future__ import annotations

import time
import json
import importlib.util
import shutil
from collections import Counter
from pathlib import Path
from typing import Any

from backend.config import settings
from backend.learning.replay_memory import replay_memory
from backend.learning.strategy_registry import strategy_registry
from backend.learning.trade_memory import is_learning_close_reason, trade_memory
from backend.storage.db import db


class LearningDataAudit:
    """Audits whether learning samples are production-grade evidence.

    Replay samples are useful for shadow calibration, but they are not the same
    as candle-level market backtests or real closed trades. This module keeps
    that distinction visible to readiness and risk gates.
    """

    def __init__(self) -> None:
        self._cache_until = 0.0
        self._cache: dict[str, Any] = {}

    def clear_cache(self) -> None:
        self._cache_until = 0.0
        self._cache = {}

    def cached_summary(self) -> dict[str, Any]:
        if self._cache:
            return self._cache
        return {
            "production_grade": False,
            "trust_level": "UNKNOWN",
            "can_hard_block_from_learning": False,
            "reasons": ["learning_data_audit_cache_empty"],
            "sources": {},
            "market_backtest": {"available": False, "quality_passed": False},
            "learning_reset_at_ms": 0,
            "instruction": "Refresh /api/learning/data-audit before treating production acceptance as current.",
        }

    def summary(self, *, force: bool = False, limit: int | None = None) -> dict[str, Any]:
        now = time.time()
        if not force and now < self._cache_until and self._cache:
            return self._cache

        sample_limit = max(100, int(limit or settings.trade_attribution_sample_limit))
        replay_samples = replay_memory.samples(limit=sample_limit) if settings.replay_enabled else []
        closed_samples = trade_memory.samples(limit=sample_limit, require_radar=True)
        raw_closed_rows = db.list_closed(limit=100000)
        closed_total = sum(1 for row in raw_closed_rows if is_learning_close_reason(row.get("close_reason")))
        excluded_close_reason_counts = self._excluded_close_reason_counts(raw_closed_rows)
        closed_sample_providers = self._closed_sample_provider_counts(closed_samples)
        radar = self._radar_summary()
        reset_at_ms = self._learning_reset_at_ms()
        market_backtest = self._apply_learning_reset_to_market_backtest(self._market_backtest_summary(), reset_at_ms)

        replay_count = len(replay_samples)
        closed_count = len(closed_samples)
        combined_count = replay_count + closed_count
        replay_ratio = replay_count / max(1, combined_count)
        min_closed = max(30, int(settings.trade_attribution_min_samples) * 3)
        min_radar_days = 14.0
        market_backtest_passed = bool(market_backtest.get("quality_passed"))
        market_span_days = float(market_backtest.get("span_days") or 0.0)
        evidence_span_days = max(float(radar.get("span_days") or 0.0), market_span_days)

        reasons: list[str] = []
        if closed_count < min_closed and not market_backtest_passed:
            reasons.append("real_closed_samples_low")
        if replay_count > 0 and replay_ratio >= 0.80 and not market_backtest_passed:
            reasons.append("replay_dominated")
        if not market_backtest["available"]:
            reasons.append("market_backtest_missing")
            reasons.extend(market_backtest.get("missing_reasons") or [])
        elif not market_backtest_passed:
            reasons.append("market_backtest_not_passing")
            reasons.extend(market_backtest.get("quality_blockers") or [])
        if not bool(radar.get("ok", True)):
            reasons.append("radar_snapshot_audit_unavailable")
        if evidence_span_days < min_radar_days:
            reasons.append("radar_history_short")

        production_grade = not reasons
        if production_grade:
            trust_level = "PRODUCTION"
        elif closed_count >= min_closed or market_backtest["available"]:
            trust_level = "MEDIUM"
        else:
            trust_level = "LOW"

        report = {
            "production_grade": production_grade,
            "trust_level": trust_level,
            "can_hard_block_from_learning": production_grade,
            "reasons": reasons,
            "minimums": {
                "real_closed_samples": min_closed,
                "radar_history_days": min_radar_days,
                "requires_market_backtest": True,
                "max_replay_ratio_for_production": 0.80,
                "market_backtest_trades": int(settings.evolve_min_backtest_trades),
                "market_backtest_holdout_trades": int(settings.evolve_min_holdout_trades),
                "market_backtest_win_rate": float(settings.evolve_min_win_rate),
                "market_backtest_holdout_win_rate": float(settings.evolve_min_holdout_win_rate),
                "market_backtest_profit_factor": float(settings.evolve_min_profit_factor),
                "market_backtest_net_pnl_r": float(settings.evolve_min_net_pnl),
            },
            "sources": {
                "combined_samples": combined_count,
                "replay_samples": replay_count,
                "real_closed_samples_with_radar": closed_count,
                "codex_real_closed_samples_with_radar": int(closed_sample_providers.get("codex_cli") or 0),
                "real_closed_samples_by_provider": closed_sample_providers,
                "closed_trades_total": closed_total,
                "raw_closed_trades_total": len(raw_closed_rows),
                "excluded_closed_trades": len(raw_closed_rows) - closed_total,
                "closed_without_radar_samples": max(0, closed_total - closed_count),
                "excluded_close_reason_counts": excluded_close_reason_counts,
                "replay_ratio": round(replay_ratio, 4),
            },
            "metrics": {
                "replay": self._metrics(replay_samples),
                "real_closed": self._metrics(closed_samples),
            },
            "learning_reset_at_ms": reset_at_ms,
            "radar_snapshots": radar,
            "market_backtest": market_backtest,
            "instruction": (
                "LOW/MEDIUM trust learning data may guide review and shadow validation, "
                "but must not be presented as production-grade backtest evidence."
            ),
        }
        self._cache = report
        self._cache_until = now + max(1, int(settings.event_calibration_ttl_seconds))
        return report

    def _closed_sample_provider_counts(self, closed_samples: list[dict[str, Any]]) -> dict[str, int]:
        strategies = self._strategy_index()
        counts: Counter[str] = Counter()
        for sample in closed_samples:
            counts[self._sample_provider(sample, strategies)] += 1
        return dict(sorted(counts.items()))

    def _sample_provider(self, sample: dict[str, Any], strategies: dict[str, dict[str, Any]]) -> str:
        strategy_id = str(sample.get("strategy_id") or "")
        strategy = strategies.get(strategy_id) or {}
        provider = str(sample.get("provider") or strategy.get("provider") or "").strip().lower()
        if not provider:
            source = str(strategy.get("source") or "").strip().lower()
            if source.startswith("ai_generated_"):
                provider = source[len("ai_generated_") :]
        if not provider:
            contract = sample.get("strategy_contract") if isinstance(sample.get("strategy_contract"), dict) else {}
            provider = str(contract.get("provider") or contract.get("model_provider") or "").strip().lower()
        return provider or "unknown"

    def _strategy_index(self) -> dict[str, dict[str, Any]]:
        try:
            strategies = strategy_registry.list(limit=10000)
        except Exception:
            return {}
        return {
            str(strategy.get("strategy_id") or ""): strategy
            for strategy in strategies
            if isinstance(strategy, dict) and str(strategy.get("strategy_id") or "")
        }

    def _excluded_close_reason_counts(self, rows: list[dict[str, Any]]) -> dict[str, int]:
        counts: Counter[str] = Counter()
        for row in rows:
            reason = str(row.get("close_reason") or "unknown")
            if not is_learning_close_reason(reason):
                counts[reason] += 1
        return dict(sorted(counts.items()))

    def compact(self) -> dict[str, Any]:
        report = self.summary()
        return {
            "production_grade": report["production_grade"],
            "trust_level": report["trust_level"],
            "can_hard_block_from_learning": report["can_hard_block_from_learning"],
            "reasons": report["reasons"],
            "sources": report["sources"],
            "market_backtest": report["market_backtest"],
            "learning_reset_at_ms": report.get("learning_reset_at_ms", 0),
        }

    def _radar_summary(self) -> dict[str, Any]:
        try:
            with db.conn() as conn:
                row = conn.execute(
                    """
                    SELECT
                        COUNT(*) AS rows,
                        COUNT(DISTINCT scan_id) AS scans,
                        COUNT(DISTINCT symbol) AS symbols,
                        MIN(ts_ms) AS min_ts,
                        MAX(ts_ms) AS max_ts
                    FROM radar_snapshots
                    """
                ).fetchone()
        except Exception as exc:
            return {
                "ok": False,
                "error": f"{type(exc).__name__}:{exc}",
                "rows": 0,
                "distinct_scans": 0,
                "distinct_symbols": 0,
                "min_ts_ms": 0,
                "max_ts_ms": 0,
                "span_days": 0.0,
            }
        min_ts = int(row["min_ts"] or 0)
        max_ts = int(row["max_ts"] or 0)
        span_ms = max(0, max_ts - min_ts)
        return {
            "ok": True,
            "rows": int(row["rows"] or 0),
            "distinct_scans": int(row["scans"] or 0),
            "distinct_symbols": int(row["symbols"] or 0),
            "min_ts_ms": min_ts,
            "max_ts_ms": max_ts,
            "span_days": round(span_ms / 86_400_000.0, 4),
        }

    def _market_backtest_summary(self) -> dict[str, Any]:
        table_error = ""
        try:
            with db.conn() as conn:
                table_rows = conn.execute(
                    """
                    SELECT name FROM sqlite_master
                    WHERE type='table'
                      AND (
                        lower(name) LIKE '%candle%'
                        OR lower(name) LIKE '%kline%'
                        OR lower(name) LIKE '%ohlc%'
                        OR lower(name) LIKE '%backtest%'
                      )
                    ORDER BY name
                    """
                ).fetchall()
            tables = [str(row["name"]) for row in table_rows]
        except Exception as exc:
            tables = []
            table_error = f"{type(exc).__name__}:{exc}"
        root = Path(__file__).resolve().parents[2]
        reports_dir = root / "trading_lab" / "reports"
        reports = [
            str(path.relative_to(root))
            for path in reports_dir.glob("*")
            if path.is_file() and path.name not in {".gitkeep", "README.md"}
        ] if reports_dir.exists() else []
        latest_report_path = self._latest_market_backtest_report(reports_dir) if reports_dir.exists() else None
        latest_report = self._read_json(latest_report_path) if latest_report_path else {}
        quality = self._market_report_quality(latest_report)
        jesse_path = Path(settings.jesse_data_path)
        jesse_installed = bool(importlib.util.find_spec("jesse"))
        jesse_cli = shutil.which("jesse") or ""
        jesse_files = self._has_any_file(jesse_path) if jesse_path.exists() else False
        available = bool(latest_report_path)
        missing_reasons = []
        if not available:
            if not settings.jesse_research_enabled:
                missing_reasons.append("jesse_research_disabled")
            if not jesse_installed:
                missing_reasons.append("jesse_not_installed")
            elif not jesse_cli:
                missing_reasons.append("jesse_cli_missing")
            if not jesse_path.exists() or not jesse_files:
                missing_reasons.append("jesse_data_missing")
            if not reports:
                missing_reasons.append("market_backtest_report_missing")
        return {
            "available": available,
            "quality_passed": bool(quality.get("passed")),
            "quality_blockers": quality.get("blockers") or [],
            "latest_report": str(latest_report_path.relative_to(root)) if latest_report_path else "",
            "latest_report_generated_at": latest_report.get("generated_at") if isinstance(latest_report, dict) else "",
            "generated_at_ms": int(_f(latest_report.get("generated_at_ms"))) if isinstance(latest_report, dict) else 0,
            "span_days": quality.get("span_days", 0.0),
            "metrics": quality.get("metrics") or {},
            "holdout_metrics": quality.get("holdout_metrics") or {},
            "by_side_metrics": quality.get("by_side_metrics") or {},
            "side_blocks": quality.get("side_blocks") or [],
            "candle_or_backtest_tables": tables[:10],
            "candle_or_backtest_table_error": table_error,
            "research_reports": reports[:10],
            "jesse_research_enabled": bool(settings.jesse_research_enabled),
            "jesse_installed": jesse_installed,
            "jesse_cli_found": bool(jesse_cli),
            "jesse_cli_path": jesse_cli,
            "jesse_data_path_exists": jesse_path.exists(),
            "jesse_data_has_files": jesse_files,
            "missing_reasons": missing_reasons,
            "detected_engine": "none" if not available else str(latest_report.get("engine") or "market_backtest"),
        }

    def _learning_reset_at_ms(self) -> int:
        try:
            return int(_f(db.get_kv("learning_data_reset_at_ms", 0)))
        except Exception:
            return 0

    def _apply_learning_reset_to_market_backtest(self, report: dict[str, Any], reset_at_ms: int) -> dict[str, Any]:
        if reset_at_ms <= 0:
            return report
        out = dict(report)
        out["learning_reset_at_ms"] = reset_at_ms
        if not out.get("available"):
            return out
        generated_at_ms = int(_f(out.get("generated_at_ms")))
        blocker = ""
        if generated_at_ms <= 0:
            blocker = "market_backtest_timestamp_missing_after_learning_reset"
        elif generated_at_ms < reset_at_ms:
            blocker = "market_backtest_before_learning_reset"
        if blocker:
            blockers = sorted(set([*(out.get("quality_blockers") or []), blocker]))
            out["quality_passed"] = False
            out["quality_blockers"] = blockers
        return out

    def _latest_market_backtest_report(self, reports_dir: Path) -> Path | None:
        candidates = [
            path
            for path in reports_dir.glob("market_backtest*.json")
            if path.is_file() and path.name != "market_backtest_latest_trades.json"
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda path: path.stat().st_mtime)

    def _read_json(self, path: Path | None) -> dict[str, Any]:
        if path is None:
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {}
        return data if isinstance(data, dict) else {}

    def _market_report_quality(self, report: dict[str, Any]) -> dict[str, Any]:
        if not report:
            return {"passed": False, "blockers": ["market_backtest_report_unreadable"]}

        metrics_root = report.get("metrics") if isinstance(report.get("metrics"), dict) else {}
        overall = metrics_root.get("overall") if isinstance(metrics_root.get("overall"), dict) else {}
        holdout = metrics_root.get("holdout") if isinstance(metrics_root.get("holdout"), dict) else {}
        validation = report.get("validation") if isinstance(report.get("validation"), dict) else {}
        blockers = list(validation.get("blockers") or [])

        trades = int(_f(overall.get("trades") or overall.get("sample_count")))
        holdout_trades = int(_f(holdout.get("trades") or holdout.get("sample_count")))
        win_rate = _f(overall.get("win_rate"))
        holdout_win_rate = _f(holdout.get("win_rate"))
        profit_factor = _f(overall.get("profit_factor"))
        net_pnl = _f(overall.get("net_pnl_r") or overall.get("pnl_r") or overall.get("pnl"))
        holdout_net_pnl = _f(holdout.get("net_pnl_r") or holdout.get("pnl_r") or holdout.get("pnl"))

        if trades < int(settings.evolve_min_backtest_trades):
            blockers.append("market_backtest_trades_low")
        if holdout_trades < int(settings.evolve_min_holdout_trades):
            blockers.append("market_backtest_holdout_trades_low")
        if win_rate < float(settings.evolve_min_win_rate):
            blockers.append("market_backtest_win_rate_low")
        if holdout_win_rate < float(settings.evolve_min_holdout_win_rate):
            blockers.append("market_backtest_holdout_win_rate_low")
        if profit_factor < float(settings.evolve_min_profit_factor):
            blockers.append("market_backtest_profit_factor_low")
        if net_pnl <= float(settings.evolve_min_net_pnl):
            blockers.append("market_backtest_net_pnl_not_positive")
        if holdout_trades > 0 and holdout_net_pnl <= 0:
            blockers.append("market_backtest_holdout_pnl_not_positive")

        report_passed = bool(validation.get("passed", report.get("passed", False)))
        blockers = sorted(set(str(item) for item in blockers if item))
        return {
            "passed": bool(report_passed and not blockers),
            "blockers": blockers,
            "span_days": _f(((report.get("data") or {}).get("span_days") if isinstance(report.get("data"), dict) else 0.0)),
            "metrics": overall,
            "holdout_metrics": holdout,
            "by_side_metrics": metrics_root.get("by_side") if isinstance(metrics_root.get("by_side"), dict) else {},
            "side_blocks": ((report.get("market_guard") or {}).get("side_blocks") if isinstance(report.get("market_guard"), dict) else []),
        }

    def _has_any_file(self, path: Path) -> bool:
        try:
            for child in path.rglob("*"):
                if child.is_file():
                    return True
        except Exception:
            return False
        return False

    def _metrics(self, samples: list[dict[str, Any]]) -> dict[str, Any]:
        pnls = [_f(sample.get("pnl")) for sample in samples if _f(sample.get("pnl")) != 0.0]
        wins = [pnl for pnl in pnls if pnl > 0]
        losses = [pnl for pnl in pnls if pnl < 0]
        gross_profit = sum(wins)
        gross_loss = abs(sum(losses))
        return {
            "samples": len(pnls),
            "win_rate": round(len(wins) / max(1, len(wins) + len(losses)), 4),
            "profit_factor": round(gross_profit / gross_loss, 4) if gross_loss > 0 else (999.0 if gross_profit > 0 else 0.0),
            "pnl": round(sum(pnls), 4),
        }


def _f(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


learning_data_audit = LearningDataAudit()
