from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_legacy_radar_templates_do_not_poll_expensive_radar_endpoint():
    radar = (ROOT / "backend" / "web" / "templates" / "radar.html").read_text(encoding="utf-8")
    dashboard = (ROOT / "backend" / "web" / "templates" / "dashboard.html").read_text(encoding="utf-8")

    assert "setInterval(refreshRadar" not in radar
    assert "setInterval(refreshDashboardOverview" not in dashboard


def test_legacy_radar_uses_condition_based_refresh_while_scan_runs():
    app_js = (ROOT / "backend" / "web" / "static" / "app.js").read_text(encoding="utf-8")

    assert "function scheduleRadarRefresh" in app_js
    assert "window.setTimeout" in app_js
    assert "radar_scan_running_no_cache" in app_js
    assert "radar_scan_warming_up" in app_js
    assert "radar_scan_already_running" in app_js
