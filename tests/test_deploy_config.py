from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_docker_compose_defines_frontend_backend_and_migrations():
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")

    assert "backend:" in compose
    assert "frontend:" in compose
    assert "alembic upgrade head" in compose
    assert "8001:8001" in compose
    assert "8080:80" in compose


def test_docker_compose_defines_service_healthchecks():
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")

    assert "/api/v2/health" in compose
    assert "condition: service_healthy" in compose
    assert "healthcheck:" in compose


def test_dockerignore_preserves_data_artifacts_for_backend_image():
    dockerignore = (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()

    assert "data/" not in dockerignore
    assert "data/*.db" in dockerignore
    assert "data/*.sqlite" in dockerignore
    assert "data/*.sqlite3" in dockerignore


def test_backend_dockerfile_uses_production_web_server():
    dockerfile = (ROOT / "Dockerfile.backend").read_text(encoding="utf-8")

    assert "gunicorn" in dockerfile
    assert "uvicorn.workers.UvicornWorker" in dockerfile


def test_backend_dockerfile_copies_hedge_runtime_backend_packages():
    dockerfile = (ROOT / "Dockerfile.backend").read_text(encoding="utf-8")

    for package in ("runtime", "strategy", "meta", "portfolio", "execution", "broker"):
        assert f"COPY {package} ./{package}" in dockerfile
    assert "COPY data ./data" in dockerfile
    assert "COPY learning ./learning" in dockerfile


def test_local_verification_script_covers_backend_frontend_and_smoke_checks():
    script = (ROOT / "scripts" / "verify_local.ps1").read_text(encoding="utf-8")

    assert "pytest -q" in script
    assert "npm test -- --run" in script
    assert "npm run build" in script
    assert "/api/v2/health" in script
    assert "vite" in script and "preview" in script


def test_docker_prereq_script_reports_wsl_and_daemon_state():
    script = (ROOT / "scripts" / "check_docker_prereqs.ps1").read_text(encoding="utf-8")

    assert "wsl --status" in script
    assert "docker info" in script
    assert "docker compose config --quiet" in script
    assert "WSL optional component" in script


def test_local_stack_scripts_start_and_stop_production_style_services():
    start_script = (ROOT / "scripts" / "start_local_stack.ps1").read_text(encoding="utf-8")
    stop_script = (ROOT / "scripts" / "stop_local_stack.ps1").read_text(encoding="utf-8")

    assert "run_v2.py" in start_script
    assert "npm run build" in start_script
    assert "vite" in start_script and "preview" in start_script
    assert "/api/v2/health" in start_script
    assert "local_stack.json" in start_script
    assert "APP_PORT" in start_script
    assert "local_stack.json" in stop_script
    assert "Get-NetTCPConnection" in stop_script
    assert "Stop-Process" in stop_script
