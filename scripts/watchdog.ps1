# Dead-man watchdog (spec/supervision.md, #72): the executor cannot report
# its own death, so this INDEPENDENT check pushes an urgent ntfy alert when
# tonight's heartbeat is missing or stale. Deliberately zero-Python: it must
# keep working when the app's environment is exactly what broke.
#
# Run by the "basis-watchdog" Scheduled Task (register-watchdog-task.ps1)
# at 22:00 on weekdays — after the executor's evening window has closed.

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $PSScriptRoot
$HeartbeatFile = Join-Path $RepoRoot "executor_heartbeat.json"

# Read NTFY_TOPIC / NTFY_SERVER straight from .env — no app code involved.
$envFile = Join-Path $RepoRoot ".env"
$topic = $null
$server = "https://ntfy.sh"
if (Test-Path $envFile) {
    foreach ($line in Get-Content $envFile) {
        if ($line -match '^\s*NTFY_TOPIC\s*=\s*(.+?)\s*$') { $topic = $Matches[1].Trim('"') }
        if ($line -match '^\s*NTFY_SERVER\s*=\s*(.+?)\s*$') { $server = $Matches[1].Trim('"') }
    }
}
if (-not $topic) {
    Write-Host "NTFY_TOPIC not set in .env — watchdog cannot alert. Exiting."
    exit 1
}

$stale = $true
if (Test-Path $HeartbeatFile) {
    $age = (Get-Date) - (Get-Item $HeartbeatFile).LastWriteTime
    if ($age.TotalHours -lt 6) { $stale = $false }
}

if ($stale) {
    $body = "Executor did not report by $(Get-Date -Format 'HH:mm'). Heartbeat missing or older than 6h. Positions may be aging unmanaged - investigate tonight."
    Invoke-RestMethod -Method Post -Uri "$server/$topic" -Body $body -Headers @{
        Title    = "basis watchdog: executor silent"
        Priority = "urgent"
        Tags     = "rotating_light"
    } | Out-Null
    Write-Host "Stale heartbeat - urgent alert pushed."
    exit 2
}

Write-Host "Heartbeat fresh - executor reported tonight."
