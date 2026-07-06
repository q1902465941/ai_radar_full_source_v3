param(
    [string]$MonitorBaseUrl = "http://127.0.0.1:8080",
    [string]$V2BaseUrl = "http://127.0.0.1:8002",
    [double]$MaxPriceDriftPct = 5,
    [double]$RadarMaxPriceDriftPct = 1.0,
    [double]$MaxMajorChangeDriftPct = 0.25,
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

function Get-MonitorMajor {
    param(
        [object]$State,
        [string]$Symbol
    )
    return $State.major | Where-Object { $_.symbol -eq $Symbol } | Select-Object -First 1
}

function Assert-MonitorMajorChangeAgainstBinance {
    param([string]$Symbol = "BTCUSDT")
    if ($SkipExternalBinanceCheck) {
        return
    }
    $state = $null
    $major = $null
    for ($i = 0; $i -lt 20; $i++) {
        $state = Read-Json "$MonitorBaseUrl/api/state"
        $major = Get-MonitorMajor -State $state -Symbol $Symbol
        if ($null -ne $major -and $null -ne $major.PSObject.Properties["change_24h"] -and $null -ne $major.change_24h) {
            break
        }
        Start-Sleep -Seconds 1
    }
    Assert-True ($null -ne $major) "$Symbol is missing from monitor major market cards"
    Assert-True ($null -ne $major.PSObject.Properties["change_24h"] -and $null -ne $major.change_24h) "$Symbol monitor 24h change is missing"
    Assert-True ($major.change_source -ne "unavailable") "$Symbol monitor change source is unavailable"
    $binance = Read-Json "https://fapi.binance.com/fapi/v1/ticker/24hr?symbol=$Symbol"
    $monitorChange = [double]$major.change_24h
    $binanceChange = [double]$binance.priceChangePercent
    $driftPct = [Math]::Abs($monitorChange - $binanceChange)
    Assert-True ($driftPct -le $MaxMajorChangeDriftPct) "$Symbol monitor 24h change drift $([Math]::Round($driftPct, 3)) percentage points exceeds $MaxMajorChangeDriftPct"
    Write-Host "$Symbol 24h change drift ok: monitor=$([Math]::Round($monitorChange, 3)) binance=$([Math]::Round($binanceChange, 3))"
}

function Assert-RadarPricesAgainstBinance {
    param(
        [object]$Radar,
        [int]$SampleSize = 8
    )
    if ($SkipExternalBinanceCheck) {
        return
    }
    $items = @($Radar.top50 | Select-Object -First $SampleSize)
    $checked = 0
    $worstSymbol = ""
    $worstDrift = 0.0
    foreach ($item in $items) {
        $symbol = [string]$item.symbol
        if ([string]::IsNullOrWhiteSpace($symbol)) {
            continue
        }
        $monitorPrice = [double]$item.price
        if ($monitorPrice -le 0) {
            continue
        }
        $binance = Read-Json "https://fapi.binance.com/fapi/v1/ticker/price?symbol=$symbol"
        $binancePrice = [double]$binance.price
        if ($binancePrice -le 0) {
            continue
        }
        $driftPct = [Math]::Abs(($monitorPrice - $binancePrice) / $binancePrice * 100.0)
        if ($driftPct -gt $worstDrift) {
            $worstDrift = $driftPct
            $worstSymbol = $symbol
        }
        Assert-True ($driftPct -le $RadarMaxPriceDriftPct) "$symbol radar price drift $([Math]::Round($driftPct, 3))% exceeds $RadarMaxPriceDriftPct%"
        $checked += 1
    }
    Assert-True ($checked -ge [Math]::Min(5, @($Radar.top50).Count)) "checked only $checked radar prices against Binance"
    Write-Host "radar price drift ok: checked=$checked worst=$worstSymbol $([Math]::Round($worstDrift, 3))%"
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
    $btc = Get-MonitorMajor -State $state -Symbol "BTCUSDT"
    Assert-True ($null -ne $btc) "BTCUSDT is missing from monitor major market cards"
    Assert-True ([double]$btc.price -gt 0) "BTCUSDT monitor price is not positive"
}

if (-not $SkipExternalBinanceCheck) {
    Invoke-Step "mainnet BTC price drift" {
        $state = Read-Json "$MonitorBaseUrl/api/state"
        $btc = Get-MonitorMajor -State $state -Symbol "BTCUSDT"
        $binance = Read-Json "https://fapi.binance.com/fapi/v1/ticker/price?symbol=BTCUSDT"
        $monitorPrice = [double]$btc.price
        $binancePrice = [double]$binance.price
        Assert-True ($binancePrice -gt 0) "Binance mainnet BTC price is not positive"
        $driftPct = [Math]::Abs(($monitorPrice - $binancePrice) / $binancePrice * 100.0)
        Assert-True ($driftPct -le $MaxPriceDriftPct) "BTCUSDT monitor price drift $([Math]::Round($driftPct, 3))% exceeds $MaxPriceDriftPct%"
    }

    Invoke-Step "mainnet BTC 24h change drift" {
        Assert-MonitorMajorChangeAgainstBinance -Symbol "BTCUSDT"
    }
}

Invoke-Step "radar data present" {
    $before = Read-Json "$MonitorBaseUrl/api/radar"
    $beforeScanId = [string]$before.last_scan_id
    $scan = Invoke-RestMethod -Method Post -Uri "$MonitorBaseUrl/api/radar/scan-now" -TimeoutSec 15
    Assert-True ($scan.ok -eq $true) "scan-now did not return ok=true"
    $radar = $null
    for ($i = 0; $i -lt 60; $i++) {
        Start-Sleep -Seconds 1
        $radar = Read-Json "$MonitorBaseUrl/api/radar"
        $count = @($radar.top50).Count
        $scanId = [string]$radar.last_scan_id
        $scanFinished = $count -gt 0 -and -not $radar.scan_status.in_progress
        $newScanReady = -not $scan.started -or [string]::IsNullOrWhiteSpace($beforeScanId) -or $scanId -ne $beforeScanId
        if ($scanFinished -and $newScanReady) {
            break
        }
    }
    $count = @($radar.top50).Count
    Assert-True ($count -gt 0) "radar top50 is empty"
    Assert-True ($radar.scan_status.market_refresh.source -ne "") "radar market refresh source is empty"
    Assert-True ($radar.scan_status.market_refresh.degraded -eq $false) "radar market_refresh.degraded is true: $($radar.scan_status.market_refresh.error)"
    Assert-True ([string]::IsNullOrWhiteSpace($radar.scan_status.market_refresh.error)) "radar market refresh error is not empty: $($radar.scan_status.market_refresh.error)"
    Assert-RadarPricesAgainstBinance -Radar $radar
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
        $openTestPositionsAfter = @($acceptance.position_delta.open_test_positions_after)
        Assert-True ($openTestPositionsAfter.Count -eq 0) "controlled paper acceptance left its own positions open: $($openTestPositionsAfter -join ',')"
    }
}

Write-Host "docker stack verification complete"
