# Registers (or updates) the Windows Scheduled Task that runs the basis
# Operator pipeline each weekday evening after market close.
#
#   .\scripts\register-operator-task.ps1              # 6:30 PM local, Mon-Fri
#   .\scripts\register-operator-task.ps1 -Time 19:00  # custom time
#   .\scripts\register-operator-task.ps1 -Unregister  # remove the task
#
# The task runs `pixi run operator` in the repo root. Digest delivery needs
# NTFY_TOPIC in .env (see README → Environment Variables).

param(
    [string]$Time = "18:30",
    [switch]$Unregister
)

$ErrorActionPreference = "Stop"
$TaskName = "basis-operator"
$RepoRoot = Split-Path -Parent $PSScriptRoot

if ($Unregister) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "Removed scheduled task '$TaskName'."
    exit 0
}

$pixi = (Get-Command pixi -ErrorAction SilentlyContinue)?.Source
if (-not $pixi) {
    $candidate = Join-Path $env:USERPROFILE ".pixi\bin\pixi.exe"
    if (Test-Path $candidate) { $pixi = $candidate }
    else { throw "pixi not found on PATH or at $candidate" }
}

$action = New-ScheduledTaskAction -Execute $pixi -Argument "run operator" -WorkingDirectory $RepoRoot
$trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday, Tuesday, Wednesday, Thursday, Friday -At $Time
$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 15) `
    -MultipleInstances IgnoreNew

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Description "basis: nightly evening pipeline (telemetry fetch, lifecycle scan, opportunity scan, ntfy digest)" `
    -Force | Out-Null

Write-Host "Scheduled task '$TaskName' registered: weekdays at $Time in $RepoRoot."
Write-Host "StartWhenAvailable is on - if the machine is asleep/off at $Time, the run fires at next wake."
