from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_legacy_radar_templates_do_not_poll_expensive_radar_endpoint():
    radar = (ROOT / "backend" / "web" / "templates" / "radar.html").read_text(encoding="utf-8")
    dashboard = (ROOT / "backend" / "web" / "templates" / "dashboard.html").read_text(encoding="utf-8")

    assert "setInterval(refreshRadar" not in radar
    assert "setInterval(refreshDashboardOverview" not in dashboard
