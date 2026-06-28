from __future__ import annotations

import json
from pathlib import Path
import subprocess
import tempfile
from typing import Any

from backend.ai_strategy.codex_cli_strategy_client import (
    codex_provider_config_args,
    default_codex_command,
    normalized_codex_service_tier,
    normalized_reasoning_effort,
    run_command,
)
from backend.config import settings
from backend.learning.backtest_engine import backtest_engine
from backend.learning.learning_data_audit import learning_data_audit
from backend.learning.replay_memory import replay_memory
from backend.learning.strategy_filter import direction_confirmations
from backend.learning.strategy_registry import strategy_registry
from backend.learning.trade_memory import trade_memory
from backend.models import new_id, now_ms


CODEX_EVOLVE_PROMPT = """You are evolving reusable crypto radar strategy filters.
Return JSON only, matching the schema. Do not output Markdown.

Goal:
- Propose reusable strategy filter blueprints, not individual trades.
- Prefer fewer high-quality trades over many weak trades.
- Avoid filters that historically feed stop-losses.
- A strategy can be promoted only after local backtest and holdout validation.

Historical summary:
{summary_json}

Filter fields you may use:
min_score, min_fund_confirm, allowed_fake_risks, min_direction_confirmations,
min_volume_spike, max_wick_ratio, require_oi_positive,
require_timeframe_alignment, require_taker_alignment,
require_depth_alignment, require_sm_delta_alignment, allowed_sides.
"""


class StrategyEvolver:
    def evolve(self, use_codex: bool = False, promote: bool = True) -> dict[str, Any]:
        replay_samples = replay_memory.samples() if settings.replay_enabled else []
        trade_samples = trade_memory.samples(limit=10000, require_radar=True)
        samples = _combine_samples(replay_samples, trade_samples)
        train_samples, holdout_samples = _train_holdout_split(samples)
        memory_summary = trade_memory.summary()
        replay_summary = replay_memory.summary() if settings.replay_enabled else {"replay_samples": 0}
        candidates = self._data_driven_candidates(train_samples)
        if use_codex:
            candidates.extend(self._codex_candidates({"trade_memory": memory_summary, "replay_memory": replay_summary}))
        candidates = candidates[: max(1, settings.evolve_max_candidates)]

        scored = []
        for candidate in candidates:
            metrics = backtest_engine.evaluate(candidate, samples)
            record = {
                **candidate,
                "metrics": metrics,
                "status": "PASS" if metrics["eligible"] else "REJECTED",
            }
            scored.append(record)

        scored.sort(key=self._score_key, reverse=True)
        best = scored[0] if scored else None
        promoted = None
        promotion_blocked = ""
        stored = []
        for record in scored[: max(1, settings.evolve_store_top_n)]:
            stored_record = strategy_registry.save(record)
            stored.append(stored_record)

        if promote and best and best["metrics"]["eligible"]:
            data_quality = learning_data_audit.summary()
            if data_quality.get("production_grade"):
                promoted = strategy_registry.activate(best["strategy_id"])
            else:
                promotion_blocked = "learning_data_not_production_grade"

        run = strategy_registry.save_run(
            {
                "run_id": new_id("evolve"),
                "created_at": now_ms(),
                "use_codex": use_codex,
                "promote_requested": promote,
                "memory": memory_summary,
                "replay": replay_summary,
                "sample_source": _sample_source(replay_samples, trade_samples),
                "sample_count": len(samples),
                "train_sample_count": len(train_samples),
                "holdout_sample_count": len(holdout_samples),
                "candidate_count": len(candidates),
                "stored_count": len(stored),
                "best": best,
                "promoted": promoted,
                "promotion_blocked": promotion_blocked,
            }
        )
        return {
            "ok": True,
            "run": run,
            "active": strategy_registry.active(),
            "top": stored,
        }

    def _data_driven_candidates(self, samples: list[dict[str, Any]]) -> list[dict[str, Any]]:
        candidates: list[dict[str, Any]] = []
        side_sets = [["LONG"], ["SHORT"], ["LONG", "SHORT"]]
        fake_sets = [["LOW"]]
        profiles = [
            ("balanced", 0.25, 0.25, 0.20, 0.80),
            ("precision", 0.45, 0.40, 0.35, 0.65),
            ("recovery", 0.65, 0.55, 0.50, 0.55),
        ]
        seen: set[str] = set()
        for sides in side_sets:
            side_samples = [s for s in samples if s.get("side") in sides]
            if len(side_samples) < settings.evolve_min_backtest_trades:
                continue
            for fake_risks in fake_sets:
                base = [s for s in side_samples if s.get("fake_breakout_risk") in fake_risks]
                winners = [s for s in base if float(s.get("pnl", 0.0) or 0.0) > 0]
                if len(winners) < max(4, settings.evolve_min_holdout_trades):
                    continue
                for name, score_q, confirm_q, volume_q, wick_q in profiles:
                    filters = {
                        "min_score": round(_quantile([_f(s.get("score")) for s in winners], score_q), 2),
                        "min_fund_confirm": max(3, int(round(_quantile([_f(s.get("fund_confirm_count")) for s in winners], 0.20)))),
                        "allowed_fake_risks": fake_risks,
                        "min_direction_confirmations": max(
                            3,
                            int(round(_quantile([direction_confirmations(s, str(s.get("side"))) for s in winners], confirm_q))),
                        ),
                        "min_volume_spike": round(_quantile([_f(s.get("volume_spike")) for s in winners], volume_q), 3),
                        "max_wick_ratio": round(min(0.55, _quantile([_f(s.get("wick_ratio"), 1.0) for s in winners], wick_q)), 3),
                        "require_oi_positive": self._lift(base, lambda s: _f(s.get("oi_change")) >= 0) >= -0.01,
                        "require_timeframe_alignment": self._lift(base, lambda s: _timeframe_ok(s)) >= 0.01 or name != "balanced",
                        "require_taker_alignment": self._lift(base, lambda s: _taker_ok(s)) >= 0.0,
                        "require_depth_alignment": self._lift(base, lambda s: _depth_ok(s)) >= 0.02 or name == "recovery",
                        "require_sm_delta_alignment": self._lift(base, lambda s: _sm_ok(s)) >= 0.02 and name != "balanced",
                        "allowed_sides": sides,
                    }
                    key = json.dumps(filters, sort_keys=True)
                    if key in seen:
                        continue
                    seen.add(key)
                    candidates.append(
                        self._candidate(
                            "sample_induction",
                            filters,
                            f"{name}_{'_'.join(sides).lower()}",
                            "Derived from winner quantiles and loser-separation lift.",
                        )
                    )
        return candidates

    def _lift(self, samples: list[dict[str, Any]], predicate) -> float:
        selected = [s for s in samples if predicate(s)]
        if len(selected) < 4 or len(samples) < 4:
            return 0.0
        base_win = sum(1 for s in samples if float(s.get("pnl", 0.0) or 0.0) > 0) / len(samples)
        selected_win = sum(1 for s in selected if float(s.get("pnl", 0.0) or 0.0) > 0) / len(selected)
        return selected_win - base_win

    def _codex_candidates(self, memory_summary: dict[str, Any]) -> list[dict[str, Any]]:
        if not settings.codex_command and not default_codex_command():
            return []
        schema_path = Path(__file__).with_name("evolved_strategy.schema.json")
        prompt = CODEX_EVOLVE_PROMPT.format(summary_json=json.dumps(memory_summary, ensure_ascii=False, indent=2))
        with tempfile.TemporaryDirectory(prefix="ai_radar_evolve_") as tmp:
            output_path = Path(tmp) / "evolved_strategies.json"
            cmd = [
                settings.codex_command or default_codex_command(),
                "exec",
                "--ignore-user-config",
                *codex_provider_config_args(),
                "-c",
                f"model_reasoning_effort={_evolve_reasoning_effort()}",
                "-c",
                f"service_tier={normalized_codex_service_tier()}",
                "--skip-git-repo-check",
                "--ephemeral",
                "--sandbox",
                "read-only",
                "-C",
                str(Path(__file__).resolve().parents[2]),
                "--output-schema",
                str(schema_path),
                "--output-last-message",
                str(output_path),
            ]
            model = _evolve_model()
            if model:
                cmd.extend(["-m", model])
            cmd.append("-")
            try:
                completed = run_command(
                    cmd,
                    cwd=str(Path(__file__).resolve().parents[2]),
                    input=prompt,
                    text=True,
                    encoding="utf-8",
                    capture_output=True,
                    timeout=_evolve_timeout_seconds(),
                )
                if completed.returncode != 0:
                    return []
                raw = output_path.read_text(encoding="utf-8") if output_path.exists() else (completed.stdout or "")
                payload = json.loads(raw)
            except (json.JSONDecodeError, OSError, subprocess.TimeoutExpired, RuntimeError):
                return []
        out = []
        for item in payload.get("strategies", []):
            filters = item.get("filters") or {}
            out.append(self._candidate("codex", filters, item.get("name"), item.get("rationale")))
        return out

    def _candidate(self, source: str, filters: dict[str, Any], name: str | None = None, rationale: str | None = None) -> dict[str, Any]:
        return {
            "strategy_id": new_id("evolved"),
            "name": name or f"{source}_filter",
            "source": source,
            "status": "WATCH",
            "filters": filters,
            "rationale": rationale or "",
            "created_at": now_ms(),
            "version": 1,
        }

    def _score_key(self, record: dict[str, Any]) -> tuple:
        metrics = record.get("metrics") or {}
        holdout = metrics.get("holdout") or {}
        return (
            1 if metrics.get("eligible") else 0,
            float(holdout.get("pnl", 0.0) or 0.0),
            float(holdout.get("win_rate", 0.0) or 0.0),
            float(metrics.get("pnl", 0.0) or 0.0),
            float(metrics.get("profit_factor", 0.0) or 0.0),
            int(metrics.get("trades", 0) or 0),
        )


def _quantile(values: list[float], q: float) -> float:
    clean = sorted(v for v in values if v == v)
    if not clean:
        return 0.0
    q = min(max(q, 0.0), 1.0)
    index = int(round((len(clean) - 1) * q))
    return clean[index]


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


def _combine_samples(replay_samples: list[dict[str, Any]], trade_samples: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for sample in sorted([*replay_samples, *trade_samples], key=_sample_time, reverse=True):
        key = _sample_key(sample)
        if key in seen:
            continue
        seen.add(key)
        out.append(sample)
    return out


def _train_holdout_split(samples: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    ordered = sorted(samples, key=_sample_time)
    if not ordered:
        return [], []
    split_at = int(len(ordered) * min(max(settings.evolve_train_split, 0.1), 0.9))
    split_at = min(max(1, split_at), max(1, len(ordered) - 1)) if len(ordered) > 1 else len(ordered)
    return ordered[:split_at], ordered[split_at:]


def _sample_source(replay_samples: list[dict[str, Any]], trade_samples: list[dict[str, Any]]) -> str:
    if replay_samples and trade_samples:
        return "replay+closed_trades"
    if replay_samples:
        return "replay"
    return "closed_trades"


def _timeframe_ok(sample: dict[str, Any]) -> bool:
    side = sample.get("side")
    if side == "LONG":
        return _f(sample.get("change_5m")) > 0 and _f(sample.get("change_15m")) > 0 and _f(sample.get("change_1h")) >= 0
    if side == "SHORT":
        return _f(sample.get("change_5m")) < 0 and _f(sample.get("change_15m")) < 0 and _f(sample.get("change_1h")) <= 0
    return False


def _taker_ok(sample: dict[str, Any]) -> bool:
    side = sample.get("side")
    return _f(sample.get("taker_buy_ratio"), 0.5) >= 0.58 if side == "LONG" else _f(sample.get("taker_sell_ratio"), 0.5) >= 0.58


def _depth_ok(sample: dict[str, Any]) -> bool:
    side = sample.get("side")
    return _f(sample.get("depth_imbalance")) >= 0.12 if side == "LONG" else _f(sample.get("depth_imbalance")) <= -0.12


def _sm_ok(sample: dict[str, Any]) -> bool:
    side = sample.get("side")
    return _f(sample.get("sm_delta")) >= 0 if side == "LONG" else _f(sample.get("sm_delta")) <= 0


def _evolve_model() -> str:
    return str(settings.codex_evolve_model or settings.codex_model or "").strip()


def _evolve_reasoning_effort() -> str:
    return normalized_reasoning_effort(settings.codex_evolve_reasoning_effort, "medium")


def _evolve_timeout_seconds() -> float:
    configured = settings.codex_evolve_timeout_seconds or settings.codex_timeout_seconds
    return max(10.0, float(configured or 120.0))


strategy_evolver = StrategyEvolver()
