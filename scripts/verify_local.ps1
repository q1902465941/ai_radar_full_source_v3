param(
    [switch]$SkipBackendTests,
    [switch]$SkipFrontendTests,
    [switch]$SkipSmoke
)

$ErrorActionPreference = "Stop"

$RepoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
$Python = Join-Path $RepoRoot ".venv\Scripts\python.exe"
$FrontendDir = Join-Path $RepoRoot "frontend"

function Invoke-Step {
    param(
        [string]$Name,
        [scriptblock]$Command
    )
    Write-Host "==> $Name"
    & $Command
}

function Stop-Listeners {
    param([int[]]$Ports)
    foreach ($port in $Ports) {
        Get-NetTCPConnection -LocalPort $port -ErrorAction SilentlyContinue |
            ForEach-Object { Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue }
    }
}

if (-not (Test-Path $Python)) {
    throw "Python virtualenv not found at $Python. Create it with: python -m venv .venv"
}

if (-not $SkipBackendTests) {
    Invoke-Step "backend tests" {
        & $Python -m pytest -q
    }
}

if (-not $SkipFrontendTests) {
    Invoke-Step "frontend tests" {
        Push-Location $FrontendDir
        try {
            & npm test -- --run
        } finally {
            Pop-Location
        }
    }

    Invoke-Step "frontend production build" {
        Push-Location $FrontendDir
        try {
            & npm run build
        } finally {
            Pop-Location
        }
    }
}

if (-not $SkipSmoke) {
    Invoke-Step "backend /api/v2/health smoke" {
        $stdout = Join-Path $env:TEMP "ai-radar-v2-smoke.out.log"
        $stderr = Join-Path $env:TEMP "ai-radar-v2-smoke.err.log"
        Remove-Item -ErrorAction SilentlyContinue $stdout, $stderr
        $server = Start-Process -FilePath $Python -ArgumentList "run_v2.py" -WorkingDirectory $RepoRoot -PassThru -WindowStyle Hidden -RedirectStandardOutput $stdout -RedirectStandardError $stderr
        try {
            $healthy = $false
            for ($i = 0; $i -lt 40; $i++) {
                Start-Sleep -Milliseconds 750
                try {
                    $body = Invoke-RestMethod -Uri "http://127.0.0.1:8001/api/v2/health" -TimeoutSec 3
                    if ($body.ok -eq $true -and $body.service -eq "ai-radar-api") {
                        $healthy = $true
                        break
                    }
                } catch {}
                if ($server.HasExited) {
                    break
                }
            }
            if (-not $healthy) {
                if (Test-Path $stderr) {
                    Get-Content $stderr -Tail 80
                }
                throw "backend health smoke failed"
            }
        } finally {
            if (-not $server.HasExited) {
                Stop-Process -Id $server.Id -Force -ErrorAction SilentlyContinue
                $server.WaitForExit()
            }
        }
    }

    Invoke-Step "frontend vite preview smoke" {
        Stop-Listeners -Ports @(4173)
        $stdout = Join-Path $env:TEMP "ai-radar-frontend-preview.out.log"
        $stderr = Join-Path $env:TEMP "ai-radar-frontend-preview.err.log"
        Remove-Item -ErrorAction SilentlyContinue $stdout, $stderr
        $preview = Start-Process -FilePath "npm.cmd" -ArgumentList @("exec", "vite", "--", "preview", "--host", "127.0.0.1", "--port", "4173") -WorkingDirectory $FrontendDir -PassThru -WindowStyle Hidden -RedirectStandardOutput $stdout -RedirectStandardError $stderr
        try {
            $served = $false
            for ($i = 0; $i -lt 30; $i++) {
                Start-Sleep -Milliseconds 500
                try {
                    $html = Invoke-WebRequest -Uri "http://127.0.0.1:4173/" -TimeoutSec 3 | Select-Object -ExpandProperty Content
                    if ($html -match '<div id="root"></div>') {
                        $served = $true
                        break
                    }
                } catch {}
                if ($preview.HasExited) {
                    break
                }
            }
            if (-not $served) {
                if (Test-Path $stderr) {
                    Get-Content $stderr -Tail 80
                }
                throw "frontend preview smoke failed"
            }
        } finally {
            if (-not $preview.HasExited) {
                Stop-Process -Id $preview.Id -Force -ErrorAction SilentlyContinue
            }
            Stop-Listeners -Ports @(4173)
        }
    }
}

Write-Host "local verification complete"
