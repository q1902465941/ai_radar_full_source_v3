from fastapi.testclient import TestClient

import backend.app.api.radar as radar_api
import backend.app.api.dashboard as dashboard_api
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
