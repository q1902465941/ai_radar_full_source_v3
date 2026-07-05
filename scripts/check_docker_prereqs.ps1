$ErrorActionPreference = "Continue"

$RepoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
$failed = $false

function Run-Check {
    param(
        [string]$Name,
        [scriptblock]$Command,
        [switch]$Required
    )
    Write-Host "==> $Name"
    try {
        & $Command
        if ($LASTEXITCODE -ne $null -and $LASTEXITCODE -ne 0) {
            throw "exit code $LASTEXITCODE"
        }
    } catch {
        Write-Host "FAILED: $Name - $($_.Exception.Message)"
        if ($Required) {
            $script:failed = $true
        }
    }
}

Run-Check "Docker CLI version" {
    docker --version
} -Required

Run-Check "Docker Compose version" {
    docker compose version
} -Required

Run-Check "WSL status" {
    $wslOutput = & wsl --status 2>&1
    $wslOutput
    $wslText = ($wslOutput | Out-String) -replace "`0", ""
    if ($LASTEXITCODE -ne 0 -or $wslText -match "WSL_OPTIONAL_COMPONENT_REQUIRED") {
        Write-Host "WSL optional component is required for Docker Desktop Linux containers."
        Write-Host "Run from an elevated PowerShell, then reboot:"
        Write-Host "  wsl --install --no-distribution"
        throw "WSL optional component missing or unavailable"
    }
} -Required

Run-Check "Docker contexts" {
    docker context ls
} -Required

Run-Check "Compose file syntax" {
    Push-Location $RepoRoot
    try {
        docker compose config --quiet
    } finally {
        Pop-Location
    }
} -Required

Run-Check "Docker daemon" {
    docker info
} -Required

if ($failed) {
    Write-Host "Docker prerequisites are not ready."
    exit 1
}

Write-Host "Docker prerequisites are ready."
