# Registers (or updates) the Windows Scheduled Task that keeps the LIVE IB
# Gateway running continuously under IBC (#1098).
#
#   .\scripts\register-live-gateway-task.ps1              # at logon, re-checked every 10 minutes
#   .\scripts\register-live-gateway-task.ps1 -Unregister  # remove the task
#
# Why continuous: a live IBKR login needs 2FA approval on the operator's phone.
# A Gateway started fresh each evening (the paper model) would need that
# approval every night. Under IBC's auto-restart the Gateway restarts daily
# WITHOUT re-authenticating ("in particular this avoids the need for second
# factor authentication after the initial login", IBC config.ini, "TWS
# Auto-Logoff, Auto-Restart and Cold Restart"). IBKR still requires one full
# login a week: "authentication is only required the first time during the
# week that TWS or Gateway run after 01:00 ET on Sunday" (IBC user guide).
# The live IBC ini's ColdRestartTime schedules that login for a Sunday time
# that suits you; approve the 2FA prompt then. A Windows reboot also needs a
# fresh 2FA approval.
#
# The paper Gateway is unaffected: it is still started and stopped per run,
# and every paper teardown leaves this Gateway alone (it is recognised by
# IBC_LIVE_INI / IBC_LIVE_START_SCRIPT in .env.live).
#
# Settings, and why:
#
#   /INLINE           IBC's user guide: "Make sure you use the /INLINE
#                     argument to StartTWS.bat or StartGateway.bat when
#                     starting IBC from Task Scheduler." With it the script
#                     instance "persists right through the various
#                     auto-restarts", so the task instance stays running.
#
#   AtLogOn + Interactive
#                     The Gateway is a desktop (Swing) app that IBC drives
#                     through its windows, so it needs the user's desktop
#                     session, like the paper Gateway tasks.
#
#   Repeat every 10 minutes, MultipleInstances IgnoreNew
#                     IBC's sample Task Scheduler setup restarts every 10
#                     minutes "only if there isn't an instance already
#                     running": a crashed Gateway comes back on its own, and
#                     a running one is never doubled.
#
#   ExecutionTimeLimit 0
#                     A Scheduled Task defaults to killing its action after
#                     three days, which would end a week-long session.
#
# Prerequisite: .env.live names IBC_LIVE_START_SCRIPT (a copy of
# StartGateway.bat pointing at the live IBC ini) and IBC_LIVE_INI. README ->
# "Executor (Live)" lists the live ini settings. This script does not read,
# write or print any credential.

param(
    [switch]$Unregister,
    [int]$RepeatMinutes = 10
)

$ErrorActionPreference = "Stop"
$TaskName = "basis-live-gateway"
$RepoRoot = Split-Path -Parent $PSScriptRoot
$Overlay = Join-Path $RepoRoot ".env.live"

if ($Unregister) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "Removed scheduled task '$TaskName'. The live Gateway keeps running until you close it."
    exit 0
}

if (-not (Test-Path $Overlay)) {
    throw ".env.live not found in $RepoRoot - create it first (README -> Executor (Live))"
}

# Read ONLY the start script's path from .env.live (a path, not a secret).
$startScript = $null
foreach ($line in Get-Content $Overlay) {
    if ($line -match '^\s*IBC_LIVE_START_SCRIPT\s*=\s*(.+?)\s*$') {
        $startScript = $Matches[1].Trim('"', "'")
    }
}
if (-not $startScript) { throw "IBC_LIVE_START_SCRIPT is not set in .env.live" }
if (-not (Test-Path $startScript)) { throw "The live IBC start script was not found: $startScript" }

$action = New-ScheduledTaskAction -Execute "cmd.exe" -Argument "/c `"`"$startScript`" /INLINE`""
$trigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
# An AtLogOn trigger takes no repetition directly; borrow it from a -Once
# trigger. With no -RepetitionDuration the repetition runs indefinitely.
$trigger.Repetition = (New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes $RepeatMinutes)).Repetition
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Seconds 0) `
    -MultipleInstances IgnoreNew

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Principal $principal `
    -Settings $settings `
    -Description "basis LIVE IB Gateway under IBC, running continuously (auto-restart daily, 2FA weekly)" `
    -Force | Out-Null

Write-Host "Registered scheduled task '$TaskName' (at logon, re-checked every $RepeatMinutes min)."
Write-Host "Start it now with: Start-ScheduledTask -TaskName $TaskName  (then approve the 2FA prompt on your phone)"
