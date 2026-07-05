param(
    [string]$MonitorBaseUrl = "http://127.0.0.1:8080",
    [string]$V2BaseUrl = "http://127.0.0.1:8002",
    [int]$MaxPriceDriftPct = 5,
    [string]$ApiToken = "",
    [switch]$SkipPaperAcceptance,
    [switch]$SkipExternalBinanceCheck
)

$ErrorActionPreference = "Stop"

function Invoke-Step {
    param(
        [string]$Name,
        [scriptblock]$Command
    )
    Write-Host "==> $Name"
    & $Command
}

function Assert-True {
    param(
        [bool]$Condition,
        [string]$Message
    )
    if (-not $Condition) {
        throw $Message
    }
}

function Read-Json {
    param([string]$Url)
    return Invoke-RestMethod -Uri $Url -TimeoutSec 15
}

function Read-ApiToken {
    if (-not [string]::IsNullOrWhiteSpace($ApiToken)) {
        return $ApiToken
    }
    if (-not (Test-Path ".env")) {
        return ""
    }
    $line = Select-String -Path ".env" -Pattern "^API_TOKEN=" -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($null -eq $line) {
        return ""
    }
    return ($line.Line -replace "^API_TOKEN=", "")
}

function Invoke-PostJson {
    param(
        [string]$Url,
        [object]$Body = $null
    )
    $headers = @{}
    $token = Read-ApiToken
    if (-not [string]::IsNullOrWhiteSpace($token)) {
        $headers["X-API-Token"] = $token
    }
    if ($null -eq $Body) {
        return Invoke-RestMethod -Method Post -Uri $Url -Headers $headers -TimeoutSec 120
    }
    $json = $Body | ConvertTo-Json -Depth 10
    return Invoke-RestMethod -Method Post -Uri $Url -Headers $headers -Body $json -ContentType "application/json" -TimeoutSec 120
}

Invoke-Step "compose config" {
    docker compose config --quiet
}

Invoke-Step "legacy monitor page" {
    $response = Invoke-WebRequest -Uri "$MonitorBaseUrl/radar" -TimeoutSec 15
    Assert-True ($response.StatusCode -eq 200) "monitor /radar did not return HTTP 200"
    Assert-True ($response.Content.Contains("AI RADAR SYSTEM")) "monitor page is missing legacy AI RADAR SYSTEM branding"
    Assert-True (-not $response.Content.Contains("AI Radar Control Center")) "monitor page is still serving the React migration shell"
}

Invoke-Step "v2 api health" {
    $health = Read-Json "$V2BaseUrl/api/v2/health"
    Assert-True ($health.ok -eq $true) "v2 health did not return ok=true"
    Assert-True ($health.service -eq "ai-radar-api") "v2 health returned an unexpected service"
}

Invoke-Step "monitor state uses mainnet market data" {
    $state = Read-Json "$MonitorBaseUrl/api/state"
    Assert-True ($state.market_data_source -eq "mainnet") "market_data_source is '$($state.market_data_source)', expected mainnet"
    $btc = $state.major | Where-Object { $_.symbol -eq "BTCUSDT" } | Select-Object -First 1
    Assert-True ($null -ne $btc) "BTCUSDT is missing from monitor major market cards"
    Assert-True ([double]$btc.price -gt 0) "BTCUSDT monitor price is not positive"
}

if (-not $SkipExternalBinanceCheck) {
    Invoke-Step "mainnet BTC price drift" {
        $state = Read-Json "$MonitorBaseUrl/api/state"
        $btc = $state.major | Where-Object { $_.symbol -eq "BTCUSDT" } | Select-Object -First 1
        $binance = Read-Json "https://fapi.binance.com/fapi/v1/ticker/price?symbol=BTCUSDT"
        $monitorPrice = [double]$btc.price
        $binancePrice = [double]$binance.price
        Assert-True ($binancePrice -gt 0) "Binance mainnet BTC price is not positive"
        $driftPct = [Math]::Abs(($monitorPrice - $binancePrice) / $binancePrice * 100.0)
        Assert-True ($driftPct -le $MaxPriceDriftPct) "BTCUSDT monitor price drift $([Math]::Round($driftPct, 3))% exceeds $MaxPriceDriftPct%"
    }
}

Invoke-Step "radar data present" {
    $scan = Invoke-RestMethod -Method Post -Uri "$MonitorBaseUrl/api/radar/scan-now" -TimeoutSec 15
    Assert-True ($scan.ok -eq $true) "scan-now did not return ok=true"
    $radar = $null
    for ($i = 0; $i -lt 60; $i++) {
        Start-Sleep -Seconds 1
        $radar = Read-Json "$MonitorBaseUrl/api/radar"
        $count = @($radar.top50).Count
        if ($count -gt 0 -and -not $radar.scan_status.in_progress) {
            break
        }
    }
    $count = @($radar.top50).Count
    Assert-True ($count -gt 0) "radar top50 is empty"
    Assert-True ($radar.scan_status.market_refresh.source -ne "") "radar market refresh source is empty"
    Assert-True ($radar.scan_status.market_refresh.degraded -eq $false) "radar market_refresh.degraded is true: $($radar.scan_status.market_refresh.error)"
    Assert-True ([string]::IsNullOrWhiteSpace($radar.scan_status.market_refresh.error)) "radar market refresh error is not empty: $($radar.scan_status.market_refresh.error)"
}

if (-not $SkipPaperAcceptance) {
    Invoke-Step "controlled paper closed loop" {
        $acceptance = Invoke-PostJson "$MonitorBaseUrl/api/trade-director/acceptance/paper-cycle"
        Assert-True ($acceptance.ok -eq $true) "controlled paper acceptance did not return ok=true"
        Assert-True ($acceptance.real_order_allowed -eq $false) "controlled paper acceptance unexpectedly allows real orders"
        $stageMap = @{}
        foreach ($stage in @($acceptance.stages)) {
            $stageMap[$stage.name] = [bool]$stage.ok
        }
        foreach ($required in @("strategy_plan", "risk_model", "paper_open", "position_manager_active", "paper_close", "learning_open_recorded", "learning_close_recorded")) {
            Assert-True ($stageMap[$required] -eq $true) "controlled paper stage '$required' did not pass"
        }
        Assert-True ([int]$acceptance.position_delta.open_positions_after -eq 0) "controlled paper acceptance left open positions behind"
    }
}

Write-Host "docker stack verification complete"
