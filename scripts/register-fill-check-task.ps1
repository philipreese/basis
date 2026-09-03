# Registers (or updates) the Windows Scheduled Task that runs the read-only
# morning fill check (backend/fill_check.py, #236).
#
#   .\scripts\register-fill-check-task.ps1               # 10:00 weekdays
#   .\scripts\register-fill-check-task.ps1 -Time 11:00   # custom time
#   .\scripts\register-fill-check-task.ps1 -Unregister   # remove the task
#
# Pushes an ntfy summary of which resting basis orders filled this morning.
# Notification only: this task itself writes nothing. It is no longer true
# that the evening executor is the sole database mutator — the 12:30 midday
# exit pass (#960) also places closes and writes rows.

param(
    [string]$Time = "10:00",
    [switch]$Unregister
)

$ErrorActionPreference = "Stop"
$TaskName = "basis-fill-check"
$RepoRoot = Split-Path -Parent $PSScriptRoot

if ($Unregister) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "Removed scheduled task '$TaskName'."
    exit 0
}

$pixiExe = (Get-Command pixi).Source

$action = New-ScheduledTaskAction -Execute $pixiExe -Argument "run fill-check" -WorkingDirectory $RepoRoot
$trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday, Tuesday, Wednesday, Thursday, Friday -At $Time
$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -WakeToRun `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 15) `
    -MultipleInstances IgnoreNew

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Description "basis morning fill check: push which resting orders filled (read-only)" `
    -Force | Out-Null

Write-Host "Registered scheduled task '$TaskName' ($Time weekdays) running 'pixi run fill-check'"
