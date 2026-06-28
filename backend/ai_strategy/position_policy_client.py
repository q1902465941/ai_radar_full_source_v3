from __future__ import annotations

import asyncio
import json
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
from typing import Any, Callable, Sequence

import httpx

from backend.ai_strategy.codex_cli_strategy_client import (
    codex_provider_config_args,
    default_codex_command,
    normalized_codex_service_tier,
    run_command,
)
from backend.config import settings
from backend.models import Position, PositionDecision, PositionPolicyReview, now_ms


POSITION_POLICY_PROMPT = """You are the AI position manager for a crypto paper-trading system.
Return exactly one JSON object matching the provided schema.
Do not output Markdown, code fences, comments, or text outside the JSON object.

Hard rules:
1. You review an already-open position. Do not create a new trade.
2. Scanning evidence is not an exit command. Minor reverse signal alone is not enough to close the core position.
3. Interpret market noise: decide whether adverse movement is normal noise, warning noise, structure weakening, or thesis invalidation.
4. You may suggest HOLD, PROTECT, REDUCE, or EXIT. The local safety kernel can override you.
5. Do not suggest EXIT unless the trade thesis is invalidated, hard risk is near, or evidence is clearly beyond normal noise.
6. Do not suggest REDUCE unless risk is weakening or profit needs protection. Use reduce_ratio between 0 and 0.5 for normal defensive reductions.
7. Respect live safety: this review never grants live trading permission.
8. Focus on position lifecycle: protect, hold, reduce, exit, and what should be learned from the decision.
9. Do not inspect files, run commands, or use tools. Use only the PositionContext below.

PositionContext:
{context_json}
"""


Runner = Callable[..., subprocess.CompletedProcess[str]]


class AIPositionPolicyClient:
    def __init__(
        self,
        *,
        runner: Runner | None = None,
        codex_command: str | None = None,
        schema_path: str | Path | None = None,
    ) -> None:
        self.runner = runner or run_command
        self.codex_command = codex_command or settings.codex_command or default_codex_command()
        self.schema_path = Path(schema_path) if schema_path else Path(__file__).with_name("position_policy.schema.json")
        self.invocation_count = 0
        self.last_invoked_ms = 0
        self.last_status = "never_invoked"
        self.last_error = ""
        self.last_symbol = ""
        self.last_action = ""
        self.consecutive_failures = 0
        self.circuit_open_until_ms = 0

    async def review(self, position: Position, signal, rule_decision: PositionDecision) -> PositionPolicyReview:
        if self.circuit_open_until_ms > now_ms():
            self.last_status = "circuit_open"
            self.last_error = f"{position_review_provider()}_circuit_open"
            self.last_symbol = position.symbol
            self.last_action = rule_decision.action
            return self._fallback(rule_decision, f"{position_review_provider()}_circuit_open")
        self.invocation_count += 1
        self.last_invoked_ms = now_ms()
        self.last_status = "running"
        self.last_error = ""
        self.last_symbol = position.symbol
        self.last_action = ""
        context = build_position_policy_context(position, signal, rule_decision)
        prompt = POSITION_POLICY_PROMPT.format(context_json=json.dumps(context, ensure_ascii=False, indent=2))
        try:
            if position_review_provider() == "deepseek":
                raw = await self._run_deepseek(prompt)
            else:
                raw = await asyncio.to_thread(self._run_codex, prompt)
            review = self._review_from_dict(json.loads(raw))
        except json.JSONDecodeError:
            self.last_status = "fallback_rule"
            self.last_error = f"{position_review_provider()}_invalid_json"
            self._record_failure()
            return self._fallback(rule_decision, f"{position_review_provider()}_invalid_json")
        except httpx.TimeoutException:
            self.last_status = "fallback_rule"
            self.last_error = "deepseek_timeout"
            self._record_failure()
            return self._fallback(rule_decision, "deepseek_timeout")
        except httpx.HTTPStatusError as exc:
            self.last_status = "fallback_rule"
            self.last_error = f"deepseek_http_{exc.response.status_code}"
            self._record_failure()
            return self._fallback(rule_decision, f"deepseek_http_{exc.response.status_code}")
        except subprocess.TimeoutExpired:
            self.last_status = "fallback_rule"
            self.last_error = "codex_timeout"
            self._record_failure()
            return self._fallback(rule_decision, "codex_timeout")
        except Exception as exc:
            if str(exc) == "codex_cli_busy":
                self.last_status = "fallback_rule"
                self.last_error = "codex_busy"
                self._record_failure()
                return self._fallback(rule_decision, "codex_busy")
            if str(exc) == "DEEPSEEK_API_KEY_MISSING":
                self.last_status = "fallback_rule"
                self.last_error = "DEEPSEEK_API_KEY_MISSING"
                self._record_failure()
                return self._fallback(rule_decision, "DEEPSEEK_API_KEY_MISSING")
            self.last_status = "fallback_rule"
            self.last_error = f"{position_review_provider()}_error:{type(exc).__name__}"
            self._record_failure()
            return self._fallback(rule_decision, f"{position_review_provider()}_error:{type(exc).__name__}")

        self.last_status = "ok"
        self.last_action = review.action
        self.consecutive_failures = 0
        self.circuit_open_until_ms = 0
        return review

    async def _run_deepseek(self, prompt: str) -> str:
        if not settings.deepseek_api_key:
            raise RuntimeError("DEEPSEEK_API_KEY_MISSING")
        base_url = str(settings.deepseek_base_url or "https://api.deepseek.com").rstrip("/")
        payload = {
            "model": position_review_model() or "deepseek-v4-pro",
            "messages": [
                {
                    "role": "system",
                    "content": "You are a realtime crypto position review engine. Return valid JSON only.",
                },
                {"role": "user", "content": prompt},
            ],
            "temperature": 0,
            "stream": False,
            "thinking": {"type": "disabled"},
            "response_format": {"type": "json_object"},
            "max_tokens": 600,
        }
        headers = {
            "Authorization": f"Bearer {settings.deepseek_api_key}",
            "Content-Type": "application/json",
        }
        timeout = httpx.Timeout(float(settings.ai_position_review_timeout_seconds or 20.0))
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(f"{base_url}/chat/completions", headers=headers, json=payload)
            response.raise_for_status()
        data = response.json()
        content = data["choices"][0]["message"]["content"]
        return extract_json_object(str(content or ""))

    def _run_codex(self, prompt: str) -> str:
        with tempfile.TemporaryDirectory(prefix="ai_position_policy_") as tmp:
            output_path = Path(tmp) / "position_policy.json"
            cmd = self._command(output_path)
            completed = self.runner(
                cmd,
                cwd=str(Path(__file__).resolve().parents[2]),
                input=prompt,
                text=True,
                encoding="utf-8",
                capture_output=True,
                timeout=settings.ai_position_review_timeout_seconds,
            )
            if completed.returncode != 0:
                message = (completed.stderr or completed.stdout or "").strip()
                raise RuntimeError(message or f"codex exited with {completed.returncode}")
            if output_path.exists():
                return output_path.read_text(encoding="utf-8").strip()
            return (completed.stdout or "").strip()

    def _command(self, output_path: Path) -> list[str]:
        cmd = [
            self.codex_command,
            "exec",
            "--ignore-user-config",
            *codex_provider_config_args(),
            "-c",
            f"model_reasoning_effort={normalized_position_review_reasoning_effort()}",
            "-c",
            f"service_tier={normalized_codex_service_tier()}",
            "--skip-git-repo-check",
            "--ephemeral",
            "--sandbox",
            "read-only",
            "-C",
            str(Path(__file__).resolve().parents[2]),
            "--output-schema",
            str(self.schema_path),
            "--output-last-message",
            str(output_path),
        ]
        model = position_review_model()
        if model:
            cmd.extend(["-m", model])
        cmd.append("-")
        return cmd

    def _review_from_dict(self, data: dict[str, Any]) -> PositionPolicyReview:
        action = str(data.get("action") or "PROTECT").upper()
        if action not in {"HOLD", "PROTECT", "REDUCE", "EXIT"}:
            action = "PROTECT"
        return PositionPolicyReview(
            ts_ms=now_ms(),
            action=action,
            thesis_alive=bool(data.get("thesis_alive", action != "EXIT")),
            confidence=_clamp(float(data.get("confidence", 0.0) or 0.0), 0.0, 1.0),
            reason=str(data.get("reason") or "ai_position_review"),
            noise_interpretation=str(data.get("noise_interpretation") or "warning_noise"),
            invalidation=str(data.get("invalidation") or ""),
            reduce_ratio=_clamp(float(data.get("reduce_ratio", 0.0) or 0.0), 0.0, 1.0),
            stop_loss=max(0.0, float(data.get("stop_loss", 0.0) or 0.0)),
            learning_note=str(data.get("learning_note") or ""),
            safety_note=str(data.get("safety_note") or ""),
            provider=position_review_provider(),
            status="ok",
        )

    def _fallback(self, rule_decision: PositionDecision, reason: str) -> PositionPolicyReview:
        self.last_action = rule_decision.action
        safe_reason = redact_secret(reason)
        return PositionPolicyReview(
            ts_ms=now_ms(),
            action=rule_decision.action,
            thesis_alive=rule_decision.thesis_alive,
            confidence=0.0,
            reason=f"ai unavailable; keep rule decision: {safe_reason}",
            noise_interpretation="warning_noise" if rule_decision.defense_level != "NORMAL" else "normal_noise",
            invalidation=rule_decision.reason if not rule_decision.thesis_alive else "",
            reduce_ratio=rule_decision.reduce_ratio,
            stop_loss=0.0,
            learning_note="AI review unavailable; evaluate rule decision outcome only.",
            safety_note="fallback_to_rule_decision",
            provider=position_review_provider(),
            status="fallback_rule",
        )

    def _record_failure(self) -> None:
        self.consecutive_failures += 1
        if self.consecutive_failures >= 3:
            self.circuit_open_until_ms = now_ms() + 10 * 60 * 1000

    def status(self) -> dict[str, Any]:
        resolved = shutil.which(self.codex_command) or ""
        provider = position_review_provider()
        return {
            "enabled": bool(settings.ai_position_review_enabled),
            "provider": provider,
            "configured_command": self.codex_command,
            "resolved_command": resolved or self.codex_command,
            "command_found": bool(resolved) if provider == "codex_cli" else False,
            "deepseek_base_url": settings.deepseek_base_url,
            "deepseek_api_key_configured": bool(settings.deepseek_api_key),
            "model": position_review_model(),
            "fallback_model": settings.codex_model,
            "timeout_seconds": settings.ai_position_review_timeout_seconds,
            "min_interval_seconds": settings.ai_position_review_min_interval_seconds,
            "reasoning_effort": normalized_position_review_reasoning_effort(),
            "invocation_count": self.invocation_count,
            "last_invoked_ms": self.last_invoked_ms,
            "last_status": self.last_status,
            "last_error": redact_secret(self.last_error),
            "last_symbol": self.last_symbol,
            "last_action": self.last_action,
            "consecutive_failures": self.consecutive_failures,
            "circuit_open_until_ms": self.circuit_open_until_ms,
        }


def build_position_policy_context(position: Position, signal, rule_decision: PositionDecision) -> dict[str, Any]:
    signal_dict = signal.asdict() if hasattr(signal, "asdict") else None
    return {
        "task": "review_open_position",
        "position": {
            "position_id": position.position_id,
            "symbol": position.symbol,
            "side": position.side,
            "stage": position.stage,
            "lifecycle_state": position.lifecycle_state,
            "entry_price": position.entry_price,
            "current_price": position.current_price,
            "stop_loss": position.stop_loss,
            "initial_stop_loss": position.initial_stop_loss,
            "tp1": position.tp1,
            "tp2": position.tp2,
            "quantity": position.quantity,
            "initial_quantity": position.initial_quantity,
            "roi": position.roi,
            "unrealized_pnl": position.unrealized_pnl,
            "realized_pnl": position.realized_pnl,
            "adverse_r": position.adverse_r,
            "favorable_r": position.favorable_r,
            "mfe": position.mfe,
            "mae": position.mae,
            "mfe_r": position.mfe_r,
            "mae_r": position.mae_r,
            "noise_budget_r": position.noise_budget_r,
            "noise_budget_pct": position.noise_budget_pct,
            "defense_level": position.defense_level,
            "thesis_alive": position.thesis_alive,
            "lock_status": position.lock_status,
            "recent_decisions": list(position.decision_log or [])[-5:],
            "strategy_contract": compact_position_contract(position.strategy_contract),
        },
        "radar_signal": signal_dict,
        "rule_decision": rule_decision.asdict(),
        "safety_kernel": {
            "hard_stop_cannot_be_overridden": True,
            "ai_exit_inside_noise_budget_will_be_blocked": True,
            "ai_reduce_requires_profit_or_material_favorable_r": True,
            "live_trading_permission": False,
        },
        "required_behavior": {
            "hold": "Use when thesis is alive and adverse movement is normal noise.",
            "protect": "Use when evidence weakens but thesis is alive.",
            "reduce": "Use when profit needs protection or risk weakens without full thesis death.",
            "exit": "Use only when thesis is invalidated beyond normal noise or hard risk is near.",
        },
    }


def compact_position_contract(contract: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(contract, dict):
        return {}
    keep = (
        "strategy_kind",
        "hypothesis",
        "signal",
        "risk",
        "hold_logic",
        "exit_logic",
        "invalidation",
        "position_management",
        "learning_tags",
        "allowed_stages",
    )
    return {key: contract.get(key) for key in keep if contract.get(key)}


def normalized_position_review_reasoning_effort() -> str:
    effort = str(settings.ai_position_review_reasoning_effort or "low").strip().lower()
    return effort if effort in {"none", "minimal", "low", "medium", "high", "xhigh"} else "low"


def position_review_provider() -> str:
    provider = str(settings.ai_position_review_provider or "codex_cli").strip().lower()
    return provider if provider in {"codex_cli", "deepseek"} else "codex_cli"


def position_review_model() -> str:
    return str(settings.ai_position_review_model or settings.codex_model or "").strip()


def extract_json_object(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.strip("`").strip()
        if stripped.lower().startswith("json"):
            stripped = stripped[4:].strip()
    if stripped.startswith("{") and stripped.endswith("}"):
        return stripped
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start >= 0 and end > start:
        return stripped[start : end + 1]
    return stripped


def redact_secret(value: Any) -> str:
    text = str(value or "")
    configured = str(settings.deepseek_api_key or "")
    if configured:
        text = text.replace(configured, "sk-***REDACTED***")
    return re.sub(r"sk-[A-Za-z0-9_-]{8,}", "sk-***REDACTED***", text)[:240]


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


ai_position_policy_client = AIPositionPolicyClient()
