from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_docker_compose_defines_frontend_backend_and_migrations():
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")

    assert "backend:" in compose
    assert "api-v2:" in compose
    assert "frontend:" in compose
    assert "alembic upgrade head" in compose
    assert "exec gunicorn backend.main:app -k uvicorn.workers.UvicornWorker -b 0.0.0.0:8001" in compose
    assert "exec gunicorn backend.app.main:app -k uvicorn.workers.UvicornWorker -b 0.0.0.0:8002" in compose
    assert "command: >" not in compose
    assert "8001:8001" in compose
    assert "8002:8002" in compose
    assert "8080:80" in compose


def test_docker_compose_defines_service_healthchecks():
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")

    assert "/api/state" in compose
    assert "/api/v2/health" in compose
    assert "condition: service_healthy" in compose
    assert "healthcheck:" in compose


def test_frontend_nginx_routes_legacy_monitor_by_default_and_v2_api_separately():
    nginx = (ROOT / "frontend" / "nginx.conf").read_text(encoding="utf-8")

    assert "proxy_pass http://api-v2:8002/api/v2/;" in nginx
    assert "proxy_pass http://backend:8001/api/;" in nginx
    assert "proxy_pass http://backend:8001;" in nginx
    assert "try_files $uri $uri/ /index.html" not in nginx


def test_dockerignore_preserves_data_artifacts_for_backend_image():
    dockerignore = (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()

    assert "data/" not in dockerignore
    assert "data/*.db" in dockerignore
    assert "data/*.sqlite" in dockerignore
    assert "data/*.sqlite3" in dockerignore


def test_backend_dockerfile_uses_production_web_server():
    dockerfile = (ROOT / "Dockerfile.backend").read_text(encoding="utf-8")

    assert "ARG PYTHON_IMAGE=python:3.12-slim" in dockerfile
    assert "gunicorn" in dockerfile
    assert "uvicorn.workers.UvicornWorker" in dockerfile


def test_frontend_dockerfile_allows_base_image_overrides():
    dockerfile = (ROOT / "frontend" / "Dockerfile").read_text(encoding="utf-8")
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")

    assert "ARG NODE_IMAGE=node:24-alpine" in dockerfile
    assert "ARG NGINX_IMAGE=nginx:1.29-alpine" in dockerfile
    assert "PYTHON_IMAGE: ${PYTHON_IMAGE:-python:3.12-slim}" in compose
    assert "NODE_IMAGE: ${NODE_IMAGE:-node:24-alpine}" in compose
    assert "NGINX_IMAGE: ${NGINX_IMAGE:-nginx:1.29-alpine}" in compose


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


def test_enable_wsl_prereq_script_self_elevates_and_runs_wsl_install():
    script = (ROOT / "scripts" / "enable_wsl_prereq.ps1").read_text(encoding="utf-8")

    assert "Start-Process" in script
    assert "-Verb RunAs" in script
    assert "wsl --install --no-distribution" in script
    assert "check_docker_prereqs.ps1" in script
    assert "Restart-Computer" not in script
    assert "docker compose up" not in script


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


def test_landing_status_documents_verified_paths_and_remaining_blocker():
    status = (ROOT / "docs" / "landing-status.md").read_text(encoding="utf-8")

    assert "scripts/verify_local.ps1" in status
    assert "scripts/start_local_stack.ps1" in status
    assert "scripts/check_docker_prereqs.ps1" in status
    assert "WSL optional component" in status
    assert "pushed to `origin/main`" in status
    assert "scripts/enable_wsl_prereq.ps1" in status


def test_github_actions_ci_runs_landing_verification():
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")

    assert "pull_request" in workflow
    assert "push" in workflow
    assert "windows-latest" in workflow
    assert "actions/setup-python" in workflow
    assert "python-version: '3.12'" in workflow
    assert "actions/setup-node" in workflow
    assert "node-version: '24'" in workflow
    assert "npm ci" in workflow
    assert "scripts\\verify_local.ps1" in workflow
    assert "docker compose config --quiet" in workflow
