param(
    [string]$MonitorBaseUrl = "http://127.0.0.1:8080",
    [string]$EnvPath = ".env",
    [int]$TimeoutSec = 300
)

$ErrorActionPreference = "Stop"

function Assert-True($Condition, [string]$Message) {
    if (-not $Condition) {
        throw $Message
    }
}

function Read-ApiToken {
    if (-not [string]::IsNullOrWhiteSpace($env:AI_RADAR_API_TOKEN)) {
        return [string]$env:AI_RADAR_API_TOKEN
    }
    if (-not (Test-Path -LiteralPath $EnvPath)) {
        return ""
    }
    $line = Select-String -Path $EnvPath -Pattern "^API_TOKEN=" -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($null -eq $line) {
        return ""
    }
    return ([string]$line.Line -replace "^API_TOKEN=", "").Trim()
}

function Get-ApiHeaders {
    $headers = @{}
    $apiToken = Read-ApiToken
    if (-not [string]::IsNullOrWhiteSpace($apiToken)) {
        $headers["X-API-Token"] = $apiToken
    }
    return $headers
}

function Read-JsonPost([string]$Url) {
    Invoke-RestMethod -Method Post -Uri $Url -Headers (Get-ApiHeaders) -TimeoutSec $TimeoutSec
}

Write-Host "==> run Codex paper sampling probe"
$probe = Read-JsonPost "$MonitorBaseUrl/api/trade-director/codex-paper-probe"

Write-Host "sampling_status=$($probe.sampling_status) ok=$($probe.ok) blocked_reason=$($probe.blocked_reason)"
Write-Host "candidate_source=$($probe.candidate_source) candidate_symbols=$($probe.candidate_symbols -join ',')"
Write-Host "codex_invocation invoked=$($probe.codex_invocation.invoked) before=$($probe.codex_invocation.before_count) after=$($probe.codex_invocation.after_count) delta=$($probe.codex_invocation.delta)"
Write-Host "graduation codex_real_closed_samples_with_radar=$($probe.graduation.codex_real_closed_samples_with_radar) real_closed_samples_with_radar=$($probe.graduation.real_closed_samples_with_radar) trust=$($probe.graduation.trust_level)"
Write-Host "safety real_order_allowed=$($probe.safety.real_order_allowed) live_trading_enabled=$($probe.safety.live_trading_enabled)"

Assert-True ($probe.safety.real_order_allowed -ne $true) "Codex paper sampling refused: real_order_allowed is true"
Assert-True ($probe.ok -eq $true) "Codex paper sampling blocked: $($probe.blocked_reason)"
Assert-True ($probe.codex_entry.provider -eq "codex_cli") "Codex paper sampling did not use codex_cli provider"
Assert-True ($probe.codex_entry.ready_for_generation -eq $true) "Codex paper sampling cannot generate: $($probe.codex_entry.availability_reason)"
$pendingClose = $probe.sampling_status -eq "OPEN_POSITION_PENDING_CLOSE"
Assert-True (($probe.codex_invocation.invoked -eq $true) -or $pendingClose) "Codex paper sampling did not invoke Codex for current candidates"

if ($pendingClose) {
    $open = @($probe.open_positions | Select-Object -First 1)[0]
    Write-Host "Codex paper position is already open: position_id=$($open.position_id) symbol=$($open.symbol) side=$($open.side). Wait for close to create the closed-loop sample."
} elseif ($probe.sampling_status -eq "OPENED") {
    Write-Host "Codex paper position opened. Wait for position_manager close to create a real closed Codex sample."
} else {
    $first = @($probe.decision_path | Select-Object -First 1)[0]
    Write-Host "No Codex paper position opened yet: decision=$($first.decision) reason=$($first.reason)"
}

$probe | ConvertTo-Json -Depth 12
