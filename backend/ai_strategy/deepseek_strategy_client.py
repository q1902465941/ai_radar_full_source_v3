from __future__ import annotations

from dataclasses import asdict
import json
from typing import Any

import httpx

from backend.ai_strategy.codex_cli_strategy_client import CODEX_PROMPT
from backend.ai_strategy.context_compressor import context_compressor
from backend.ai_strategy.position_policy_client import extract_json_object, redact_secret
from backend.ai_strategy.strategy_contract import attach_contract, build_rule_contract, contract_quality
from backend.ai_strategy.strategy_validator import strategy_validator
from backend.config import settings
from backend.models import RadarItem, StrategyPlan, new_id, now_ms
from backend.radar.candidate_feature_enhancer import candidate_feature_enhancer


class DeepSeekStrategyClient:
    def __init__(self) -> None:
        self.invocation_count = 0
        self.last_invoked_ms = 0
        self.last_status = "never_invoked"
        self.last_error = ""
        self.last_symbol = ""
        self.last_action = ""
        self.last_model = ""
        self.last_plan: dict[str, Any] = {}
        self.recent_plans: list[dict[str, Any]] = []

    async def generate(self, item: RadarItem, position_context: dict | None = None) -> StrategyPlan:
        self.invocation_count += 1
        self.last_invoked_ms = now_ms()
        self.last_status = "running"
        self.last_error = ""
        self.last_symbol = item.symbol
        self.last_action = ""
        self.last_model = deepseek_strategy_model()
        context = context_compressor.build_strategy_context(item, position_context)
        prompt = CODEX_PROMPT.format(context_json=json.dumps(context, ensure_ascii=False, indent=2))
        try:
            raw = await self._run_deepseek(prompt)
            plan = self._plan_from_dict(json.loads(extract_json_object(raw)), item)
        except json.JSONDecodeError:
            self.last_status = "fallback_wait"
            self.last_error = "deepseek_invalid_json"
            return self._fallback(item, "deepseek_invalid_json")
        except httpx.TimeoutException:
            self.last_status = "fallback_wait"
            self.last_error = "deepseek_timeout"
            return self._fallback(item, "deepseek_timeout")
        except httpx.HTTPStatusError as exc:
            self.last_status = "fallback_wait"
            self.last_error = f"deepseek_http_{exc.response.status_code}"
            return self._fallback(item, f"deepseek_http_{exc.response.status_code}")
        except Exception as exc:
            reason = "deepseek_api_key_missing" if str(exc) == "deepseek_api_key_missing" else f"deepseek_error:{type(exc).__name__}"
            self.last_status = "fallback_wait"
            self.last_error = redact_secret(reason)
            return self._fallback(item, reason)

        ok, reason = strategy_validator.validate(plan)
        if not ok:
            self.last_status = "fallback_wait"
            self.last_error = f"deepseek_invalid_plan:{reason}"
            return self._fallback(item, f"deepseek_invalid_plan:{reason}")
        self.last_status = "ok"
        self.last_action = plan.action
        return self._record_plan(item, plan, "ok", "")

    async def _run_deepseek(self, prompt: str) -> str:
        if not settings.deepseek_api_key:
            raise RuntimeError("deepseek_api_key_missing")
        payload = {
            "model": deepseek_strategy_model(),
            "messages": [
                {"role": "system", "content": "You are a crypto strategy planner. Return valid JSON only."},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0,
            "stream": False,
            "thinking": {"type": "disabled"},
            "response_format": {"type": "json_object"},
            "max_tokens": 2600,
        }
        headers = {
            "Authorization": f"Bearer {settings.deepseek_api_key}",
            "Content-Type": "application/json",
        }
        base_url = str(settings.deepseek_base_url or "https://api.deepseek.com").rstrip("/")
        timeout = httpx.Timeout(float(settings.deepseek_strategy_timeout_seconds or 30.0))
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(f"{base_url}/chat/completions", headers=headers, json=payload)
            response.raise_for_status()
        data = response.json()
        return str(data["choices"][0]["message"]["content"] or "")

    def _plan_from_dict(self, data: dict[str, Any], item: RadarItem) -> StrategyPlan:
        raw_upgrade = data.get("upgrade_condition") or {}
        raw_upgrade = {k: v for k, v in raw_upgrade.items() if v is not None} if isinstance(raw_upgrade, dict) else {}
        plan = StrategyPlan(
            strategy_id=new_id("deepseek"),
            action=data.get("action", "WAIT"),
            symbol=data.get("symbol") or item.symbol,
            side=data.get("side") or ("NEUTRAL" if data.get("action") == "WAIT" else item.direction),
            entry_zone_low=float(data.get("entry_zone_low", item.price) or 0),
            entry_zone_high=float(data.get("entry_zone_high", item.price) or 0),
            ideal_entry_price=float(data.get("ideal_entry_price", item.price) or item.price),
            stop_loss=float(data.get("stop_loss", 0) or 0),
            tp1=float(data.get("tp1", 0) or 0),
            tp2=float(data.get("tp2", 0) or 0),
            confidence=float(data.get("confidence", 0) or 0),
            reason=str(data.get("reason") or "deepseek_plan"),
            wait_type=str(data.get("wait_type") or ""),
            expire_after_seconds=int(data.get("expire_after_seconds", 180) or 180),
            raw={
                "provider": "deepseek",
                "upgrade_condition": raw_upgrade,
                "model": deepseek_strategy_model(),
                "cyqnt_feature_enhancement": candidate_feature_enhancer.evaluate(item).asdict(),
            },
        )
        raw_contract = data.get("strategy_contract")
        if isinstance(raw_contract, dict) and contract_quality(raw_contract)[0]:
            return attach_contract(plan, raw_contract)
        return attach_contract(plan, build_rule_contract(item, plan, paper_probe=False))

    def _fallback(self, item: RadarItem, reason: str) -> StrategyPlan:
        self.last_action = "WAIT"
        plan = StrategyPlan(
            strategy_id=new_id("deepseek_wait"),
            action="WAIT",
            symbol=item.symbol,
            side="NEUTRAL",
            entry_zone_low=item.price,
            entry_zone_high=item.price,
            ideal_entry_price=item.price,
            stop_loss=0,
            tp1=0,
            tp2=0,
            confidence=0,
            reason="deepseek unavailable or invalid; wait instead of forcing a trade",
            wait_type="WAIT_FOR_DEEPSEEK_RECOVERY",
            expire_after_seconds=180,
            raw={
                "provider": "deepseek_unavailable",
                "fallback_reason": redact_secret(reason),
                "model": deepseek_strategy_model(),
                "cyqnt_feature_enhancement": candidate_feature_enhancer.evaluate(item).asdict(),
            },
        )
        return self._record_plan(item, plan, "fallback_wait", reason)

    def _record_plan(self, item: RadarItem, plan: StrategyPlan, status: str, reason: str) -> StrategyPlan:
        row = {
            "ts_ms": now_ms(),
            "provider": "deepseek",
            "model": deepseek_strategy_model(),
            "status": status,
            "status_reason": redact_secret(reason),
            "symbol": item.symbol,
            "signal": {
                "rank": item.rank,
                "side": item.direction,
                "score": item.score,
                "fund_confirm": f"{item.fund_confirm_count}/{item.fund_confirm_total}",
                "fake_breakout_risk": item.fake_breakout_risk,
                "volume_spike": item.volume_spike,
                "wick_ratio": item.wick_ratio,
                "atr_pct": item.atr_pct,
                "taker_buy_ratio": item.taker_buy_ratio,
                "taker_sell_ratio": item.taker_sell_ratio,
                "depth_imbalance": item.depth_imbalance,
                "sm_delta": item.sm_delta,
                "cyqnt_feature_enhancement": candidate_feature_enhancer.evaluate(item).asdict(),
            },
            "plan": asdict(plan),
        }
        self.last_plan = row
        self.recent_plans.insert(0, row)
        self.recent_plans = self.recent_plans[:30]
        return plan

    def status(self) -> dict[str, Any]:
        return {
            "configured": True,
            "base_url": settings.deepseek_base_url,
            "api_key_configured": bool(settings.deepseek_api_key),
            "model": deepseek_strategy_model(),
            "timeout_seconds": settings.deepseek_strategy_timeout_seconds,
            "invocation_count": self.invocation_count,
            "last_invoked_ms": self.last_invoked_ms,
            "last_status": self.last_status,
            "last_error": redact_secret(self.last_error),
            "last_symbol": self.last_symbol,
            "last_action": self.last_action,
            "last_plan": self.last_plan,
            "recent_plans": self.recent_plans[:10],
        }


def deepseek_strategy_model() -> str:
    return str(settings.deepseek_strategy_model or settings.ai_position_review_model or "deepseek-v4-pro").strip()


deepseek_strategy_client = DeepSeekStrategyClient()
