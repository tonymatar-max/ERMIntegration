# Installs the ERM Project Ledger as a real Windows Service using NSSM
# (Non-Sucking Service Manager) — the service starts at boot, restarts
# automatically if it crashes, and runs without anyone logged in.
#
# One-time prerequisite: download NSSM from https://nssm.cc/download and
# place nssm.exe somewhere on PATH, or pass its path via -NssmPath.
#
# Usage (run as Administrator):
#   .\service\install_service.ps1
#   .\service\install_service.ps1 -Port 8080 -ServiceName ERM2
#   .\service\install_service.ps1 -BindHost 127.0.0.1   # behind a reverse proxy (Caddy/IIS/nginx) — see service/Caddyfile.example
#
# After installing, start it with:
#   nssm start ERMProjectLedger
# or via services.msc / Start-Service ERMProjectLedger.

param(
    [string]$ServiceName = "ERMProjectLedger",
    [int]$Port = 9020,
    [string]$BindHost = "0.0.0.0",
    [string]$NssmPath = "nssm.exe"
)

$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $PSScriptRoot
$VenvPython = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$DataDir = Join-Path $ProjectRoot "data"

if (-not (Test-Path $VenvPython)) {
    Write-Error "Virtual environment not found at $VenvPython. Run the First-time setup steps in README.md first (python -m venv .venv; pip install -r requirements.txt)."
    exit 1
}

$nssm = Get-Command $NssmPath -ErrorAction SilentlyContinue
if (-not $nssm) {
    Write-Error "nssm.exe not found (looked for '$NssmPath' on PATH). Download it from https://nssm.cc/download, place nssm.exe on PATH or pass -NssmPath <full path to nssm.exe>, then re-run this script."
    exit 1
}

New-Item -ItemType Directory -Force -Path $DataDir | Out-Null

& $NssmPath install $ServiceName $VenvPython "-m uvicorn app.main:app --host $BindHost --port $Port"
& $NssmPath set $ServiceName AppDirectory $ProjectRoot
& $NssmPath set $ServiceName AppStdout (Join-Path $DataDir "service-stdout.log")
& $NssmPath set $ServiceName AppStderr (Join-Path $DataDir "service-stderr.log")
& $NssmPath set $ServiceName AppRotateFiles 1
& $NssmPath set $ServiceName AppRotateBytes 2097152
& $NssmPath set $ServiceName Start SERVICE_AUTO_START
& $NssmPath set $ServiceName AppExit Default Restart
& $NssmPath set $ServiceName DisplayName "ERM Project Ledger"
& $NssmPath set $ServiceName Description "Hosted multi-user Redmine Project Ledger dashboard (FastAPI)"

Write-Host ""
Write-Host "Installed service '$ServiceName' -> http://${BindHost}:$Port" -ForegroundColor Green
Write-Host "No manual secret-key setup needed — the app generates and persists its own key in data\secret.key on first run."
Write-Host ""
Write-Host "Start it now with:  nssm start $ServiceName"
Write-Host "Check status with:  nssm status $ServiceName"
Write-Host "View logs at:       $DataDir\service-stdout.log / service-stderr.log / app.log"
