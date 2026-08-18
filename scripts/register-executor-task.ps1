# Registers (or updates) the Windows Scheduled Task that runs the basis
# Executor (Paper) nightly pipeline via the Gateway lifecycle wrapper (#68):
# start IB Gateway through IBC -> poll the API port -> run the executor ->
# stop Gateway. Holiday evenings run the executor's heartbeat-only path
# without launching Gateway.
#
#   .\scripts\register-executor-task.ps1              # 18:45 local, Mon-Fri
#   .\scripts\register-executor-task.ps1 -Time 19:15  # custom time
#   .\scripts\register-executor-task.ps1 -Unregister  # remove the task
#
# Prerequisites: scripts/setup-ibc.ps1 completed, IBC_START_SCRIPT in .env.
# Digest delivery needs NTFY_TOPIC in .env.

param(
    [string]$Time = "18:45",
    [switch]$Unregister
)

$ErrorActionPreference = "Stop"
$TaskName = "basis-executor"
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

$action = New-ScheduledTaskAction -Execute $pixi -Argument "run executor-nightly" -WorkingDirectory $RepoRoot
$trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday, Tuesday, Wednesday, Thursday, Friday -At $Time
$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 30) `
    -MultipleInstances IgnoreNew

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Description "basis Executor (Paper): IBC Gateway start-on-demand + nightly trading pipeline" `
    -Force | Out-Null

Write-Host "Registered scheduled task '$TaskName' ($Time Mon-Fri) running 'pixi run executor-nightly' in $RepoRoot."
