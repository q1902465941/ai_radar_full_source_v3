param(
    [int]$TimeoutSeconds = 240,
    [string]$Service = "backend"
)

$ErrorActionPreference = "Stop"

Write-Host "==> build backend image for Codex strategy acceptance"
docker compose build $Service
if ($LASTEXITCODE -ne 0) {
    throw "docker compose build $Service failed with exit code $LASTEXITCODE"
}

Write-Host "==> docker compose run real Codex strategy generation acceptance"
# Equivalent command surface: docker compose run --rm --no-deps -e AI_ENABLED=true -e AI_STRATEGY_PROVIDER=codex_cli -e REQUIRE_CODEX_STRATEGY_FOR_ENTRY=true backend python -m backend.ai_strategy.codex_generation_acceptance
$dockerArgs = @(
    "compose",
    "run",
    "--rm",
    "--no-deps",
    "-e", "AI_ENABLED=true",
    "-e", "AI_STRATEGY_PROVIDER=codex_cli",
    "-e", "REQUIRE_CODEX_STRATEGY_FOR_ENTRY=true",
    "-e", "CODEX_HOME=/root/.codex",
    "-e", "CODEX_COMMAND=codex",
    "-e", "CODEX_MODEL_PROVIDER=chatgpt_http",
    "-e", "CODEX_PROVIDER_NAME=ChatGPT HTTP",
    "-e", "CODEX_PROVIDER_REQUIRES_OPENAI_AUTH=true",
    "-e", "CODEX_PROVIDER_SUPPORTS_WEBSOCKETS=false",
    "-e", "CODEX_TIMEOUT_SECONDS=$TimeoutSeconds",
    "-e", "CODEX_REASONING_EFFORT=medium",
    "-e", "CODEX_SERVICE_TIER=fast",
    $Service,
    "python",
    "-m",
    "backend.ai_strategy.codex_generation_acceptance"
)

& docker @dockerArgs
if ($LASTEXITCODE -ne 0) {
    throw "Codex strategy generation acceptance failed with exit code $LASTEXITCODE"
}

Write-Host "==> Codex strategy generation acceptance complete"
