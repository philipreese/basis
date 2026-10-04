# Registers (or updates) the Windows Scheduled Task that runs the basis
# Executor (LIVE) nightly (#1065): probe the live API port -> run the live
# executor -> back up the live database. Since #1098 it never starts or stops
# a Gateway: the live Gateway runs continuously under IBC
# (register-live-gateway-task.ps1), so its 2FA login lasts the week. When it
# is not logged in, the run refuses with an urgent push saying so.
#
#   .\scripts\register-live-executor-task.ps1              # 19:30 local, Mon-Fri
#   .\scripts\register-live-executor-task.ps1 -Time 19:45  # custom time
#   .\scripts\register-live-executor-task.ps1 -Unregister  # remove the task
#
# The run is a DRY RUN every night until .env.live sets IBKR_LIVE_ARM=TRANSMIT.
# Registering this task does not arm anything.
#
# Prerequisites (README -> "Executor (Live)"):
#   - .env.live exists beside .env (IBKR_TRADING_MODE=live, IBKR_LIVE_ACCOUNT_ID,
#     IBKR_LIVE_GATEWAY_PORT, IBKR_GATEWAY_PORT, IBC_LIVE_START_SCRIPT,
#     IBC_LIVE_INI, BASIS_LIVE_STAKE_<book>);
#   - a separate IBC config + start script for the live API login, which the
#     operator writes and types the credentials into themselves;
#   - the live Gateway task (register-live-gateway-task.ps1) running.
# The default time sits after the paper executor (18:45, 30-minute limit).

param(
    [string]$Time = "19:30",
    [switch]$Unregister
)

$ErrorActionPreference = "Stop"
$TaskName = "basis-live-executor"
$RepoRoot = Split-Path -Parent $PSScriptRoot

if ($Unregister) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "Removed scheduled task '$TaskName'."
    exit 0
}

if (-not (Test-Path (Join-Path $RepoRoot ".env.live"))) {
    throw ".env.live not found in $RepoRoot - create it first (README -> Executor (Live))"
}

$pixi = (Get-Command pixi -ErrorAction SilentlyContinue)?.Source
if (-not $pixi) {
    $candidate = Join-Path $env:USERPROFILE ".pixi\bin\pixi.exe"
    if (Test-Path $candidate) { $pixi = $candidate }
    else { throw "pixi not found on PATH or at $candidate" }
}

$action = New-ScheduledTaskAction -Execute $pixi -Argument "run live-executor-nightly" -WorkingDirectory $RepoRoot
$trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday, Tuesday, Wednesday, Thursday, Friday -At $Time
$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -WakeToRun `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 30) `
    -MultipleInstances IgnoreNew

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Description "basis Executor (LIVE): stage-1 share-book run against the persistent live Gateway (dry run unless armed)" `
    -Force | Out-Null

Write-Host "Registered scheduled task '$TaskName' ($Time Mon-Fri) running 'pixi run live-executor-nightly' in $RepoRoot."
