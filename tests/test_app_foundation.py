from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

import backend.app.api.radar as radar_api
import backend.app.api.dashboard as dashboard_api
import backend.app.api.ai as ai_api
from backend.app.db.models import Base, RadarCandidateRecord, RadarScanRecord
from backend.app.db.session import build_engine, session_scope
from backend.app.main import create_app
from backend.app.workers.task_registry import TaskRegistry


def test_v2_app_health_route():
    client = TestClient(create_app())

    response = client.get("/api/v2/health")

    assert response.status_code == 200
    assert response.json() == {
        "ok": True,
        "service": "ai-radar-api",
        "version": "v2",
    }


def test_v2_app_starts_hedge_runtime_when_enabled(monkeypatch):
    import backend.app.main as app_main
    from backend.config import settings

    class RuntimeController:
        def __init__(self):
            self.started = 0
            self.stopped = 0

        def start(self):
            self.started += 1

        def stop(self):
            self.stopped += 1

    controller = RuntimeController()
    monkeypatch.setattr(settings, "hedge_runtime_enabled", True, raising=False)
    monkeypatch.setattr(app_main, "create_hedge_fund_runtime", lambda: controller, raising=False)

    with TestClient(app_main.create_app(initialize_database=False)) as client:
        response = client.get("/api/v2/health")
        assert response.json() == {
            "ok": True,
            "service": "ai-radar-api",
            "version": "v2",
        }
        assert controller.started == 1
        assert client.app.state.hedge_fund_runtime is controller

    assert controller.stopped == 1


def test_v2_task_status_route_returns_registry_task():
    registry = TaskRegistry()
    task = registry.create(kind="radar_scan", metadata={"source": "test"})
    client = TestClient(create_app(task_registry=registry))

    response = client.get(f"/api/v2/tasks/{task.task_id}")

    assert response.status_code == 200
    assert response.json()["task_id"] == task.task_id
    assert response.json()["kind"] == "radar_scan"
    assert response.json()["state"] == "pending"


def test_v2_task_status_route_returns_404_for_missing_task():
    client = TestClient(create_app(task_registry=TaskRegistry()))

    response = client.get("/api/v2/tasks/missing")

    assert response.status_code == 404
    assert response.json()["detail"] == "task_not_found"


def test_v2_task_status_route_reads_persisted_task_from_shared_database(tmp_path):
    db_path = tmp_path / "app.db"
    engine = build_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)

    worker_a = TaskRegistry(session_factory=SessionLocal)
    task = worker_a.create(kind="radar_scan", metadata={"force_refresh": True})
    worker_a.mark_running(task.task_id)

    worker_b = TaskRegistry(session_factory=SessionLocal)
    client = TestClient(
        create_app(
            task_registry=worker_b,
            session_factory=SessionLocal,
            initialize_database=False,
        )
    )

    response = client.get(f"/api/v2/tasks/{task.task_id}")

    assert response.status_code == 200
    data = response.json()
    assert data["task_id"] == task.task_id
    assert data["kind"] == "radar_scan"
    assert data["state"] == "running"
    assert data["metadata"] == {"force_refresh": True}


def test_v2_ai_status_route_uses_unified_service(monkeypatch):
    class FakeAIService:
        def status(self, **kwargs):
            return {
                "enabled": True,
                "provider": "fake",
                "audit": {"ai_tasks_table": True},
            }

    monkeypatch.setattr(ai_api, "ai_service", FakeAIService())
    client = TestClient(create_app())

    response = client.get("/api/v2/ai/status")

    assert response.status_code == 200
    assert response.json() == {
        "ok": True,
        "service": {
            "enabled": True,
            "provider": "fake",
            "audit": {"ai_tasks_table": True},
        },
    }


def test_v2_radar_scan_route_returns_background_task():
    registry = TaskRegistry()

    class FakeTaskRunner:
        def __init__(self):
            self.submitted = []

        def submit(self, kind, metadata, handler):
            self.submitted.append({"kind": kind, "metadata": metadata, "handler": handler})
            return registry.create(kind=kind, metadata=metadata)

    runner = FakeTaskRunner()
    client = TestClient(create_app(task_registry=registry, task_runner=runner))

    response = client.post("/api/v2/radar/scans", json={"force_refresh": True})

    assert response.status_code == 202
    data = response.json()
    assert data["ok"] is True
    assert data["task"]["kind"] == "radar_scan"
    assert data["task"]["state"] == "pending"
    assert runner.submitted[0]["kind"] == "radar_scan"
    assert runner.submitted[0]["metadata"] == {"force_refresh": True}


def test_v2_radar_summary_uses_existing_radar_state(monkeypatch):
    class FakeRadarEngine:
        top50 = [object(), object(), object()]
        top4 = [object()]
        last_scan_id = "scan-test"
        last_scan_time = "2026-06-27 12:00:00"
        market_heat = 64
        alert_count = 5

        def scan_status(self):
            return {"in_progress": False, "active_coins": {"active_count": 3}}

    monkeypatch.setattr(radar_api, "radar_engine", FakeRadarEngine())
    client = TestClient(create_app())

    response = client.get("/api/v2/radar/summary")

    assert response.status_code == 200
    assert response.json() == {
        "ok": True,
        "top50_count": 3,
        "top4_count": 1,
        "last_scan_id": "scan-test",
        "last_scan_time": "2026-06-27 12:00:00",
        "market_heat": 64,
        "alert_count": 5,
        "scan_status": {"in_progress": False, "active_coins": {"active_count": 3}},
    }


def test_v2_latest_radar_scan_route_returns_persisted_scan(tmp_path):
    db_path = tmp_path / "app.db"
    engine = build_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)

    with session_scope(SessionLocal) as session:
        session.add(
            RadarScanRecord(
                scan_id="scan-latest",
                state="succeeded",
                source="test",
                top50_count=1,
                market_heat=68,
            )
        )
        session.add(
            RadarCandidateRecord(
                scan_id="scan-latest",
                symbol="ETHUSDT",
                rank=1,
                score=77.5,
                direction="SHORT",
                score_features_json={"volume_score": 80},
                score_explain_json={"score_model": "test"},
                raw_json={"symbol": "ETHUSDT", "score": 77.5},
            )
        )

    client = TestClient(create_app(session_factory=SessionLocal, initialize_database=False))

    response = client.get("/api/v2/radar/scans/latest")

    assert response.status_code == 200
    data = response.json()
    assert data["ok"] is True
    assert data["scan"]["scan_id"] == "scan-latest"
    assert data["scan"]["top50_count"] == 1
    assert data["candidates"][0]["symbol"] == "ETHUSDT"
    assert data["candidates"][0]["direction"] == "SHORT"
    assert "score_features" not in data["candidates"][0]
    assert "score_explain" not in data["candidates"][0]
    assert "raw" not in data["candidates"][0]

    detail_response = client.get("/api/v2/radar/scans/latest?include_details=true")
    detail_candidate = detail_response.json()["candidates"][0]
    assert detail_candidate["score_features"] == {"volume_score": 80}
    assert detail_candidate["score_explain"] == {"score_model": "test"}
    assert detail_candidate["raw"] == {"symbol": "ETHUSDT", "score": 77.5}


def test_v2_latest_radar_scan_prefers_live_legacy_monitor_over_stale_v2_scan(tmp_path, monkeypatch):
    db_path = tmp_path / "app.db"
    engine = build_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)

    with session_scope(SessionLocal) as session:
        session.add(
            RadarScanRecord(
                scan_id="stale-v2-scan",
                state="succeeded",
                source="v2_task",
                top50_count=1,
                market_heat=12,
            )
        )
        session.add(
            RadarCandidateRecord(
                scan_id="stale-v2-scan",
                symbol="OLDUSDT",
                rank=1,
                score=11,
                direction="SHORT",
            )
        )

    async def fake_live_payload():
        return {
            "ok": True,
            "last_scan_id": "live-scan",
            "top50": [
                {
                    "symbol": "LIVEUSDT",
                    "base_asset": "LIVE",
                    "rank": 1,
                    "score": 88,
                    "direction": "LONG",
                    "price": 1.23,
                    "change_5m": 0.6,
                    "change_15m": 1.2,
                    "change_1h": 3.4,
                    "oi_change": 2.1,
                    "fund_confirm_count": 4,
                    "fund_confirm_total": 5,
                    "fake_breakout_risk": "LOW",
                    "ai_candidate": True,
                    "market_structure": {"action": "OPEN_LONG"},
                    "score_features": {"source": "live"},
                    "score_explain": {"why": "fresh"},
                }
            ],
            "top4": [
                {
                    "symbol": "LIVEUSDT",
                    "rank": 1,
                    "score": 88,
                    "direction": "LONG",
                    "ai_candidate": True,
                }
            ],
            "scan_status": {"last_scan_time": "03:33:33"},
        }

    monkeypatch.setattr(radar_api, "fetch_live_radar_payload", fake_live_payload)
    client = TestClient(create_app(session_factory=SessionLocal, initialize_database=False))

    response = client.get("/api/v2/radar/scans/latest?include_details=true")

    assert response.status_code == 200
    data = response.json()
    assert data["ok"] is True
    assert data["scan"]["scan_id"] == "live-scan"
    assert data["scan"]["source"] == "legacy_live"
    assert data["scan"]["top50_count"] == 1
    assert data["candidates"][0]["symbol"] == "LIVEUSDT"
    assert data["candidates"][0]["score_features"] == {"source": "live"}
    assert data["candidates"][0]["raw"]["symbol"] == "LIVEUSDT"


def test_v2_dashboard_overview_summarizes_existing_radar_state(monkeypatch):
    class FakeRadarEngine:
        last_scan_id = "scan-dashboard"
        last_scan_time = "2026-06-27 14:30:00"
        market_heat = 71
        alert_count = 4
        top4 = [
            {
                "symbol": "BTCUSDT",
                "base_asset": "BTC",
                "direction": "LONG",
                "score": 88,
                "ai_candidate": True,
                "fund_confirm_count": 4,
                "fake_breakout_risk": "LOW",
                "market_structure": {"action": "OPEN_LONG", "regime": "breakout", "phase": "actionable"},
            }
        ]
        top50 = [
            top4[0],
            {
                "symbol": "ETHUSDT",
                "base_asset": "ETH",
                "direction": "SHORT",
                "score": 70,
                "ai_candidate": True,
                "fund_confirm_count": 3,
                "fake_breakout_risk": "HIGH",
                "market_structure": {"action": "WAIT", "regime": "pullback", "phase": "building"},
            },
            {
                "symbol": "SOLUSDT",
                "base_asset": "SOL",
                "direction": "NEUTRAL",
                "score": 40,
                "ai_candidate": False,
                "fund_confirm_count": 1,
                "fake_breakout_risk": "MEDIUM",
                "market_structure": {"action": "WAIT", "regime": "range_or_chop", "phase": "observation"},
            },
        ]

        def scan_status(self):
            return {
                "in_progress": False,
                "active_coins": {"active_count": 9, "active_symbols": ["BTCUSDT", "ETHUSDT"]},
                "dynamic_stream": {"active_count": 12},
            }

    monkeypatch.setattr(dashboard_api, "radar_engine", FakeRadarEngine())
    client = TestClient(create_app())

    response = client.get("/api/v2/dashboard/overview")

    assert response.status_code == 200
    data = response.json()
    assert data["ok"] is True
    assert data["state"]["code"] == "WATCH"
    assert data["metrics"] == {
        "top50_count": 3,
        "ai_candidate_count": 2,
        "actionable_count": 1,
        "average_score": 66,
        "dynamic_stream_count": 12,
        "active_coin_count": 9,
        "fund_ready_count": 2,
        "fake_high_count": 1,
    }
    assert data["direction"] == {
        "long": 1,
        "short": 1,
        "neutral": 1,
        "long_pct": 33,
        "short_pct": 33,
        "neutral_pct": 34,
    }
    assert data["candidates"][0] == {
        "symbol": "BTCUSDT",
        "base_asset": "BTC",
        "direction": "LONG",
        "score": 88,
        "action": "OPEN_LONG",
        "regime": "breakout",
        "phase": "actionable",
    }


def test_v2_dashboard_overview_prefers_live_legacy_monitor_source(monkeypatch):
    class EmptyV2RadarEngine:
        last_scan_id = ""
        last_scan_time = ""
        market_heat = 0
        alert_count = 0
        top4 = []
        top50 = []

        def scan_status(self):
            return {"active_coins": {"active_count": 0}, "dynamic_stream": {"active_count": 0}}

    async def fake_live_payload():
        return {
            "ok": True,
            "last_scan_id": "live-dashboard",
            "top50": [
                {
                    "symbol": "BTCUSDT",
                    "base_asset": "BTC",
                    "direction": "LONG",
                    "score": 90,
                    "ai_candidate": True,
                    "fund_confirm_count": 4,
                    "fake_breakout_risk": "LOW",
                    "market_structure": {"action": "OPEN_LONG", "regime": "breakout", "phase": "actionable"},
                },
                {
                    "symbol": "ETHUSDT",
                    "base_asset": "ETH",
                    "direction": "SHORT",
                    "score": 70,
                    "ai_candidate": False,
                    "fund_confirm_count": 2,
                    "fake_breakout_risk": "HIGH",
                    "market_structure": {"action": "WAIT", "regime": "pullback", "phase": "watch"},
                },
            ],
            "top4": [
                {
                    "symbol": "BTCUSDT",
                    "base_asset": "BTC",
                    "direction": "LONG",
                    "score": 90,
                    "ai_candidate": True,
                    "fund_confirm_count": 4,
                    "fake_breakout_risk": "LOW",
                    "market_structure": {"action": "OPEN_LONG", "regime": "breakout", "phase": "actionable"},
                }
            ],
            "scan_status": {
                "last_scan_time": "03:44:44",
                "active_coins": {"active_count": 120},
                "dynamic_stream": {"active_count": 120},
            },
        }

    monkeypatch.setattr(dashboard_api, "radar_engine", EmptyV2RadarEngine())
    monkeypatch.setattr(dashboard_api, "fetch_live_radar_payload", fake_live_payload)
    client = TestClient(create_app())

    response = client.get("/api/v2/dashboard/overview")

    assert response.status_code == 200
    data = response.json()
    assert data["scan"]["last_scan_id"] == "live-dashboard"
    assert data["scan"]["last_scan_time"] == "03:44:44"
    assert data["metrics"]["top50_count"] == 2
    assert data["metrics"]["active_coin_count"] == 120
    assert data["metrics"]["dynamic_stream_count"] == 120
    assert data["candidates"][0]["symbol"] == "BTCUSDT"
