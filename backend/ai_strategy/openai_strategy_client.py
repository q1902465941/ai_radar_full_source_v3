from __future__ import annotations
from backend.config import settings
from backend.models import RadarItem, StrategyPlan, new_id
from backend.ai_strategy.codex_cli_strategy_client import codex_cli_strategy_client
from backend.ai_strategy.deepseek_strategy_client import deepseek_strategy_client
from backend.positions.position_registry import position_registry

SYSTEM_PROMPT = """你是交易策略生成模块。只能输出合法JSON，不要Markdown，不要解释。若条件不足，action=WAIT。LONG必须 sl < entry < tp1 < tp2。SHORT必须 tp2 < tp1 < entry < sl。reason少于80中文字符。"""

class OpenAIStrategyClient:
    async def generate(self, item: RadarItem, position_context: dict | None = None) -> StrategyPlan:
        if _position_manager_priority() or _capacity_full():
            return _position_priority_wait(item)
        if not settings.ai_enabled:
            return _ai_strategy_wait(item, "ai_disabled")
        if settings.require_codex_strategy_for_entry and settings.ai_strategy_provider != "codex_cli":
            return _ai_strategy_wait(item, f"codex_required_provider_{settings.ai_strategy_provider}")
        if settings.ai_strategy_provider == "deepseek":
            return await deepseek_strategy_client.generate(item, position_context)
        if settings.ai_strategy_provider == "codex_cli":
            return await codex_cli_strategy_client.generate(item, position_context)
        if not settings.openai_api_key:
            return _ai_strategy_wait(item, "openai_unimplemented_or_key_missing")
        # This keeps project dependency small. Install openai SDK and implement call here for production.
        return _ai_strategy_wait(item, "openai_provider_unimplemented")

    def status(self, *, candidate_count: int = 0, candidate_source: str = "") -> dict:
        provider = settings.ai_strategy_provider
        position_priority = _position_manager_priority()
        capacity_full = _capacity_full()
        if position_priority:
            not_invoked_reason = "open_position_manager_priority"
        elif capacity_full:
            not_invoked_reason = "capacity_full"
        elif not settings.ai_enabled:
            not_invoked_reason = "ai_disabled"
        elif candidate_count <= 0:
            not_invoked_reason = "candidate_filter_empty_before_ai"
        elif settings.require_codex_strategy_for_entry and provider != "codex_cli":
            not_invoked_reason = f"codex_required_provider_{provider}"
        elif provider not in {"codex_cli", "deepseek"}:
            not_invoked_reason = f"provider_{provider}_uses_local_or_unimplemented_fallback"
        else:
            not_invoked_reason = ""
        return {
            "enabled": bool(settings.ai_enabled),
            "provider": provider,
            "candidate_source": candidate_source,
            "candidate_count_before_ai": candidate_count,
            "will_invoke_for_current_candidates": bool(
                settings.ai_enabled
                and (provider == "codex_cli" or (provider == "deepseek" and not settings.require_codex_strategy_for_entry))
                and candidate_count > 0
                and not position_priority
                and not capacity_full
            ),
            "not_invoked_reason": not_invoked_reason,
            "codex_cli": codex_cli_strategy_client.status(),
            "deepseek": deepseek_strategy_client.status(),
        }

def _position_manager_priority() -> bool:
    return bool(settings.ai_position_review_enabled and position_registry.list_open())

def _capacity_full() -> bool:
    return len(position_registry.list_open()) >= int(settings.max_open_positions or 1)

def _position_priority_wait(item: RadarItem) -> StrategyPlan:
    return StrategyPlan(
        strategy_id=new_id("position_priority_wait"),
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
        reason="open position active; reserve AI budget for position management",
        wait_type="WAIT_OPEN_POSITION_MANAGER_PRIORITY",
        expire_after_seconds=60,
        raw={"provider": "local_position_priority_guard", "open_position_priority": True},
    )

def _ai_strategy_wait(item: RadarItem, reason: str) -> StrategyPlan:
    return StrategyPlan(
        strategy_id=new_id("ai_wait"),
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
        reason="Codex strategy generation is required before any entry",
        wait_type="WAIT_FOR_CODEX_STRATEGY",
        expire_after_seconds=180,
        raw={
            "provider": "codex_required_unavailable",
            "fallback_reason": reason,
            "codex_required_for_entry": True,
        },
    )

openai_strategy_client = OpenAIStrategyClient()
