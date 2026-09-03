# Registers (or updates) the Windows Scheduled Task that runs the midday exit
# pass (backend/midday_exits.py, #960).
#
#   .\scripts\register-midday-exits-task.ps1               # 12:30 weekdays
#   .\scripts\register-midday-exits-task.ps1 -Time 13:00   # custom time
#   .\scripts\register-midday-exits-task.ps1 -Unregister   # remove the task
#
# An exits-only executor run during the session: fires profit-target /
# loss-limit / time-rule closes against live quotes and re-prices last night's
# unfilled resting DAY exits to the current mid, under every guard the nightly
# run applies. Entries, rolls, and rung escalation stay nightly. Placed here
# because a position could otherwise swing from a profit-target exit to a 2x
# loss-limit breach between two nightly checks (B07, 2026-09-02).

param(
    [string]$Time = "12:30",
    [switch]$Unregister
)

$ErrorActionPreference = "Stop"
$TaskName = "basis-midday-exits"
$RepoRoot = Split-Path -Parent $PSScriptRoot

if ($Unregister) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "Removed scheduled task '$TaskName'."
    exit 0
}

$pixiExe = (Get-Command pixi).Source

$action = New-ScheduledTaskAction -Execute $pixiExe -Argument "run midday-exits" -WorkingDirectory $RepoRoot
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
    -Description "basis midday exit pass: exits-only executor run (no entries, no rolls, no rung escalation)" `
    -Force | Out-Null

Write-Host "Registered scheduled task '$TaskName' ($Time weekdays) running 'pixi run midday-exits'"
