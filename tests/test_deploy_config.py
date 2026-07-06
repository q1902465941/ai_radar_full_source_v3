from pathlib import Path
import subprocess


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


def test_docker_compose_passes_mainnet_market_runtime_env_to_backend_services():
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")

    assert "MARKET_DATA_MODE: ${MARKET_DATA_MODE:-binance}" in compose
    assert "BINANCE_TESTNET: ${BINANCE_TESTNET:-false}" in compose
    assert "BINANCE_MARKET_FALLBACK_TESTNET: ${BINANCE_MARKET_FALLBACK_TESTNET:-false}" in compose
    assert "BINANCE_WS_ENABLED: ${BINANCE_WS_ENABLED:-true}" in compose
    assert "BINANCE_ASCII_SYMBOLS_ONLY: ${BINANCE_ASCII_SYMBOLS_ONLY:-true}" in compose
    assert "BINANCE_API_KEY: ${BINANCE_API_KEY:-}" in compose
    assert "BINANCE_API_SECRET: ${BINANCE_API_SECRET:-}" in compose
    assert "AUTO_TRADING_CANDIDATE_MODE: ${AUTO_TRADING_CANDIDATE_MODE:-paper_top}" in compose
    assert "AUTO_TRADING_CANDIDATE_MIN_SCORE: ${AUTO_TRADING_CANDIDATE_MIN_SCORE:-70}" in compose
    assert "AUTO_TRADING_CANDIDATE_LIMIT: ${AUTO_TRADING_CANDIDATE_LIMIT:-1}" in compose
    assert "PAPER_PROBE_ENABLED: ${PAPER_PROBE_ENABLED:-true}" in compose
    assert "PAPER_PROBE_MIN_SCORE_FLOOR: ${PAPER_PROBE_MIN_SCORE_FLOOR:-18}" in compose
    assert "PAPER_PROBE_MIN_FUND_CONFIRM: ${PAPER_PROBE_MIN_FUND_CONFIRM:-1}" in compose
    assert "PAPER_PROBE_MIN_DIRECTION_CONFIRMATIONS: ${PAPER_PROBE_MIN_DIRECTION_CONFIRMATIONS:-4}" in compose
    assert "PAPER_PROBE_MAX_WICK_RATIO: ${PAPER_PROBE_MAX_WICK_RATIO:-0.55}" in compose
    assert "MONITOR_LEGACY_BACKEND_URL: ${MONITOR_LEGACY_BACKEND_URL:-http://backend:8001}" in compose
    assert "MONITOR_LEGACY_DB_FALLBACK_ENABLED: ${MONITOR_LEGACY_DB_FALLBACK_ENABLED:-true}" in compose
    assert "AI_STRATEGY_PROVIDER: ${AI_STRATEGY_PROVIDER:-rule}" in compose
    assert "REQUIRE_CODEX_STRATEGY_FOR_ENTRY: ${REQUIRE_CODEX_STRATEGY_FOR_ENTRY:-false}" in compose
    assert "OPENAI_API_KEY: ${OPENAI_API_KEY:-}" in compose
    assert "CODEX_HOME: /root/.codex" in compose
    assert "CODEX_COMMAND: ${DOCKER_CODEX_COMMAND:-codex}" in compose
    assert "CODEX_MODEL: ${CODEX_MODEL:-}" in compose
    assert "CODEX_MODEL_PROVIDER: ${CODEX_MODEL_PROVIDER:-}" in compose
    assert "CODEX_PROVIDER_NAME: ${CODEX_PROVIDER_NAME:-ChatGPT HTTP}" in compose
    assert "CODEX_PROVIDER_REQUIRES_OPENAI_AUTH: ${CODEX_PROVIDER_REQUIRES_OPENAI_AUTH:-true}" in compose
    assert "CODEX_PROVIDER_SUPPORTS_WEBSOCKETS: ${CODEX_PROVIDER_SUPPORTS_WEBSOCKETS:-false}" in compose
    assert "CODEX_TIMEOUT_SECONDS: ${CODEX_TIMEOUT_SECONDS:-90}" in compose
    assert "CODEX_REASONING_EFFORT: ${CODEX_REASONING_EFFORT:-medium}" in compose
    assert "CODEX_SERVICE_TIER: ${CODEX_SERVICE_TIER:-fast}" in compose
    assert "CODEX_FAST_MODEL: ${CODEX_FAST_MODEL:-}" in compose
    assert "CODEX_QA_MODEL: ${CODEX_QA_MODEL:-}" in compose
    assert "CODEX_EVOLVE_MODEL: ${CODEX_EVOLVE_MODEL:-}" in compose
    assert "CODEX_COMMAND: ${CODEX_COMMAND:-}" not in compose
    assert "CODEX_CLI_VERSION: ${CODEX_CLI_VERSION:-0.130.0}" in compose
    assert "INSTALL_CODEX_CLI: ${INSTALL_CODEX_CLI:-true}" in compose
    assert "${DOCKER_CODEX_HOME:-./.codex-docker}:/root/.codex" in compose
    assert "${DOCKER_CODEX_HOME:-./.codex-docker}:/root/.codex:ro" not in compose
    assert "DB_PATH: ${DOCKER_DB_PATH:-data/ai_radar.db}" in compose
    assert "DB_PATH: ${DB_PATH:-data/ai_radar.db}" not in compose


def test_docker_compose_runs_legacy_monitor_as_single_background_worker():
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")

    assert "backend.main:app -k uvicorn.workers.UvicornWorker -b 0.0.0.0:8001 --workers 1 --timeout 300" in compose


def test_frontend_nginx_routes_legacy_monitor_by_default_and_v2_api_separately():
    nginx = (ROOT / "frontend" / "nginx.conf").read_text(encoding="utf-8")

    assert "proxy_pass http://api-v2:8002/api/v2/;" in nginx
    assert "proxy_pass http://backend:8001/api/;" in nginx
    assert "proxy_pass http://backend:8001;" in nginx
    assert "try_files $uri $uri/ /index.html" not in nginx


def test_frontend_nginx_allows_long_running_api_calls():
    nginx = (ROOT / "frontend" / "nginx.conf").read_text(encoding="utf-8")

    assert "proxy_read_timeout 300s;" in nginx
    assert "proxy_send_timeout 300s;" in nginx


def test_strategy_ai_page_surfaces_tradable_strategy_audit():
    template = (ROOT / "backend" / "web" / "templates" / "strategy_ai.html").read_text(encoding="utf-8")

    assert "tradable_strategy_count" in template
    assert "tradable_strategy_by_source" in template
    assert "tradable_strategy_by_source_provider" in template
    assert "invalid_strategy_count" in template
    assert "last_tradable_strategy" in template
    assert "candidate_source" in template
    assert "recent_strategy_tasks" in template


def test_env_example_defaults_to_mainnet_public_market_data():
    env_example = (ROOT / ".env.example").read_text(encoding="utf-8")

    assert "MARKET_DATA_MODE=binance" in env_example
    assert "BINANCE_TESTNET=false" in env_example
    assert "BINANCE_MARKET_FALLBACK_TESTNET=false" in env_example
    assert "BINANCE_ASCII_SYMBOLS_ONLY=true" in env_example
    assert "AI_STRATEGY_PROVIDER=rule" in env_example
    assert "REQUIRE_CODEX_STRATEGY_FOR_ENTRY=false" in env_example
    assert "CODEX_COMMAND=codex" in env_example
    assert "DOCKER_CODEX_COMMAND=codex" in env_example
    assert "INSTALL_CODEX_CLI=true" in env_example
    assert "CODEX_CLI_VERSION=0.130.0" in env_example
    assert "DOCKER_CODEX_HOME=.codex-docker" in env_example
    assert "DOCKER_DB_PATH=data/ai_radar.db" in env_example
    assert "MONITOR_LEGACY_BACKEND_URL=" in env_example
    assert "MONITOR_LEGACY_BACKEND_URL=http://backend:8001" not in env_example
    assert "MARKET_DATA_MODE=mock" not in env_example
    assert "BINANCE_TESTNET=true" not in env_example


def test_docker_stack_verification_script_checks_monitor_and_mainnet_market_data():
    script = (ROOT / "scripts" / "verify_docker_stack.ps1").read_text(encoding="utf-8")
    monitor_js = (ROOT / "backend" / "web" / "static" / "app.js").read_text(encoding="utf-8")

    assert '[string]$MonitorBaseUrl = "http://127.0.0.1:8080"' in script
    assert "Read-HttpTextWithRetry" in script
    assert "Start-Sleep -Seconds" in script
    assert "$MonitorBaseUrl/radar" in script
    assert "AI RADAR SYSTEM" in script
    assert "AI Radar Control Center" in script
    assert "market_data_source" in script
    assert "mainnet" in script
    assert "market_refresh.degraded" in script
    assert "warning=${market.warning}" in monitor_js
    assert "market.warning ? 'WARN ' : ''" in monitor_js
    assert "entry_enforced" in monitor_js
    assert "entry_enforcement_reason" in monitor_js
    assert "codex_strategy_not_enforced_for_live_intent" in script
    assert "Codex entry enforcement" in script
    assert "https://fapi.binance.com/fapi/v1/ticker/price?symbol=$encodedSymbol" in script
    assert "api/trade-director/acceptance/paper-cycle" in script
    assert "learning_open_recorded" in script
    assert "learning_close_recorded" in script
    assert "real_order_allowed" in script
    assert "graduation_progress" in script
    assert "missing_real_closed_samples" in script
    assert "codex_real_closed_samples_with_radar" in script
    assert "real_closed_samples_by_provider" in script
    assert "codex_real_closed_samples_with_radar" in monitor_js
    assert "real_closed_samples_by_provider" in monitor_js
    assert "active ticker candidate coverage" in script
    assert "Get-BinanceRankedTickerCandidates" in script
    assert "https://fapi.binance.com/fapi/v1/exchangeInfo" in script
    assert "market symbols use supported USD-M ASCII contracts" in script
    assert "Test-AsciiSymbol" in script
    assert "EscapeDataString" in script
    assert "database path uses mounted Docker volume" in script


def test_codex_strategy_generation_verification_script_runs_real_docker_codex_path():
    script = (ROOT / "scripts" / "verify_codex_strategy_generation.ps1").read_text(encoding="utf-8")
    module = (ROOT / "backend" / "ai_strategy" / "codex_generation_acceptance.py").read_text(encoding="utf-8")

    assert "docker compose run" in script
    assert "--no-deps" in script
    assert "-e AI_ENABLED=true" in script
    assert "-e AI_STRATEGY_PROVIDER=codex_cli" in script
    assert "-e REQUIRE_CODEX_STRATEGY_FOR_ENTRY=true" in script
    assert "python -m backend.ai_strategy.codex_generation_acceptance" in script

    assert "ai_service.generate_strategy" in module
    assert "strategy_validator.validate" in module
    assert "contract_quality" in module
    assert "codex_real_strategy_generated" in module
    assert "tradable_strategy_count" in module
    assert "tradable_strategy_by_source" in module
    assert "tradable_strategy_by_source_provider" in module
    assert "production_acceptance" in module
    assert "ai_task_audit_missing_production_acceptance_codex_tradable_strategy" in module
    assert "last_tradable_strategy" in module
    assert "recent_strategy_tasks" in module
    assert "ai_task_audit_missing_current_production_acceptance_tradable_strategy" in module
    assert '"OPEN_LONG"' in module
    assert "codex_cli_unavailable" in module
    assert "strategy_contract_quality" in module


def test_enable_codex_strategy_mode_script_persists_host_env_without_leaking_secrets(tmp_path):
    script = ROOT / "scripts" / "enable_codex_strategy_mode.ps1"
    env_path = tmp_path / ".env"
    env_path.write_text(
        "\n".join(
            [
                "API_TOKEN=secret-token",
                "BINANCE_API_SECRET=secret-binance",
                "AI_ENABLED=false",
                "AI_STRATEGY_PROVIDER=rule",
                "REQUIRE_CODEX_STRATEGY_FOR_ENTRY=false",
                "LIVE_TRADING_ENABLED=true",
                "LIVE_USE_TEST_ORDER=false",
                "UNRELATED_SETTING=keep-me",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    result = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(script),
            "-EnvPath",
            str(env_path),
            "-NoApiCall",
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert result.returncode == 0, result.stderr
    output = result.stdout + result.stderr
    raw = env_path.read_bytes()
    updated = env_path.read_text(encoding="utf-8")
    assert not raw.startswith(b"\xef\xbb\xbf")
    assert "AI_ENABLED=true" in updated
    assert "AI_STRATEGY_PROVIDER=codex_cli" in updated
    assert "REQUIRE_CODEX_STRATEGY_FOR_ENTRY=true" in updated
    assert "LIVE_TRADING_ENABLED=false" in updated
    assert "LIVE_USE_TEST_ORDER=true" in updated
    assert "UNRELATED_SETTING=keep-me" in updated
    assert "API_TOKEN=secret-token" in updated
    assert "BINANCE_API_SECRET=secret-binance" in updated
    assert "secret-token" not in output
    assert "secret-binance" not in output


def test_dockerignore_preserves_data_artifacts_for_backend_image():
    dockerignore = (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()

    assert "data/" not in dockerignore
    assert "data/*.db" in dockerignore
    assert "data/*.sqlite" in dockerignore
    assert "data/*.sqlite3" in dockerignore


def test_gitignore_excludes_local_codex_mount_directory():
    gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8")

    assert ".codex-docker/" in gitignore


def test_backend_dockerfile_uses_production_web_server():
    dockerfile = (ROOT / "Dockerfile.backend").read_text(encoding="utf-8")

    assert "ARG PYTHON_IMAGE=python:3.12-slim" in dockerfile
    assert "gunicorn" in dockerfile
    assert "uvicorn.workers.UvicornWorker" in dockerfile


def test_backend_dockerfile_installs_codex_cli_for_strategy_generation():
    dockerfile = (ROOT / "Dockerfile.backend").read_text(encoding="utf-8")

    assert "ARG INSTALL_CODEX_CLI=true" in dockerfile
    assert "ARG CODEX_CLI_VERSION=0.130.0" in dockerfile
    assert "Acquire::Retries=5" in dockerfile
    assert "https://deb.nodesource.com/node_24.x" in dockerfile
    assert 'npm install -g "@openai/codex@${CODEX_CLI_VERSION}"' in dockerfile
    assert "codex --version" in dockerfile


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
    assert "[int]$BackendSmokePort = 8011" in script
    assert "[int]$FrontendSmokePort = 4183" in script
    assert "APP_PORT = [string]($BackendSmokePort - 1)" in script
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
    assert "[int]$BackendPort = 8011" in start_script
    assert "[int]$FrontendPort = 4183" in start_script
    assert "npm run build" in start_script
    assert "vite" in start_script and "preview" in start_script
    assert "/api/v2/health" in start_script
    assert "local_stack.json" in start_script
    assert "APP_PORT" in start_script
    assert "local_stack.json" in stop_script
    assert "[int]$BackendPort = 8011" in stop_script
    assert "[int]$FrontendPort = 4183" in stop_script
    assert "Get-NetTCPConnection" in stop_script
    assert "Stop-Process" in stop_script


def test_landing_status_documents_verified_paths_and_remaining_blocker():
    status = (ROOT / "docs" / "landing-status.md").read_text(encoding="utf-8")

    assert "scripts/verify_local.ps1" in status
    assert "scripts/start_local_stack.ps1" in status
    assert "scripts/check_docker_prereqs.ps1" in status
    assert "scripts/verify_docker_stack.ps1" in status
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
