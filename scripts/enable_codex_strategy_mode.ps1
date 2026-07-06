param(
    [string]$EnvPath = ".env",
    [string]$MonitorBaseUrl = "http://127.0.0.1:8080",
    [switch]$NoApiCall
)

$ErrorActionPreference = "Stop"

function Read-EnvMap {
    param([string]$Path)
    $values = @{}
    if (-not (Test-Path -LiteralPath $Path)) {
        return $values
    }
    foreach ($line in Get-Content -LiteralPath $Path) {
        if ($line -match '^\s*#' -or [string]::IsNullOrWhiteSpace($line)) {
            continue
        }
        if ($line -match '^\s*([^=\s]+)\s*=\s*(.*)\s*$') {
            $values[$Matches[1]] = $Matches[2].Trim()
        }
    }
    return $values
}

function Set-EnvValues {
    param(
        [string]$Path,
        [hashtable]$Updates
    )
    $lines = @()
    $seen = @{}
    if (Test-Path -LiteralPath $Path) {
        foreach ($line in Get-Content -LiteralPath $Path) {
            if ($line -match '^\s*([^#=\s]+)\s*=') {
                $key = $Matches[1]
                if ($Updates.ContainsKey($key)) {
                    $lines += "$key=$($Updates[$key])"
                    $seen[$key] = $true
                    continue
                }
            }
            $lines += $line
        }
    }
    foreach ($key in @($Updates.Keys | Sort-Object)) {
        if (-not $seen.ContainsKey($key)) {
            $lines += "$key=$($Updates[$key])"
        }
    }
    $parent = Split-Path -Parent $Path
    if (-not [string]::IsNullOrWhiteSpace($parent) -and -not (Test-Path -LiteralPath $parent)) {
        New-Item -ItemType Directory -Path $parent | Out-Null
    }
    $content = ($lines -join [Environment]::NewLine) + [Environment]::NewLine
    $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
    $fullPath = [System.IO.Path]::GetFullPath($Path)
    [System.IO.File]::WriteAllText($fullPath, $content, $utf8NoBom)
}

$resolvedEnvPath = $EnvPath
$updates = @{
    "AI_ENABLED" = "true"
    "AI_STRATEGY_PROVIDER" = "codex_cli"
    "REQUIRE_CODEX_STRATEGY_FOR_ENTRY" = "true"
    "LIVE_TRADING_ENABLED" = "false"
    "LIVE_USE_TEST_ORDER" = "true"
}

Set-EnvValues -Path $resolvedEnvPath -Updates $updates
Write-Output "Host env updated for Codex strategy enforcement: $resolvedEnvPath"
Write-Output "Safety: LIVE_TRADING_ENABLED=false LIVE_USE_TEST_ORDER=true"

if ($NoApiCall) {
    Write-Output "Runtime API call skipped."
    exit 0
}

$envValues = Read-EnvMap -Path $resolvedEnvPath
$apiToken = [string]$envValues["API_TOKEN"]
if ([string]::IsNullOrWhiteSpace($apiToken)) {
    Write-Output "API_TOKEN is not configured in env file; host env was updated, runtime API call skipped."
    exit 0
}

$url = ($MonitorBaseUrl.TrimEnd("/") + "/api/config/codex-strategy")
$response = Invoke-RestMethod -Method Post -Uri $url -Headers @{ "X-API-Token" = $apiToken } -ContentType "application/json" -Body "{}"
$codex = $response.codex_entry
$safety = $response.safety
Write-Output "Runtime Codex mode: provider=$($codex.provider) entry_enforced=$($codex.entry_enforced) reason=$($codex.entry_enforcement_reason)"
Write-Output "Runtime safety: live_trading_enabled=$($safety.live_trading_enabled) live_use_test_order=$($safety.live_use_test_order) real_order_allowed=$($safety.real_order_allowed)"
