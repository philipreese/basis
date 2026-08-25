# Registers (or updates) the Windows Scheduled Task that runs the report-only
# afternoon preflight rehearsal (backend/preflight.py, #827).
#
#   .\scripts\register-preflight-task.ps1               # 14:00 weekdays
#   .\scripts\register-preflight-task.ps1 -Time 15:00   # custom time
#   .\scripts\register-preflight-task.ps1 -Unregister   # remove the task
#
# Walks the nightly run's broker machinery (Gateway launch, session open,
# reconciliation comparison, preview probe, control/heartbeat state) with no
# orders and no book writes, then pushes one ntfy report — so a failure that
# would otherwise be discovered at 18:45 is known while the operator can act.

param(
    [string]$Time = "14:00",
    [switch]$Unregister
)

$ErrorActionPreference = "Stop"
$TaskName = "basis-preflight"
$RepoRoot = Split-Path -Parent $PSScriptRoot

if ($Unregister) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "Removed scheduled task '$TaskName'."
    exit 0
}

$pixiExe = (Get-Command pixi).Source

$action = New-ScheduledTaskAction -Execute $pixiExe -Argument "run preflight" -WorkingDirectory $RepoRoot
$trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday, Tuesday, Wednesday, Thursday, Friday -At $Time
$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -WakeToRun `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 20) `
    -MultipleInstances IgnoreNew

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Description "basis afternoon preflight: report-only rehearsal of the broker machinery (no orders)" `
    -Force | Out-Null

Write-Host "Registered scheduled task '$TaskName' ($Time weekdays) running 'pixi run preflight'"
