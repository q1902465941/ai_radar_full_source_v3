from __future__ import annotations

import importlib.util
from pathlib import Path
import shutil
from typing import Any

from backend.config import settings
from backend.models import RadarItem, StrategyPlan


class JesseResearchAdapter:
    def status(self) -> dict[str, Any]:
        module_spec = importlib.util.find_spec("jesse")
        cli_path = shutil.which("jesse") or ""
        data_path = Path(settings.jesse_data_path)
        return {
            "role": "strategy_research_and_backtest_validator",
            "enabled": bool(settings.jesse_research_enabled),
            "installed": bool(module_spec),
            "cli_found": bool(cli_path),
            "cli_path": cli_path,
            "data_path": str(data_path),
            "live_permission": False,
            "execution_permission": False,
            "state": self._state(bool(module_spec), bool(cli_path)),
            "instruction": (
                "Jesse is an external research/backtest harness. It must not place live orders in this system. "
                "AITradeDirector may use Jesse results as evidence only after parity tests exist."
            ),
        }

    def audit_context(self, item: RadarItem | None = None, plan: StrategyPlan | None = None) -> dict[str, Any]:
        status = self.status()
        context: dict[str, Any] = {
            "role": status["role"],
            "enabled": status["enabled"],
            "installed": status["installed"],
            "state": status["state"],
            "live_permission": False,
            "execution_permission": False,
        }
        if item is not None:
            context["candidate"] = {
                "symbol": item.symbol,
                "side": item.direction,
                "score": item.score,
                "rank": item.rank,
            }
        if plan is not None:
            context["plan"] = {
                "strategy_id": plan.strategy_id,
                "action": plan.action,
                "side": plan.side,
                "entry": plan.ideal_entry_price,
                "stop_loss": plan.stop_loss,
                "tp1": plan.tp1,
                "tp2": plan.tp2,
            }
        context["result"] = {
            "audit_ran": False,
            "reason": "jesse_research_disabled_or_not_installed",
            "usable_for_live": False,
        }
        return context

    def _state(self, installed: bool, cli_found: bool) -> str:
        if not settings.jesse_research_enabled:
            return "disabled"
        if not installed:
            return "not_installed"
        if not cli_found:
            return "module_installed_cli_missing"
        return "ready_for_research"


jesse_research_adapter = JesseResearchAdapter()
