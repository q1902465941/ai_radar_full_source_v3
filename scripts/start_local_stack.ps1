param(
    [switch]$SkipBuild,
    [switch]$ForceStop,
    [int]$BackendPort = 8011,
    [int]$FrontendPort = 4183
)

$ErrorActionPreference = "Stop"

$RepoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
$Python = Join-Path $RepoRoot ".venv\Scripts\python.exe"
$FrontendDir = Join-Path $RepoRoot "frontend"
$LogDir = Join-Path $RepoRoot "logs"
$StateFile = Join-Path $LogDir "local_stack.json"
$StopScript = Join-Path $PSScriptRoot "stop_local_stack.ps1"

function Assert-PortFree {
    param([int]$Port)
    $listener = Get-NetTCPConnection -LocalPort $Port -ErrorAction SilentlyContinue
    if ($listener) {
        throw "Port $Port is already in use. Re-run with -ForceStop or stop the existing process."
    }
}

function Wait-HttpOk {
    param(
        [string]$Url,
        [scriptblock]$Predicate,
        [int]$Attempts = 40,
        [int]$DelayMilliseconds = 750
    )
    for ($i = 0; $i -lt $Attempts; $i++) {
        Start-Sleep -Milliseconds $DelayMilliseconds
        try {
            $response = Invoke-WebRequest -Uri $Url -TimeoutSec 3
            if (& $Predicate $response) {
                return
            }
        } catch {}
    }
    throw "Timed out waiting for $Url"
}

if (-not (Test-Path $Python)) {
    throw "Python virtualenv not found at $Python. Create it with: python -m venv .venv"
}

New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

if ($ForceStop) {
    & $StopScript -BackendPort $BackendPort -FrontendPort $FrontendPort
}

Assert-PortFree -Port $BackendPort
Assert-PortFree -Port $FrontendPort

if (-not $SkipBuild) {
    Write-Host "==> npm run build"
    Push-Location $FrontendDir
    try {
        & npm run build
    } finally {
        Pop-Location
    }
}

$backendOut = Join-Path $LogDir "local_backend.out.log"
$backendErr = Join-Path $LogDir "local_backend.err.log"
$frontendOut = Join-Path $LogDir "local_frontend.out.log"
$frontendErr = Join-Path $LogDir "local_frontend.err.log"
Remove-Item -ErrorAction SilentlyContinue $backendOut, $backendErr, $frontendOut, $frontendErr

$backend = $null
$frontend = $null

try {
    Write-Host "==> starting backend on $BackendPort"
    $originalAppPort = $env:APP_PORT
    $env:APP_PORT = [string]($BackendPort - 1)
    try {
        $backend = Start-Process -FilePath $Python -ArgumentList "run_v2.py" -WorkingDirectory $RepoRoot -PassThru -WindowStyle Hidden -RedirectStandardOutput $backendOut -RedirectStandardError $backendErr
    } finally {
        $env:APP_PORT = $originalAppPort
    }
    Wait-HttpOk -Url "http://127.0.0.1:$BackendPort/api/v2/health" -Predicate {
        param($response)
        $body = $response.Content | ConvertFrom-Json
        return $body.ok -eq $true -and $body.service -eq "ai-radar-api"
    }

    Write-Host "==> starting frontend preview on $FrontendPort"
    $frontend = Start-Process -FilePath "npm.cmd" -ArgumentList @("exec", "vite", "--", "preview", "--host", "127.0.0.1", "--port", "$FrontendPort") -WorkingDirectory $FrontendDir -PassThru -WindowStyle Hidden -RedirectStandardOutput $frontendOut -RedirectStandardError $frontendErr
    Wait-HttpOk -Url "http://127.0.0.1:$FrontendPort/" -Predicate {
        param($response)
        return $response.Content -match '<div id="root"></div>'
    } -Attempts 30 -DelayMilliseconds 500

    $backendPid = 0
    $frontendPid = 0
    if ($backend) {
        $backendPid = $backend.Id
    }
    if ($frontend) {
        $frontendPid = $frontend.Id
    }

    @{
        backend_pid = $backendPid
        frontend_pid = $frontendPid
        backend_port = $BackendPort
        frontend_port = $FrontendPort
        backend_url = "http://127.0.0.1:$BackendPort/api/v2/health"
        frontend_url = "http://127.0.0.1:$FrontendPort/"
        created_at = (Get-Date).ToString("o")
        backend_stdout = $backendOut
        backend_stderr = $backendErr
        frontend_stdout = $frontendOut
        frontend_stderr = $frontendErr
    } | ConvertTo-Json | Set-Content -Encoding UTF8 $StateFile

    Write-Host "local stack started"
    Write-Host "Frontend: http://127.0.0.1:$FrontendPort/"
    Write-Host "Backend health: http://127.0.0.1:$BackendPort/api/v2/health"
    Write-Host "Stop with: powershell -ExecutionPolicy Bypass -File .\scripts\stop_local_stack.ps1"
} catch {
    if ($backend -and -not $backend.HasExited) {
        Stop-Process -Id $backend.Id -Force -ErrorAction SilentlyContinue
    }
    if ($frontend -and -not $frontend.HasExited) {
        Stop-Process -Id $frontend.Id -Force -ErrorAction SilentlyContinue
    }
    & $StopScript -BackendPort $BackendPort -FrontendPort $FrontendPort
    throw
}
