param(
    [int]$BackendPort = 8011,
    [int]$FrontendPort = 4183
)

$ErrorActionPreference = "Continue"

$RepoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
$LogDir = Join-Path $RepoRoot "logs"
$StateFile = Join-Path $LogDir "local_stack.json"

function Stop-Pid {
    param([int]$ProcessId)
    if ($ProcessId -le 0) {
        return
    }
    Stop-Process -Id $ProcessId -Force -ErrorAction SilentlyContinue
}

function Stop-PortListeners {
    param([int[]]$Ports)
    foreach ($port in $Ports) {
        Get-NetTCPConnection -LocalPort $port -ErrorAction SilentlyContinue |
            ForEach-Object { Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue }
    }
}

if (Test-Path $StateFile) {
    try {
        $state = Get-Content -Raw $StateFile | ConvertFrom-Json
        $backendPid = 0
        $frontendPid = 0
        if ($state.backend_pid) {
            $backendPid = [int]$state.backend_pid
        }
        if ($state.frontend_pid) {
            $frontendPid = [int]$state.frontend_pid
        }
        Stop-Pid -ProcessId $backendPid
        Stop-Pid -ProcessId $frontendPid
    } catch {
        Write-Host "Could not parse $StateFile; falling back to port cleanup."
    }
}

Stop-PortListeners -Ports @($BackendPort, $FrontendPort)
Remove-Item -ErrorAction SilentlyContinue $StateFile

Write-Host "local stack stopped"
