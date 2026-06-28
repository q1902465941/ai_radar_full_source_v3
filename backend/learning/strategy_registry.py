from __future__ import annotations

import json
from typing import Any

from backend.models import now_ms
from backend.storage.db import db


class StrategyRegistry:
    def save(self, strategy: dict[str, Any]) -> dict[str, Any]:
        now = now_ms()
        payload = dict(strategy)
        payload.setdefault("created_at", now)
        payload["updated_at"] = now
        status = payload.get("status", "WATCH")
        with db.conn() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO evolved_strategies(strategy_id,status,payload,created_at,updated_at)
                VALUES(?,?,?,?,?)
                """,
                (
                    payload["strategy_id"],
                    status,
                    json.dumps(payload, ensure_ascii=False),
                    int(payload.get("created_at") or now),
                    now,
                ),
            )
        return payload

    def save_run(self, run: dict[str, Any]) -> dict[str, Any]:
        payload = dict(run)
        payload.setdefault("created_at", now_ms())
        with db.conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO strategy_evolution_runs(run_id,payload,created_at) VALUES(?,?,?)",
                (payload["run_id"], json.dumps(payload, ensure_ascii=False), int(payload["created_at"])),
            )
        return payload

    def activate(self, strategy_id: str) -> dict[str, Any] | None:
        strategy = self.get(strategy_id)
        if not strategy:
            return None
        with db.conn() as conn:
            conn.execute("UPDATE evolved_strategies SET status='INACTIVE' WHERE status='ACTIVE'")
        strategy["status"] = "ACTIVE"
        return self.save(strategy)

    def get(self, strategy_id: str) -> dict[str, Any] | None:
        with db.conn() as conn:
            row = conn.execute("SELECT payload FROM evolved_strategies WHERE strategy_id=?", (strategy_id,)).fetchone()
        if not row:
            return None
        return json.loads(row["payload"])

    def active(self) -> dict[str, Any] | None:
        with db.conn() as conn:
            row = conn.execute(
                "SELECT payload FROM evolved_strategies WHERE status='ACTIVE' ORDER BY updated_at DESC LIMIT 1"
            ).fetchone()
        if not row:
            return None
        return json.loads(row["payload"])

    def list(self, limit: int = 50) -> list[dict[str, Any]]:
        with db.conn() as conn:
            rows = conn.execute(
                "SELECT payload FROM evolved_strategies ORDER BY updated_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [json.loads(row["payload"]) for row in rows]

    def runs(self, limit: int = 20) -> list[dict[str, Any]]:
        with db.conn() as conn:
            rows = conn.execute(
                "SELECT payload FROM strategy_evolution_runs ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [json.loads(row["payload"]) for row in rows]


strategy_registry = StrategyRegistry()
