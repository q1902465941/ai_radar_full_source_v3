from sqlalchemy import inspect, select
from sqlalchemy.orm import sessionmaker

from backend.app.db.models import (
    BackgroundTaskRecord,
    Base,
    RadarCandidateRecord,
    RadarScanRecord,
)
from backend.app.db.session import build_engine, session_scope


def test_database_foundation_creates_task_tables(tmp_path):
    db_path = tmp_path / "foundation.db"
    engine = build_engine(f"sqlite:///{db_path}")

    Base.metadata.create_all(engine)

    tables = set(inspect(engine).get_table_names())
    assert "background_tasks" in tables
    assert "ai_tasks" in tables
    assert "radar_scans" in tables
    assert "radar_candidates" in tables

    candidate_columns = {column["name"] for column in inspect(engine).get_columns("radar_candidates")}
    assert {
        "scan_id",
        "symbol",
        "rank",
        "score",
        "direction",
        "raw_json",
    } <= candidate_columns


def test_session_scope_commits_records(tmp_path):
    db_path = tmp_path / "foundation.db"
    engine = build_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)

    with session_scope(SessionLocal) as session:
        session.add(
            BackgroundTaskRecord(
                task_id="task-1",
                kind="radar_scan",
                state="succeeded",
                payload_json={"source": "test"},
            )
        )

    with session_scope(SessionLocal) as session:
        row = session.execute(
            select(BackgroundTaskRecord).where(BackgroundTaskRecord.task_id == "task-1")
        ).scalar_one()

    assert row.kind == "radar_scan"
    assert row.state == "succeeded"
    assert row.payload_json == {"source": "test"}


def test_structured_radar_scan_records_candidates(tmp_path):
    db_path = tmp_path / "foundation.db"
    engine = build_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)

    with session_scope(SessionLocal) as session:
        session.add(
            RadarScanRecord(
                scan_id="scan-1",
                state="succeeded",
                source="v2_task",
                top50_count=1,
                market_heat=72,
                alert_count=1,
                duration_ms=1250,
                metadata_json={"force_refresh": True},
            )
        )
        session.add(
            RadarCandidateRecord(
                scan_id="scan-1",
                symbol="BTCUSDT",
                rank=1,
                score=88.5,
                direction="LONG",
                raw_json={"symbol": "BTCUSDT", "score": 88.5},
            )
        )

    with session_scope(SessionLocal) as session:
        scan = session.execute(
            select(RadarScanRecord).where(RadarScanRecord.scan_id == "scan-1")
        ).scalar_one()
        candidate = session.execute(
            select(RadarCandidateRecord).where(RadarCandidateRecord.scan_id == "scan-1")
        ).scalar_one()

    assert scan.top50_count == 1
    assert scan.metadata_json == {"force_refresh": True}
    assert candidate.symbol == "BTCUSDT"
    assert candidate.rank == 1
    assert candidate.raw_json["score"] == 88.5
