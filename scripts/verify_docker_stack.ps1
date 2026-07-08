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
    $response = Read-HttpTextWithRetry -Url $Url -Retries 5 -DelaySeconds 2 -TimeoutSec 40
    $content = [string]$response.Content
    Assert-True (-not [string]::IsNullOrWhiteSpace($content)) "empty JSON response from $Url"
    $parsed = $content | ConvertFrom-Json
    return $parsed
}

function Read-HttpTextWithRetry {
    param(
        [string]$Url,
        [int]$Retries = 12,
        [int]$DelaySeconds = 3,
        [int]$TimeoutSec = 20
    )
    $lastError = ""
    for ($attempt = 1; $attempt -le $Retries; $attempt++) {
        try {
            return Invoke-WebRequest -Uri $Url -TimeoutSec $TimeoutSec
        }
        catch {
            $lastError = $_.Exception.Message
            if ($attempt -ge $Retries) {
                throw "HTTP request to $Url failed after $Retries attempts: $lastError"
            }
            Start-Sleep -Seconds $DelaySeconds
        }
    }
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

function Encode-UrlValue {
    param([string]$Value)
    return [System.Uri]::EscapeDataString($Value)
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
    $encodedSymbol = Encode-UrlValue $Symbol
    $binance = Read-Json "https://fapi.binance.com/fapi/v1/ticker/24hr?symbol=$encodedSymbol"
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
    $tickerRows = @(Read-Json "https://fapi.binance.com/fapi/v1/ticker/24hr")
    $tickerBySymbol = @{}
    foreach ($ticker in $tickerRows) {
        $tickerSymbol = [string]$ticker.symbol
        if (-not [string]::IsNullOrWhiteSpace($tickerSymbol)) {
            $tickerBySymbol[$tickerSymbol] = $ticker
        }
    }
    $items = @($Radar.top50 | Select-Object -First $SampleSize)
    $checked = 0
    $worstSymbol = ""
    $worstDrift = 0.0
    $missingSymbols = @()
    foreach ($item in $items) {
        $symbol = [string]$item.symbol
        if ([string]::IsNullOrWhiteSpace($symbol)) {
            continue
        }
        $monitorPrice = [double]$item.price
        if ($monitorPrice -le 0) {
            continue
        }
        $binance = $tickerBySymbol[$symbol]
        if ($null -eq $binance) {
            $missingSymbols += $symbol
            continue
        }
        $binancePrice = [double]$binance.lastPrice
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
    if ($missingSymbols.Count -gt 0) {
        Write-Host "radar symbols skipped from price drift check: $($missingSymbols -join ',')"
    }
    Write-Host "radar price drift ok: checked=$checked worst=$worstSymbol $([Math]::Round($worstDrift, 3))%"
}

function Get-DefaultRadarExcludedMajorSymbols {
    return @(
        "BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT", "DOGEUSDT",
        "ADAUSDT", "TRXUSDT", "AVAXUSDT", "LINKUSDT", "LTCUSDT", "BCHUSDT",
        "DOTUSDT", "SUIUSDT", "HYPEUSDT", "WLDUSDT", "ZECUSDT", "XAUUSDT",
        "XAGUSDT"
    )
}

function Test-AsciiSymbol {
    param([string]$Symbol)
    if ([string]::IsNullOrWhiteSpace($Symbol)) {
        return $false
    }
    foreach ($ch in $Symbol.ToCharArray()) {
        if ([int][char]$ch -gt 127) {
            return $false
        }
    }
    return $true
}

function Test-BinanceCryptoPerpetualSymbol {
    param([object]$Meta)
    if ($null -eq $Meta) {
        return $false
    }
    if (-not (Test-AsciiSymbol -Symbol ([string]$Meta.symbol))) {
        return $false
    }
    if ([string]$Meta.status -ne "TRADING") {
        return $false
    }
    if ([string]$Meta.contractType -ne "PERPETUAL") {
        return $false
    }
    if ([string]$Meta.quoteAsset -ne "USDT") {
        return $false
    }
    if ([string]$Meta.marginAsset -ne "USDT") {
        return $false
    }
    $underlyingType = [string]$Meta.underlyingType
    if (-not [string]::IsNullOrWhiteSpace($underlyingType) -and $underlyingType -ne "COIN") {
        return $false
    }
    foreach ($subtype in @($Meta.underlyingSubType)) {
        if ([string]$subtype -eq "TRADFI") {
            return $false
        }
    }
    return $true
}

function Get-BinanceRankedTickerCandidates {
    $tickers = @(Read-Json "https://fapi.binance.com/fapi/v1/ticker/24hr")
    $exchangeInfo = Read-Json "https://fapi.binance.com/fapi/v1/exchangeInfo"
    $metaBySymbol = @{}
    foreach ($row in @($exchangeInfo.symbols)) {
        $symbol = [string]$row.symbol
        if (-not [string]::IsNullOrWhiteSpace($symbol)) {
            $metaBySymbol[$symbol] = $row
        }
    }
    $excluded = @{}
    foreach ($symbol in Get-DefaultRadarExcludedMajorSymbols) {
        $excluded[$symbol] = $true
    }
    $rows = @()
    foreach ($ticker in $tickers) {
        $symbol = [string]$ticker.symbol
        if ([string]::IsNullOrWhiteSpace($symbol) -or -not $symbol.EndsWith("USDT")) {
            continue
        }
        if ($excluded.ContainsKey($symbol)) {
            continue
        }
        if (-not (Test-BinanceCryptoPerpetualSymbol -Meta $metaBySymbol[$symbol])) {
            continue
        }
        $quoteVolume = [double]$ticker.quoteVolume
        $changeAbs = [Math]::Abs([double]$ticker.priceChangePercent)
        if ($quoteVolume -lt 500000.0 -or $changeAbs -lt 2.5) {
            continue
        }
        $score = $changeAbs * [Math]::Max(1.0, [Math]::Log10([Math]::Max($quoteVolume, 10.0)))
        $rows += [pscustomobject]@{
            symbol = $symbol
            score = $score
            quoteVolume = $quoteVolume
            changeAbs = $changeAbs
        }
    }
    return @($rows | Sort-Object -Property @{ Expression = { -[double]$_.score } }, @{ Expression = { -[double]$_.quoteVolume } }, @{ Expression = { [string]$_.symbol } })
}

function Assert-MonitorSymbolsUseSupportedUsdMAsciiContracts {
    if ($SkipExternalBinanceCheck) {
        return
    }
    $radar = Read-Json "$MonitorBaseUrl/api/radar"
    $readiness = Read-Json "$MonitorBaseUrl/api/system/readiness"
    $exchangeInfo = Read-Json "https://fapi.binance.com/fapi/v1/exchangeInfo"
    $metaBySymbol = @{}
    foreach ($row in @($exchangeInfo.symbols)) {
        $symbol = [string]$row.symbol
        if (-not [string]::IsNullOrWhiteSpace($symbol)) {
            $metaBySymbol[$symbol] = $row
        }
    }
    $symbols = @()
    foreach ($item in @($radar.top50)) {
        $symbols += [string]$item.symbol
    }
    foreach ($symbol in @($readiness.market_data.active_coins.active_symbols | ForEach-Object { [string]$_ })) {
        $symbols += $symbol
    }
    $symbols = @($symbols | Where-Object { -not [string]::IsNullOrWhiteSpace($_) } | Select-Object -Unique)
    Assert-True ($symbols.Count -gt 0) "no monitor market symbols available to validate"
    foreach ($symbol in $symbols) {
        Assert-True (Test-AsciiSymbol -Symbol $symbol) "monitor symbol '$symbol' is not ASCII"
        Assert-True ($symbol.EndsWith("USDT")) "monitor symbol '$symbol' is not a USDT contract"
        Assert-True (Test-BinanceCryptoPerpetualSymbol -Meta $metaBySymbol[$symbol]) "monitor symbol '$symbol' is not a supported Binance USD-M perpetual contract"
    }
    Write-Host "market symbols use supported USD-M ASCII contracts: checked=$($symbols.Count)"
}

function Assert-ActiveTickerCandidateCoverage {
    if ($SkipExternalBinanceCheck) {
        return
    }
    $readiness = Read-Json "$MonitorBaseUrl/api/system/readiness"
    $activeSymbols = @($readiness.market_data.active_coins.active_symbols | ForEach-Object { [string]$_ })
    Assert-True ($activeSymbols.Count -gt 0) "active ticker symbol pool is empty"
    $ranked = @(Get-BinanceRankedTickerCandidates | Select-Object -First 10)
    if ($ranked.Count -lt 5) {
        Write-Host "active ticker priority coverage skipped: only $($ranked.Count) external candidates"
        return
    }
    $activeSet = @{}
    foreach ($symbol in $activeSymbols) {
        $activeSet[$symbol] = $true
    }
    $matched = @()
    foreach ($row in $ranked) {
        $symbol = [string]$row.symbol
        if ($activeSet.ContainsKey($symbol)) {
            $matched += $symbol
        }
    }
    $required = [Math]::Min(7, $ranked.Count)
    Assert-True ($matched.Count -ge $required) "active ticker pool covers only $($matched.Count)/$($ranked.Count) top Binance candidates; matched=$($matched -join ',')"
    Write-Host "active ticker priority coverage ok: matched=$($matched.Count)/$($ranked.Count)"
}

function Assert-PaperGraduationProgress {
    $readiness = Read-Json "$MonitorBaseUrl/api/system/readiness"
    $progress = $readiness.paper_learning.graduation_progress
    Assert-True ($null -ne $progress) "paper learning graduation_progress is missing from readiness"
    Assert-True ($null -ne $progress.PSObject.Properties["real_closed_samples_with_radar"]) "graduation_progress.real_closed_samples_with_radar is missing"
    Assert-True ($null -ne $progress.PSObject.Properties["codex_real_closed_samples_with_radar"]) "graduation_progress.codex_real_closed_samples_with_radar is missing"
    Assert-True ($null -ne $progress.PSObject.Properties["codex_missing_real_closed_samples"]) "graduation_progress.codex_missing_real_closed_samples is missing"
    Assert-True ($null -ne $progress.PSObject.Properties["real_closed_samples_by_provider"]) "graduation_progress.real_closed_samples_by_provider is missing"
    Assert-True ($null -ne $progress.PSObject.Properties["minimum_real_closed_samples"]) "graduation_progress.minimum_real_closed_samples is missing"
    Assert-True ($null -ne $progress.PSObject.Properties["missing_real_closed_samples"]) "graduation_progress.missing_real_closed_samples is missing"
    $realClosed = [int]$progress.real_closed_samples_with_radar
    $codexClosed = [int]$progress.codex_real_closed_samples_with_radar
    $codexMissing = [int]$progress.codex_missing_real_closed_samples
    $minimumClosed = [int]$progress.minimum_real_closed_samples
    $missingClosed = [int]$progress.missing_real_closed_samples
    Assert-True ($minimumClosed -gt 0) "minimum_real_closed_samples is not positive"
    Assert-True ($missingClosed -ge 0) "missing_real_closed_samples is negative"
    Assert-True ($codexClosed -ge 0) "codex_real_closed_samples_with_radar is negative"
    Assert-True ($codexMissing -ge 0) "codex_missing_real_closed_samples is negative"
    Assert-True (($realClosed + $missingClosed) -ge $minimumClosed) "graduation sample math is inconsistent"
    Write-Host "paper graduation progress: real_closed=$realClosed/$minimumClosed missing=$missingClosed codex_closed=$codexClosed codex_missing=$codexMissing trust=$($progress.trust_level)"
}

function Assert-CodexEntryEnforcementVisible {
    $readiness = Read-Json "$MonitorBaseUrl/api/system/readiness"
    $codex = $readiness.codex
    Assert-True ($null -ne $codex) "codex readiness section is missing"
    Assert-True ($null -ne $codex.PSObject.Properties["entry_enforced"]) "codex.entry_enforced is missing"
    Assert-True ($null -ne $codex.PSObject.Properties["entry_enforcement_reason"]) "codex.entry_enforcement_reason is missing"
    if ($codex.entry_enforced -eq $true) {
        Assert-True ($codex.provider -eq "codex_cli") "Codex entry enforcement claims true but provider is $($codex.provider)"
        Assert-True ($codex.required_for_entry -eq $true) "Codex entry enforcement claims true but required_for_entry is false"
        Write-Host "Codex entry enforcement: enforced provider=$($codex.provider) ready=$($codex.ready_for_generation)"
        return
    }
    $blockers = @($readiness.blockers)
    $blocker = $blockers | Where-Object { $_.code -eq "codex_strategy_not_enforced_for_live_intent" } | Select-Object -First 1
    Assert-True ($null -ne $blocker) "Codex entry enforcement is false but codex_strategy_not_enforced_for_live_intent blocker is missing"
    Write-Host "Codex entry enforcement: not enforced reason=$($codex.entry_enforcement_reason)"
}

function Assert-DockerDatabasePath {
    $readiness = Read-Json "$MonitorBaseUrl/api/system/readiness"
    $db = $readiness.database
    Assert-True ($null -ne $db) "database readiness section is missing"
    $path = [string]$db.path
    Assert-True (-not [string]::IsNullOrWhiteSpace($path)) "database path is empty"
    Assert-True ($path -eq "data/ai_radar.db" -or $path -eq "/app/data/ai_radar.db") "database path '$path' is not the mounted Docker data path"
    Assert-True ($db.exists -eq $true) "database file does not exist at '$path'"
    Write-Host "database path uses mounted Docker volume: $path"
}

Invoke-Step "compose config" {
    docker compose config --quiet
}

Invoke-Step "legacy monitor page" {
    $response = Read-HttpTextWithRetry -Url "$MonitorBaseUrl/radar" -Retries 12 -DelaySeconds 3 -TimeoutSec 20
    Assert-True ($response.StatusCode -eq 200) "monitor /radar did not return HTTP 200"
    Assert-True ($response.Content.Contains("AI RADAR SYSTEM")) "monitor page is missing legacy AI RADAR SYSTEM branding"
    Assert-True (-not $response.Content.Contains("AI Radar Control Center")) "monitor page is still serving the React migration shell"
}

Invoke-Step "v2 api health" {
    $health = Read-Json "$V2BaseUrl/api/v2/health"
    Assert-True ($health.ok -eq $true) "v2 health did not return ok=true"
    Assert-True ($health.service -eq "ai-radar-api") "v2 health returned an unexpected service"
}

Invoke-Step "database path uses mounted Docker volume" {
    Assert-DockerDatabasePath
}

Invoke-Step "monitor state uses mainnet market data" {
    $state = $null
    $btc = $null
    for ($i = 0; $i -lt 30; $i++) {
        $state = Read-Json "$MonitorBaseUrl/api/state"
        $btc = Get-MonitorMajor -State $state -Symbol "BTCUSDT"
        if ($state.market_data_source -eq "mainnet" -and $null -ne $btc -and [double]$btc.price -gt 0) {
            break
        }
        Start-Sleep -Seconds 1
    }
    Assert-True ($state.market_data_source -eq "mainnet") "market_data_source is '$($state.market_data_source)', expected mainnet"
    Assert-True ($null -ne $btc) "BTCUSDT is missing from monitor major market cards"
    Assert-True ([double]$btc.price -gt 0) "BTCUSDT monitor price is not positive"
}

if (-not $SkipExternalBinanceCheck) {
    Invoke-Step "mainnet BTC price drift" {
        $state = Read-Json "$MonitorBaseUrl/api/state"
        $btc = Get-MonitorMajor -State $state -Symbol "BTCUSDT"
        $encodedSymbol = Encode-UrlValue "BTCUSDT"
        $binance = Read-Json "https://fapi.binance.com/fapi/v1/ticker/price?symbol=$encodedSymbol"
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

Invoke-Step "market symbols use supported USD-M ASCII contracts" {
    Assert-MonitorSymbolsUseSupportedUsdMAsciiContracts
}

Invoke-Step "active ticker candidate coverage" {
    Assert-ActiveTickerCandidateCoverage
}

Invoke-Step "paper graduation progress visible" {
    Assert-PaperGraduationProgress
}

Invoke-Step "Codex entry enforcement visible" {
    Assert-CodexEntryEnforcementVisible
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
