param(
    [switch]$NoElevate
)

$ErrorActionPreference = "Stop"

$RepoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")

function Test-IsElevated {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = [Security.Principal.WindowsPrincipal]::new($identity)
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

if (-not (Test-IsElevated)) {
    if ($NoElevate) {
        throw "This script must run from an elevated PowerShell."
    }

    Write-Host "Requesting elevated PowerShell to enable the WSL optional component..."
    $process = Start-Process -FilePath "powershell.exe" `
        -ArgumentList @(
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            "`"$PSCommandPath`"",
            "-NoElevate"
        ) `
        -Verb RunAs `
        -Wait `
        -PassThru

    if ($process.ExitCode -ne 0) {
        exit $process.ExitCode
    }

    Write-Host "Elevated WSL prerequisite step finished."
    Write-Host "Reboot Windows, then run:"
    Write-Host "  powershell -ExecutionPolicy Bypass -File .\scripts\check_docker_prereqs.ps1"
    exit 0
}

Write-Host "Checking WSL status..."
$wslOutput = & wsl --status 2>&1
$wslExit = $LASTEXITCODE
$wslText = ($wslOutput | Out-String) -replace "`0", ""
$wslOutput

if ($wslExit -eq 0 -and $wslText -notmatch "WSL_OPTIONAL_COMPONENT_REQUIRED") {
    Write-Host "WSL status is available."
    Write-Host "Run Docker prerequisite verification from the repository root:"
    Write-Host "  powershell -ExecutionPolicy Bypass -File .\scripts\check_docker_prereqs.ps1"
    exit 0
}

Write-Host "Enabling WSL optional component for Docker Desktop Linux containers..."
& wsl --install --no-distribution
if ($LASTEXITCODE -ne 0) {
    throw "wsl --install --no-distribution failed with exit code $LASTEXITCODE"
}

Write-Host "WSL optional component installation was requested."
Write-Host "Reboot Windows before running Docker Desktop or Docker Compose."
Write-Host "After reboot:"
Write-Host "  cd $RepoRoot"
Write-Host "  powershell -ExecutionPolicy Bypass -File .\scripts\check_docker_prereqs.ps1"
