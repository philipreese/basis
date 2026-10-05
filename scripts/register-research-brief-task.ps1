# Registers (or updates) the Windows Scheduled Tasks that run the research
# brief's nightly and monthly jobs (#1131 phase 2).
#
#   .\scripts\register-research-brief-task.ps1                           # defaults below
#   .\scripts\register-research-brief-task.ps1 -NightlyTime 17:40        # custom nightly time
#   .\scripts\register-research-brief-task.ps1 -MonthlyTime 23:30        # custom monthly time
#   .\scripts\register-research-brief-task.ps1 -Unregister               # remove both tasks
#
# Two tasks:
#
#   basis-research-nightly    Every weekday at -NightlyTime (default 17:35).
#                              Runs `pixi run research-snapshot` THEN
#                              `pixi run research-brief` with `&`, not `&&` —
#                              the brief step always runs even when the
#                              snapshot came back INCOMPLETE or crashed, so
#                              SOMETHING reaches the phone every night
#                              (research_brief.py's main() always pushes,
#                              even on a skip, and surfaces the newest
#                              snapshot's status when that's why it skipped
#                              — fill_check's precedent: "silence would be
#                              indistinguishable from the check not
#                              running"). research_brief.py also re-checks
#                              the snapshot's hash before it reads a price,
#                              so a stale or missing snapshot still fails
#                              loud rather than briefing something it
#                              shouldn't.
#
#   basis-research-monthly    The design (spec/research-brief.md) wants this
#                              on "the first trading evening of each month".
#                              `New-ScheduledTaskTrigger` has no -Monthly
#                              parameter set at all (only Once/Daily/Weekly/
#                              AtStartup/AtLogOn — confirmed against this
#                              machine's PowerShell: `Get-Command
#                              New-ScheduledTaskTrigger -Syntax`), so this
#                              task fires EVERY weekday at -MonthlyTime, same
#                              as nightly, and the database gate
#                              `research-brief --kind monthly --check-due`
#                              does the actual filtering: due when today is a
#                              trading day AND no MONTHLY BRIEF exists whose
#                              snapshot's as_of falls in this calendar month
#                              (research_brief.monthly_brief_due) — checking
#                              for a BRIEF, not just a snapshot, so a day
#                              whose snapshot completed but whose brief step
#                              then crashed (a model API outage, a bad key)
#                              still retries the next weekday, every weekday,
#                              until a monthly brief lands that month — a
#                              better fit for "a missed slot runs at the next
#                              opportunity" than a fixed days-1-4 window
#                              would have been. On every other weekday the
#                              gate itself is the only thing that runs (no
#                              network, no database writes). The chain is
#                              `check-due && (snapshot & brief)`: the
#                              parenthesized group means "once due, run the
#                              snapshot, then the brief regardless of the
#                              snapshot's exit code" (same always-pushes
#                              reasoning as nightly) — written as
#                              `check-due && snapshot && brief` the brief
#                              step would silently never run on an
#                              INCOMPLETE monthly snapshot either.
#
# Why this time: the design only asks that the nightly/monthly runs stay
# clear of 18:30-19:30 (the evening executor's window) — but this repo also
# registers `basis-live-executor` at 19:30 (register-live-executor-task.ps1)
# and `basis-watchdog` at 22:00 (register-watchdog-task.ps1), both writing to
# the SAME live database a monthly snapshot+brief run touches, and neither
# publishes a measured runtime. -MonthlyTime therefore defaults well clear of
# both: 23:30, after the live executor's evening work and the watchdog check
# are expected to be long done, not merely "not yet started". -NightlyTime
# (17:35, 40-minute budget, done by 18:15) sits before both the paper
# executor (18:45) and the live executor (19:30). If you change either time,
# re-check every register-*.ps1 default below for overlap:
#
#   fill-check 10:00, flex-audit 09:00 Sat, preflight 14:00,
#   midday-exits 12:30, executor (paper) 18:45, live-executor 19:30,
#   watchdog 22:00.
#
# ExecutionTimeLimit: unmeasured (flagged for the conductor to revisit after
# the first real runs — see the PR body). Nightly gets 40 minutes; monthly
# gets 90, since a full-universe 35-day-lookback filing scan is heavier and
# its true runtime has not been measured against production data.
#
# Like every other register-*.ps1 script, the times are read on the clock of
# the machine the task runs on; this repo assumes that clock is US Eastern.
#
# Prerequisites (README -> "Research brief and the operator picks book"):
# .env.live must exist (the live overlay the live-mode tasks share),
# BASIS_SEC_CONTACT and BASIS_RESEARCH_API_KEY must be set in .env. This
# script does not read, write or print either value. Before registering,
# run `pixi run research-snapshot` then `pixi run research-brief` by hand
# once with the key set, so an auth or schema problem surfaces while someone
# is watching rather than at 17:35.

param(
    [string]$NightlyTime = "17:35",
    [string]$MonthlyTime = "23:30",
    [switch]$Unregister
)

$ErrorActionPreference = "Stop"
$NightlyTask = "basis-research-nightly"
$MonthlyTask = "basis-research-monthly"
$RepoRoot = Split-Path -Parent $PSScriptRoot

if ($Unregister) {
    Unregister-ScheduledTask -TaskName $NightlyTask -Confirm:$false -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $MonthlyTask -Confirm:$false -ErrorAction SilentlyContinue
    Write-Host "Removed scheduled tasks '$NightlyTask' and '$MonthlyTask'."
    exit 0
}

if (-not (Test-Path (Join-Path $RepoRoot ".env.live"))) {
    throw ".env.live not found in $RepoRoot - create it first (README -> Research brief and the operator picks book)"
}

$pixiExe = (Get-Command pixi).Source

# Nightly: snapshot, THEN brief regardless of the snapshot's exit code
# (`&`, not `&&`) — see the always-pushes note above.
$nightlySettings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -WakeToRun `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 40) `
    -MultipleInstances IgnoreNew
$nightlyArgs = "/c `"`"$pixiExe`" run research-snapshot & `"$pixiExe`" run research-brief`""
$nightlyAction = New-ScheduledTaskAction -Execute "cmd.exe" -Argument $nightlyArgs -WorkingDirectory $RepoRoot
$nightlyTrigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday, Tuesday, Wednesday, Thursday, Friday -At $NightlyTime

Register-ScheduledTask `
    -TaskName $NightlyTask `
    -Action $nightlyAction `
    -Trigger $nightlyTrigger `
    -Settings $nightlySettings `
    -Description "basis research brief: nightly snapshot + brief, always pushes (#1131)" `
    -Force | Out-Null

# Monthly: gated by a database-aware --check-due; once due, snapshot THEN
# brief regardless of the snapshot's exit code (the parenthesized group) —
# see the chain explanation above.
$monthlySettings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -WakeToRun `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 90) `
    -MultipleInstances IgnoreNew
$monthlyArgs = (
    "/c `"`"$pixiExe`" run research-brief --kind monthly --check-due && " +
    "(`"$pixiExe`" run research-snapshot --kind monthly & `"$pixiExe`" run research-brief --kind monthly)`""
)
$monthlyAction = New-ScheduledTaskAction -Execute "cmd.exe" -Argument $monthlyArgs -WorkingDirectory $RepoRoot
$monthlyTrigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday, Tuesday, Wednesday, Thursday, Friday -At $MonthlyTime

Register-ScheduledTask `
    -TaskName $MonthlyTask `
    -Action $monthlyAction `
    -Trigger $monthlyTrigger `
    -Settings $monthlySettings `
    -Description "basis research brief: monthly snapshot + brief, database-gated to the first un-briefed trading day of the month (#1131)" `
    -Force | Out-Null

Write-Host "Registered scheduled task '$NightlyTask' ($NightlyTime weekdays)."
Write-Host "Registered scheduled task '$MonthlyTask' ($MonthlyTime every weekday; --check-due gates the real work to the first trading day with no monthly brief yet this month)."
