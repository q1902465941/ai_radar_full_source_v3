param(
    [string]$MonitorBaseUrl = "http://127.0.0.1:8080",
    [string]$EnvPath = ".env",
    [int]$MaxWaitSeconds = 1800,
    [int]$PollSeconds = 30,
    [switch]$NoFailOnTimeout
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

function Read-JsonGet([string]$Url) {
    Invoke-RestMethod -Method Get -Uri $Url -Headers (Get-ApiHeaders) -TimeoutSec 90
}

function Read-JsonPost([string]$Url) {
    Invoke-RestMethod -Method Post -Uri $Url -Headers (Get-ApiHeaders) -TimeoutSec 360
}

function Get-CodexSampleCount($Readiness) {
    $progress = $Readiness.paper_learning.graduation_progress
    if ($null -eq $progress) {
        return 0
    }
    return [int]$progress.codex_real_closed_samples_with_radar
}

function Get-WatchTarget($Probe) {
    $open = @($Probe.open_positions | Select-Object -First 1)
    if ($open.Count -gt 0 -and -not [string]::IsNullOrWhiteSpace([string]$open[0].position_id)) {
        return $open[0]
    }
    $path = @($Probe.decision_path | Where-Object { -not [string]::IsNullOrWhiteSpace([string]$_.position_id) } | Select-Object -First 1)
    if ($path.Count -gt 0) {
        return [pscustomobject]@{
            position_id = $path[0].position_id
            symbol = $path[0].symbol
            side = ""
        }
    }
    return $null
}

function Test-PositionStillOpen($Positions, [string]$PositionId) {
    $openRows = @($Positions.open)
    foreach ($row in $openRows) {
        if ([string]$row.position_id -eq $PositionId) {
            return $true
        }
    }
    return $false
}

$maxWait = [Math]::Max(0, $MaxWaitSeconds)
$poll = [Math]::Max(1, $PollSeconds)

Write-Host "==> watch Codex paper sample until closed evidence appears"
$probe = Read-JsonPost "$MonitorBaseUrl/api/trade-director/codex-paper-probe"
Assert-True ($probe.safety.real_order_allowed -ne $true) "watch refused: real_order_allowed is true"
Assert-True ($probe.codex_entry.provider -eq "codex_cli") "watch refused: provider is not codex_cli"
Assert-True ($probe.codex_entry.ready_for_generation -eq $true) "watch refused: Codex is not ready: $($probe.codex_entry.availability_reason)"

$initialCodexClosed = [int]$probe.graduation.codex_real_closed_samples_with_radar
$target = Get-WatchTarget $probe
Assert-True ($null -ne $target) "No Codex paper position is open or pending from probe status=$($probe.sampling_status)"
$pendingClose = $probe.sampling_status -eq "OPEN_POSITION_PENDING_CLOSE"

$positionId = [string]$target.position_id
Write-Host "watching position_id=$positionId symbol=$($target.symbol) side=$($target.side) sampling_status=$($probe.sampling_status) pending_close=$pendingClose initial_codex_real_closed_samples_with_radar=$initialCodexClosed"

$deadline = (Get-Date).AddSeconds($maxWait)
while ($true) {
    $readiness = Read-JsonGet "$MonitorBaseUrl/api/system/readiness"
    $currentCodexClosed = Get-CodexSampleCount $readiness
    $positions = Read-JsonGet "$MonitorBaseUrl/api/positions"
    $stillOpen = Test-PositionStillOpen $positions $positionId
    $elapsed = [Math]::Round($maxWait - [Math]::Max(0, ($deadline - (Get-Date)).TotalSeconds), 1)

    Write-Host "watch status elapsed=${elapsed}s position_open=$stillOpen codex_real_closed_samples_with_radar=$currentCodexClosed"

    if ($currentCodexClosed -gt $initialCodexClosed) {
        Write-Host "Codex closed-loop sample confirmed: before=$initialCodexClosed after=$currentCodexClosed"
        exit 0
    }

    if (-not $stillOpen) {
        throw "Codex paper position $positionId is no longer open, but codex_real_closed_samples_with_radar did not increase"
    }

    if ((Get-Date) -ge $deadline) {
        $message = "Timed out waiting for Codex paper position $positionId to close into a counted sample"
        if ($NoFailOnTimeout) {
            Write-Host $message
            exit 0
        }
        throw $message
    }

    Start-Sleep -Seconds $poll
}
