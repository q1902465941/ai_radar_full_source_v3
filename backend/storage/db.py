from __future__ import annotations
import sqlite3, json, os
from pathlib import Path
from typing import Any
from backend.config import settings
from backend.models import now_ms

class DB:
    def __init__(self, path: str | None = None):
        self.path = Path(path or settings.db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init()

    def conn(self):
        c = sqlite3.connect(self.path, check_same_thread=False)
        c.row_factory = sqlite3.Row
        return c

    def _init(self):
        with self.conn() as c:
            c.executescript("""
            CREATE TABLE IF NOT EXISTS radar_snapshots(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                scan_id TEXT,
                symbol TEXT,
                payload TEXT,
                ts_ms INTEGER
            );
            CREATE INDEX IF NOT EXISTS idx_radar_ts ON radar_snapshots(ts_ms);
            CREATE INDEX IF NOT EXISTS idx_radar_scan_symbol ON radar_snapshots(scan_id, symbol);
            CREATE TABLE IF NOT EXISTS positions(
                position_id TEXT PRIMARY KEY,
                payload TEXT,
                status TEXT,
                symbol TEXT,
                ts_ms INTEGER
            );
            CREATE TABLE IF NOT EXISTS closed_positions(
                position_id TEXT PRIMARY KEY,
                payload TEXT,
                symbol TEXT,
                close_time INTEGER
            );
            CREATE TABLE IF NOT EXISTS kv(
                key TEXT PRIMARY KEY,
                value TEXT
            );
            CREATE TABLE IF NOT EXISTS evolved_strategies(
                strategy_id TEXT PRIMARY KEY,
                status TEXT,
                payload TEXT,
                created_at INTEGER,
                updated_at INTEGER
            );
            CREATE INDEX IF NOT EXISTS idx_evolved_status ON evolved_strategies(status, updated_at);
            CREATE TABLE IF NOT EXISTS strategy_evolution_runs(
                run_id TEXT PRIMARY KEY,
                payload TEXT,
                created_at INTEGER
            );
            CREATE TABLE IF NOT EXISTS ai_decision_observations(
                observation_id TEXT PRIMARY KEY,
                payload TEXT,
                symbol TEXT,
                side TEXT,
                decision TEXT,
                reason TEXT,
                created_at INTEGER
            );
            CREATE INDEX IF NOT EXISTS idx_ai_decision_obs_created ON ai_decision_observations(created_at);
            CREATE INDEX IF NOT EXISTS idx_ai_decision_obs_symbol_side ON ai_decision_observations(symbol, side, created_at);
            CREATE TABLE IF NOT EXISTS universal_anomaly_samples(
                sample_id TEXT PRIMARY KEY,
                payload TEXT,
                symbol TEXT,
                label_direction TEXT,
                label_return_pct REAL,
                horizon_minutes INTEGER,
                source_ts_ms INTEGER,
                label_ts_ms INTEGER,
                created_at INTEGER
            );
            CREATE INDEX IF NOT EXISTS idx_universal_anomaly_samples_created ON universal_anomaly_samples(created_at);
            CREATE INDEX IF NOT EXISTS idx_universal_anomaly_samples_horizon ON universal_anomaly_samples(horizon_minutes, created_at);
            CREATE INDEX IF NOT EXISTS idx_universal_anomaly_samples_symbol ON universal_anomaly_samples(symbol, source_ts_ms);
            """)

    def set_kv(self, key: str, value: Any):
        with self.conn() as c:
            c.execute("INSERT OR REPLACE INTO kv(key,value) VALUES(?,?)", (key, json.dumps(value, ensure_ascii=False)))

    def get_kv(self, key: str, default=None):
        with self.conn() as c:
            r = c.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
            return json.loads(r["value"]) if r else default

    def save_radar_items(self, scan_id: str, items: list[dict]):
        with self.conn() as c:
            for it in items:
                c.execute("INSERT INTO radar_snapshots(scan_id,symbol,payload,ts_ms) VALUES(?,?,?,?)", (scan_id, it["symbol"], json.dumps(it, ensure_ascii=False), it.get("ts_ms",0)))

    def save_position(self, p: dict):
        with self.conn() as c:
            c.execute("INSERT OR REPLACE INTO positions(position_id,payload,status,symbol,ts_ms) VALUES(?,?,?,?,?)", (p["position_id"], json.dumps(p, ensure_ascii=False), p["status"], p["symbol"], p.get("open_time",0)))

    def delete_position(self, position_id: str):
        with self.conn() as c:
            c.execute("DELETE FROM positions WHERE position_id=?", (position_id,))

    def list_positions(self):
        with self.conn() as c:
            rows = c.execute("SELECT payload FROM positions WHERE status='OPEN' ORDER BY ts_ms DESC").fetchall()
            return [json.loads(r["payload"]) for r in rows]

    def save_closed(self, p: dict):
        with self.conn() as c:
            c.execute("INSERT OR REPLACE INTO closed_positions(position_id,payload,symbol,close_time) VALUES(?,?,?,?)", (p["position_id"], json.dumps(p, ensure_ascii=False), p["symbol"], p.get("close_time",0)))

    def archive_closed_position(self, p: dict):
        with self.conn() as c:
            c.execute("BEGIN")
            c.execute(
                "INSERT OR REPLACE INTO closed_positions(position_id,payload,symbol,close_time) VALUES(?,?,?,?)",
                (p["position_id"], json.dumps(p, ensure_ascii=False), p["symbol"], p.get("close_time", 0)),
            )
            c.execute("DELETE FROM positions WHERE position_id=?", (p["position_id"],))

    def list_closed(self, limit=10000):
        with self.conn() as c:
            rows = c.execute("SELECT payload FROM closed_positions ORDER BY close_time DESC LIMIT ?", (limit,)).fetchall()
            return [json.loads(r["payload"]) for r in rows]

    def save_ai_observation(self, payload: dict):
        with self.conn() as c:
            c.execute(
                """
                INSERT OR REPLACE INTO ai_decision_observations(
                    observation_id,payload,symbol,side,decision,reason,created_at
                ) VALUES(?,?,?,?,?,?,?)
                """,
                (
                    payload["observation_id"],
                    json.dumps(payload, ensure_ascii=False),
                    payload.get("symbol", ""),
                    payload.get("side", ""),
                    payload.get("decision", ""),
                    payload.get("reason", ""),
                    int(payload.get("created_at") or 0),
                ),
            )

    def list_ai_observations(self, limit=200):
        with self.conn() as c:
            rows = c.execute(
                "SELECT payload FROM ai_decision_observations ORDER BY created_at DESC LIMIT ?",
                (max(1, int(limit)),),
            ).fetchall()
            return [json.loads(r["payload"]) for r in rows]

    def save_universal_anomaly_samples(self, samples: list[dict]) -> int:
        created = 0
        with self.conn() as c:
            for sample in samples:
                cursor = c.execute(
                    """
                    INSERT OR IGNORE INTO universal_anomaly_samples(
                        sample_id,payload,symbol,label_direction,label_return_pct,
                        horizon_minutes,source_ts_ms,label_ts_ms,created_at
                    ) VALUES(?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        sample["sample_id"],
                        json.dumps(sample, ensure_ascii=False),
                        sample.get("symbol", ""),
                        sample.get("label_direction", ""),
                        float(sample.get("label_return_pct") or 0.0),
                        int(sample.get("label_horizon_minutes") or sample.get("horizon_minutes") or 0),
                        int(sample.get("source_ts_ms") or 0),
                        int(sample.get("label_ts_ms") or 0),
                        int(sample.get("created_at") or 0),
                    ),
                )
                created += max(0, int(cursor.rowcount or 0))
        return created

    def list_universal_anomaly_samples(self, limit=200, horizon_minutes: int | None = None):
        limit = max(1, int(limit))
        with self.conn() as c:
            if horizon_minutes is None:
                rows = c.execute(
                    "SELECT payload FROM universal_anomaly_samples ORDER BY created_at DESC LIMIT ?",
                    (limit,),
                ).fetchall()
            else:
                rows = c.execute(
                    """
                    SELECT payload FROM universal_anomaly_samples
                    WHERE horizon_minutes=?
                    ORDER BY created_at DESC
                    LIMIT ?
                    """,
                    (int(horizon_minutes), limit),
                ).fetchall()
            return [json.loads(r["payload"]) for r in rows]

    def update_universal_anomaly_sample_payloads(self, samples: list[dict]) -> int:
        updated = 0
        with self.conn() as c:
            for sample in samples:
                sample_id = str(sample.get("sample_id") or "").strip()
                if not sample_id:
                    continue
                cursor = c.execute(
                    """
                    UPDATE universal_anomaly_samples
                    SET payload=?,
                        symbol=?,
                        label_direction=?,
                        label_return_pct=?,
                        horizon_minutes=?,
                        source_ts_ms=?,
                        label_ts_ms=?,
                        created_at=?
                    WHERE sample_id=?
                    """,
                    (
                        json.dumps(sample, ensure_ascii=False),
                        sample.get("symbol", ""),
                        sample.get("label_direction", ""),
                        float(sample.get("label_return_pct") or 0.0),
                        int(sample.get("label_horizon_minutes") or sample.get("horizon_minutes") or 0),
                        int(sample.get("source_ts_ms") or 0),
                        int(sample.get("label_ts_ms") or 0),
                        int(sample.get("created_at") or 0),
                        sample_id,
                    ),
                )
                updated += max(0, int(cursor.rowcount or 0))
        return updated

    def prune_universal_anomaly_samples(
        self,
        *,
        max_samples: int = 50000,
        retention_days: int | float = 30,
        now_ms_value: int | None = None,
    ) -> dict:
        max_count = max(1, int(max_samples))
        retention = max(0.0, float(retention_days or 0))
        now_value = int(now_ms_value or now_ms())
        deleted_by_age = 0
        deleted_by_cap = 0
        with self.conn() as c:
            if retention > 0:
                cutoff = now_value - int(retention * 86_400_000)
                cursor = c.execute(
                    """
                    DELETE FROM universal_anomaly_samples
                    WHERE (
                        CASE
                            WHEN created_at > 0 THEN created_at
                            ELSE source_ts_ms
                        END
                    ) > 0
                    AND (
                        CASE
                            WHEN created_at > 0 THEN created_at
                            ELSE source_ts_ms
                        END
                    ) < ?
                    """,
                    (cutoff,),
                )
                deleted_by_age = max(0, int(cursor.rowcount or 0))
            cursor = c.execute(
                """
                DELETE FROM universal_anomaly_samples
                WHERE sample_id IN (
                    SELECT sample_id
                    FROM universal_anomaly_samples
                    ORDER BY
                        CASE
                            WHEN created_at > 0 THEN created_at
                            ELSE source_ts_ms
                        END DESC,
                        source_ts_ms DESC
                    LIMIT -1 OFFSET ?
                )
                """,
                (max_count,),
            )
            deleted_by_cap = max(0, int(cursor.rowcount or 0))
            remaining = c.execute("SELECT COUNT(*) AS n FROM universal_anomaly_samples").fetchone()["n"]
        return {
            "ok": True,
            "deleted_by_age": deleted_by_age,
            "deleted_by_cap": deleted_by_cap,
            "deleted": deleted_by_age + deleted_by_cap,
            "remaining": int(remaining or 0),
            "max_samples": max_count,
            "retention_days": retention,
        }

    def universal_anomaly_sample_summary(self) -> dict:
        with self.conn() as c:
            total = c.execute("SELECT COUNT(*) AS n FROM universal_anomaly_samples").fetchone()["n"]
            by_horizon = c.execute(
                """
                SELECT horizon_minutes, COUNT(*) AS n
                FROM universal_anomaly_samples
                GROUP BY horizon_minutes
                ORDER BY horizon_minutes
                """
            ).fetchall()
            by_label = c.execute(
                """
                SELECT label_direction, COUNT(*) AS n
                FROM universal_anomaly_samples
                GROUP BY label_direction
                ORDER BY label_direction
                """
            ).fetchall()
            return {
                "total": int(total or 0),
                "by_horizon": {str(row["horizon_minutes"]): int(row["n"] or 0) for row in by_horizon},
                "by_label": {str(row["label_direction"]): int(row["n"] or 0) for row in by_label},
            }

db = DB()
