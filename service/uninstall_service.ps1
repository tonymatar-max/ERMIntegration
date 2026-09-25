# Stops and removes the Windows Service installed by install_service.ps1.
# Run as Administrator. Does NOT touch data\erm.db, data\secret.key, or
# any application data — only the service registration itself.
#
# Usage:
#   .\service\uninstall_service.ps1
#   .\service\uninstall_service.ps1 -ServiceName ERM2

param(
    [string]$ServiceName = "ERMProjectLedger",
    [string]$NssmPath = "nssm.exe"
)

$ErrorActionPreference = "Stop"

$nssm = Get-Command $NssmPath -ErrorAction SilentlyContinue
if (-not $nssm) {
    Write-Error "nssm.exe not found (looked for '$NssmPath' on PATH). Pass -NssmPath <full path to nssm.exe> if it's not on PATH."
    exit 1
}

& $NssmPath stop $ServiceName
& $NssmPath remove $ServiceName confirm

Write-Host "Removed service '$ServiceName'." -ForegroundColor Green
