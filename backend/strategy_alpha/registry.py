from __future__ import annotations

from typing import Any

from backend.models import now_ms
from backend.storage.db import db
from backend.strategy_alpha.promotion import StrategyPromotionPolicy, strategy_promotion_policy

POOL_KEY = "strategy_alpha.pool"
RUNS_KEY = "strategy_alpha.runs"


class StrategyAlphaRegistry:
    def __init__(
        self,
        *,
        db_obj=db,
        promotion_policy: StrategyPromotionPolicy = strategy_promotion_policy,
        pool_limit: int = 100,
    ) -> None:
        self.db = db_obj
        self.promotion_policy = promotion_policy
        self.pool_limit = max(1, int(pool_limit))

    def save(self, strategy: dict[str, Any], evaluation: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = dict(strategy)
        payload.setdefault("source", "strategy_alpha")
        payload.setdefault("status", "RESEARCH_ALPHA")
        payload.setdefault("created_at", now_ms())
        payload["updated_at"] = now_ms()
        if evaluation:
            payload["evaluation"] = dict(evaluation)
            for key in ("alpha_score", "stability_score", "overfit_risk", "pnl", "winrate", "profit_factor", "max_drawdown", "sharpe"):
                if key in evaluation:
                    payload[key] = evaluation[key]
        payload["promotion"] = self.promotion_policy.review(payload)

        pool = [row for row in self.list(limit=self.pool_limit) if row.get("strategy_id") != payload.get("strategy_id")]
        pool.append(payload)
        pool = sorted(pool, key=lambda row: float(row.get("alpha_score") or 0.0), reverse=True)[: self.pool_limit]
        self.db.set_kv(POOL_KEY, pool)
        return payload

    def top(self, limit: int = 10) -> list[dict[str, Any]]:
        return self.list(limit=limit)

    def list(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = self.db.get_kv(POOL_KEY, []) or []
        if not isinstance(rows, list):
            return []
        sorted_rows = sorted(
            [row for row in rows if isinstance(row, dict)],
            key=lambda row: float(row.get("alpha_score") or 0.0),
            reverse=True,
        )
        return sorted_rows[: max(1, int(limit))]

    def strategy_pool_score(self) -> float:
        promotable = [
            row
            for row in self.list(limit=self.pool_limit)
            if self.promotion_policy.can_promote_to_micro_live(row)
        ]
        if not promotable:
            return 0.0
        return round(max(float(row.get("alpha_score") or 0.0) for row in promotable), 4)

    def save_run(self, run: dict[str, Any]) -> dict[str, Any]:
        payload = dict(run)
        payload.setdefault("created_at", now_ms())
        runs = self.runs(limit=100)
        runs.insert(0, payload)
        self.db.set_kv(RUNS_KEY, runs[:100])
        return payload

    def runs(self, limit: int = 20) -> list[dict[str, Any]]:
        rows = self.db.get_kv(RUNS_KEY, []) or []
        if not isinstance(rows, list):
            return []
        return [row for row in rows if isinstance(row, dict)][: max(1, int(limit))]


strategy_alpha_registry = StrategyAlphaRegistry()
