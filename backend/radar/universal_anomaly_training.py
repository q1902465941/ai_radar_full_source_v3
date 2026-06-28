from __future__ import annotations

import json
from dataclasses import fields
from typing import Any

from backend.models import RadarItem, now_ms
from backend.radar.universal_anomaly_model import universal_anomaly_model
from backend.storage.db import db


_RADAR_ITEM_FIELDS = {field.name for field in fields(RadarItem)}


class UniversalAnomalyTrainingBuilder:
    def __init__(self, database=None, model=None):
        self.database = database or db
        self.model = model or universal_anomaly_model

    def collect(self, *, horizon_minutes: int = 5, limit: int = 500) -> dict[str, Any]:
        horizon = max(1, int(horizon_minutes))
        horizon_ms = horizon * 60 * 1000
        rows = self._source_rows(limit=max(1, int(limit)))
        samples: list[dict[str, Any]] = []
        missing_future = 0
        skipped = 0
        for row in rows:
            source_ts = int(row["ts_ms"] or 0)
            future = self._future_row(str(row["symbol"] or ""), source_ts + horizon_ms)
            if future is None:
                missing_future += 1
                continue
            sample = self._sample_from_pair(row, future, horizon)
            if sample is None:
                skipped += 1
                continue
            samples.append(sample)
        created = self.database.save_universal_anomaly_samples(samples)
        return {
            "ok": True,
            "model": self.model.model_name,
            "horizon_minutes": horizon,
            "examined": len(rows),
            "created": created,
            "duplicates": max(0, len(samples) - created),
            "missing_future": missing_future,
            "skipped": skipped,
            "summary": self.database.universal_anomaly_sample_summary(),
        }

    def recent_samples(self, *, limit: int = 50, horizon_minutes: int | None = None) -> list[dict[str, Any]]:
        return self.database.list_universal_anomaly_samples(limit=limit, horizon_minutes=horizon_minutes)

    def summary(self) -> dict[str, Any]:
        return self.database.universal_anomaly_sample_summary()

    def _source_rows(self, *, limit: int):
        with self.database.conn() as conn:
            return conn.execute(
                """
                SELECT id, scan_id, symbol, payload, ts_ms
                FROM radar_snapshots AS source
                WHERE EXISTS (
                    SELECT 1
                    FROM radar_snapshots AS later
                    WHERE later.symbol = source.symbol
                      AND later.ts_ms > source.ts_ms
                )
                ORDER BY ts_ms DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()

    def _future_row(self, symbol: str, min_ts_ms: int):
        with self.database.conn() as conn:
            return conn.execute(
                """
                SELECT id, scan_id, symbol, payload, ts_ms
                FROM radar_snapshots
                WHERE symbol=? AND ts_ms>=?
                ORDER BY ts_ms ASC
                LIMIT 1
                """,
                (symbol, int(min_ts_ms)),
            ).fetchone()

    def _sample_from_pair(self, source_row, future_row, horizon_minutes: int) -> dict[str, Any] | None:
        source_payload = self._payload(source_row)
        future_payload = self._payload(future_row)
        item = self._radar_item(source_payload)
        if item is None:
            return None
        source_price = self._float(source_payload.get("price"))
        future_price = self._float(future_payload.get("price"))
        if source_price <= 0 or future_price <= 0:
            return None
        future_return_pct = (future_price - source_price) / source_price * 100.0
        row = self.model.training_row(item, future_return_pct=future_return_pct, horizon_minutes=horizon_minutes)
        sample = {
            "sample_id": self._sample_id(str(source_row["symbol"]), int(source_row["ts_ms"] or 0), int(future_row["ts_ms"] or 0), horizon_minutes),
            "model": self.model.model_name,
            "symbol": str(source_row["symbol"] or ""),
            "source_scan_id": source_row["scan_id"],
            "label_scan_id": future_row["scan_id"],
            "source_ts_ms": int(source_row["ts_ms"] or 0),
            "label_ts_ms": int(future_row["ts_ms"] or 0),
            "source_price": round(source_price, 12),
            "label_price": round(future_price, 12),
            "source_direction": source_payload.get("direction", ""),
            "source_rank": int(source_payload.get("rank") or 0),
            "source_score": self._float(source_payload.get("score")),
            "features": row["features"],
            "label_return_pct": row["label_return_pct"],
            "label_direction": row["label_direction"],
            "label_horizon_minutes": row["horizon_minutes"],
            "created_at": now_ms(),
        }
        return sample

    def _payload(self, row) -> dict[str, Any]:
        try:
            payload = json.loads(row["payload"])
        except (TypeError, json.JSONDecodeError):
            return {}
        return payload if isinstance(payload, dict) else {}

    def _radar_item(self, payload: dict[str, Any]) -> RadarItem | None:
        try:
            data = {key: payload[key] for key in _RADAR_ITEM_FIELDS if key in payload}
            return RadarItem(**data)
        except (TypeError, ValueError):
            return None

    def _sample_id(self, symbol: str, source_ts_ms: int, label_ts_ms: int, horizon_minutes: int) -> str:
        return f"universal_anomaly:{int(horizon_minutes)}:{symbol}:{int(source_ts_ms)}:{int(label_ts_ms)}"

    def _float(self, value: Any, default: float = 0.0) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return default


universal_anomaly_training = UniversalAnomalyTrainingBuilder()
