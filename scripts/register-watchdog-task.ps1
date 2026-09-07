# Registers (or updates) the Windows Scheduled Task that runs the dead-man
# watchdog each weekday night, after the executor's evening window.
#
#   .\scripts\register-watchdog-task.ps1              # 10:00 PM local, Mon-Fri
#   .\scripts\register-watchdog-task.ps1 -Time 22:30  # custom time
#   .\scripts\register-watchdog-task.ps1 -Unregister  # remove the task
#
# The watchdog (scripts/watchdog.ps1) pushes an urgent ntfy alert when the
# executor's heartbeat is missing or stale — the executor cannot report its
# own death (spec/supervision.md, #72).

param(
    [string]$Time = "22:00",
    [switch]$Unregister
)

$ErrorActionPreference = "Stop"
$TaskName = "basis-watchdog"
$RepoRoot = Split-Path -Parent $PSScriptRoot

if ($Unregister) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "Removed scheduled task '$TaskName'."
    exit 0
}

function Resolve-StablePwsh {
    # The Store's versioned directory vanishes on update; its alias is a reparse
    # point the Store keeps current. The 2026-09-04 22:00 run failed with
    # 0x80070002 because the registered versioned pwsh directory had vanished.
    if ($env:LOCALAPPDATA) {
        $aliasPath = Join-Path $env:LOCALAPPDATA "Microsoft\WindowsApps\pwsh.exe"
        if (Test-Path -LiteralPath $aliasPath -PathType Leaf) { return $aliasPath }
    }
    # Without the alias, (Get-Command pwsh).Source is itself usually the versioned Store
    # path — the exact failure this function exists to avoid — so reject that shape and
    # fall through to System32 powershell.exe, the most version-stable interpreter on the
    # box. watchdog.ps1 uses nothing beyond Windows PowerShell 5.1.
    $pwshCommand = Get-Command pwsh -ErrorAction SilentlyContinue
    if ($pwshCommand -and $pwshCommand.Source -notmatch '\\WindowsApps\\Microsoft\.PowerShell_') {
        return $pwshCommand.Source
    }
    return (Get-Command powershell).Source
}

$script = Join-Path $RepoRoot "scripts\watchdog.ps1"
$pwshExe = Resolve-StablePwsh

$action = New-ScheduledTaskAction -Execute $pwshExe -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$script`"" -WorkingDirectory $RepoRoot
$trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday, Tuesday, Wednesday, Thursday, Friday -At $Time
$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -WakeToRun `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 5) `
    -MultipleInstances IgnoreNew

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Description "basis dead-man watchdog: alerts when the executor heartbeat is stale" `
    -Force | Out-Null

Write-Host "Registered scheduled task '$TaskName' ($Time weekdays) running scripts\watchdog.ps1"
