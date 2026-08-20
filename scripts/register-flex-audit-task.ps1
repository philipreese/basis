# Registers (or updates) the Windows Scheduled Task that runs the weekly
# Flex Query audit (backend/flex_audit.py, #74).
#
#   .\scripts\register-flex-audit-task.ps1               # Saturday 09:00 local
#   .\scripts\register-flex-audit-task.ps1 -Time 10:30   # custom time
#   .\scripts\register-flex-audit-task.ps1 -Unregister   # remove the task
#
# The audit cross-checks the broker's Activity Flex statement against the
# incremental fills ledger and pushes the result over ntfy. Requires
# IBKR_FLEX_TOKEN / IBKR_FLEX_QUERY_ID in .env (spec/design/executor-paper.md §4.5).

param(
    [string]$Time = "09:00",
    [switch]$Unregister
)

$ErrorActionPreference = "Stop"
$TaskName = "basis-flex-audit"
$RepoRoot = Split-Path -Parent $PSScriptRoot

if ($Unregister) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "Removed scheduled task '$TaskName'."
    exit 0
}

$pixiExe = (Get-Command pixi).Source

$action = New-ScheduledTaskAction -Execute $pixiExe -Argument "run flex-audit" -WorkingDirectory $RepoRoot
$trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Saturday -At $Time
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
    -Description "basis weekly Flex Query audit: broker statement vs fills ledger" `
    -Force | Out-Null

Write-Host "Registered scheduled task '$TaskName' ($Time Saturdays) running 'pixi run flex-audit'"
