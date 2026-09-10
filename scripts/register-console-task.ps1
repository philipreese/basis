# Registers (or updates) the Windows Scheduled Task that runs the supervision
# console -- the FastAPI app, which since #1019 serves the built frontend at
# `/` as well as the API (#1021).
#
#   .\scripts\register-console-task.ps1                  # at startup, restart on failure
#   .\scripts\register-console-task.ps1 -RemoveLegacyUi  # also delete basis-console-ui
#   .\scripts\register-console-task.ps1 -Unregister      # remove the task
#
# Replaces two tasks registered by hand and documented nowhere in scripts/:
# `basis-console` (this one) and `basis-console-ui` (a permanently-running
# Vite dev server, retired by #1019 -- pass -RemoveLegacyUi to delete it).
#
# Three settings here differ from the scheduled entrypoints' scripts, because
# this is a SERVER and they are jobs that run and exit:
#
#   AtStartup + S4U   The console must survive a reboot with nobody logged
#                     in. The other tasks fire on a clock while someone is
#                     using the machine; this one is the operator's only
#                     RESUME surface (ADR-0008) and its absence is silent.
#                     S4U runs as the user without storing a password; the
#                     cost is no network credentials, which this task does
#                     not need (it binds loopback and reads the local
#                     checkout).
#
#   ExecutionTimeLimit 0
#                     A Scheduled Task defaults to killing its action after
#                     three days. For fill-check that is a safety net; for a
#                     server it is a guaranteed outage every third day, and
#                     one that looks exactly like a crash.
#
#   RestartCount      A crashed console stays dead until someone notices,
#                     and nothing pages when the console is down -- the
#                     watchdog checks the executor's heartbeat, not this.

param(
    [switch]$Unregister,
    [switch]$RemoveLegacyUi
)

$ErrorActionPreference = "Stop"
$TaskName = "basis-console"
$LegacyUiTaskName = "basis-console-ui"
$RepoRoot = Split-Path -Parent $PSScriptRoot

function Remove-TaskIfPresent {
    param([string]$Name)
    if (Get-ScheduledTask -TaskName $Name -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $Name -Confirm:$false
        Write-Host "Removed scheduled task '$Name'."
        return $true
    }
    return $false
}

if ($Unregister) {
    if (-not (Remove-TaskIfPresent -Name $TaskName)) {
        Write-Host "No scheduled task '$TaskName' to remove."
    }
    exit 0
}

if ($RemoveLegacyUi) {
    # #1019 retired the separate dev-server task. Leaving it registered means
    # a second process still serving a STALE build on :5173 -- the confusing
    # half-deployed state this whole change set exists to end.
    if (-not (Remove-TaskIfPresent -Name $LegacyUiTaskName)) {
        Write-Host "No scheduled task '$LegacyUiTaskName' present (already retired)."
    }
}

$pixiExe = (Get-Command pixi).Source

$action = New-ScheduledTaskAction -Execute $pixiExe -Argument "run server" -WorkingDirectory $RepoRoot
$trigger = New-ScheduledTaskTrigger -AtStartup
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType S4U -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Seconds 0) `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -MultipleInstances IgnoreNew

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Principal $principal `
    -Settings $settings `
    -Description "basis supervision console: FastAPI serving the API and the built UI on 127.0.0.1:8000" `
    -Force | Out-Null

Write-Host "Registered scheduled task '$TaskName' (at startup, restart-on-failure) running 'pixi run server'"
Write-Host "The console serves the API and the built UI together; run 'pixi run build-frontend' after a UI change."
