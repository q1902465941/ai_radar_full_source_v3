from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_legacy_monitor_renders_graduation_progress_card():
    script = (ROOT / "backend" / "web" / "static" / "app.js").read_text(encoding="utf-8")

    assert "graduation_progress" in script
    assert "Graduation" in script
    assert "missing_real_closed_samples" in script
    assert "codex_real_closed_samples_with_radar" in script
    assert "codex_missing_real_closed_samples" in script
    assert "real_closed_samples_by_provider" in script


def test_legacy_monitor_renders_codex_generation_readiness():
    script = (ROOT / "backend" / "web" / "static" / "app.js").read_text(encoding="utf-8")

    assert "ready_for_generation" in script
    assert "availability_reason" in script


def test_legacy_monitor_renders_codex_countability_for_open_positions():
    script = (ROOT / "backend" / "web" / "static" / "app.js").read_text(encoding="utf-8")

    assert "learning_countability" in script
    assert "will_count_when_closed" in script
    assert "blocking_reasons" in script
    assert "countable_close_reasons" in script
