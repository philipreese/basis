<#
.SYNOPSIS
    Rebuilds the served console after main moves (#1143). Run by the
    post-merge git hook that scripts/install-hooks.ps1 writes.
.DESCRIPTION
    The backend serves a prebuilt frontend/dist (#1019), and nothing rebuilt it
    after a deploy: on 2026-10-05 dist was two days stale and hid the merged
    share-book UI (#1071/#1073) until someone ran `pixi run build-frontend` by
    hand. A stale console looks exactly like a working one, so the rebuild
    has to happen on the deploy itself, `git pull --ff-only` on main.

    What this protects, in order:
      1. The served dist is never half-written. The build goes to
         frontend/.dist-staging, and only a complete build (index.html present)
         is renamed into frontend/dist. The old dist moves aside first and is
         restored if the second rename fails. Windows has no atomic directory
         replace, so the gap between the two renames is milliseconds, not zero.
      2. A failed build is loud: a red error here plus ONE ntfy alert, and the
         old dist stays in place. A failed rebuild leaves a stale console, which
         is the exact failure this exists to end, so silence is not an option.
      3. Only main rebuilds. The hooks directory is shared by every worktree,
         and a pull in a feature worktree must not build (or alert) anything.
      4. Nothing happens when the merge did not touch frontend/.

    The alert POSTs straight to ntfy using NTFY_TOPIC / NTFY_SERVER read from
    .env, the same zero-Python path as scripts/watchdog.ps1. The backend sender
    (backend.operator.send_ntfy_with_retry) needs the pixi environment, and a
    broken environment is one of the ways a build fails.

    -Force skips the branch and diff checks and rebuilds now: the manual
    fallback when the hook did not run (a `git pull --rebase`, a reset, or a
    checkout without the hook installed).
#>
param([switch]$Force)

$ErrorActionPreference = "Stop"
$RepoRoot = (git rev-parse --show-toplevel).Trim()
Set-Location $RepoRoot

$Frontend = Join-Path $RepoRoot "frontend"
$Dist = Join-Path $Frontend "dist"
$Staging = Join-Path $Frontend ".dist-staging"
$Previous = Join-Path $Frontend ".dist-previous"

function Say([string]$Message) { Write-Host "[post-merge] $Message" }

# Native output merged into stdout inside cmd, so PowerShell 5.1 never turns
# an npm warning on stderr into a terminating error record.
function Invoke-Pixi([string]$ArgLine) {
    cmd /c "pixi $ArgLine 2>&1" | ForEach-Object { Write-Host $_ }
    return $LASTEXITCODE
}

function Send-Alert([string]$Body) {
    $envFile = Join-Path $RepoRoot ".env"
    $topic = $null
    $server = "https://ntfy.sh"
    if (Test-Path $envFile) {
        foreach ($line in Get-Content $envFile) {
            if ($line -match '^\s*NTFY_TOPIC\s*=\s*(.+?)\s*$') { $topic = $Matches[1].Trim('"') }
            if ($line -match '^\s*NTFY_SERVER\s*=\s*(.+?)\s*$') { $server = $Matches[1].Trim('"') }
        }
    }
    if (-not $topic) {
        Say "NTFY_TOPIC not set in .env - no alert sent."
        return
    }
    Say "Sending ntfy alert."
    try {
        Invoke-RestMethod -Method Post -Uri "$server/$topic" -Body $Body -TimeoutSec 15 -Headers @{
            Title    = "basis deploy: console rebuild failed"
            Priority = "high"
            Tags     = "warning"
        } | Out-Null
    } catch {
        Say "ntfy alert could not be delivered: $($_.Exception.Message)"
    }
}

function Move-Dir([string]$From, [string]$To) {
    # A directory rename fails on Windows while any file inside is open: a
    # request in flight on the live server, or Defender scanning new files.
    for ($i = 0; $i -lt 10; $i++) {
        try {
            [System.IO.Directory]::Move($From, $To)
            return $true
        } catch {
            Start-Sleep -Milliseconds 500
        }
    }
    return $false
}

function Remove-Dir([string]$Path) {
    if (Test-Path $Path) {
        try { Remove-Item -Recurse -Force $Path } catch { Say "WARNING: could not remove $Path ($($_.Exception.Message))." }
    }
}

function Fail([string]$Reason) {
    Remove-Dir $Staging
    $head = (git rev-parse --short HEAD).Trim()
    Write-Host "[post-merge] CONSOLE REBUILD FAILED at $head - $Reason" -ForegroundColor Red
    Write-Host "[post-merge] The previous frontend/dist is still being served, so the console is now STALE." -ForegroundColor Red
    Write-Host "[post-merge] Fix it, then run: powershell -ExecutionPolicy Bypass -File scripts/post-merge-rebuild.ps1 -Force" -ForegroundColor Red
    Send-Alert "Console rebuild failed after main moved to $head - $Reason. The previous build is still served (stale). Rerun scripts/post-merge-rebuild.ps1 -Force on the host."
    exit 1
}

$needDeps = -not (Test-Path (Join-Path $Frontend "node_modules"))
if (-not $Force) {
    $branch = (git rev-parse --abbrev-ref HEAD).Trim()
    if ($branch -ne "main") {
        Say "On '$branch', not main - console rebuild skipped."
        exit 0
    }
    git rev-parse -q --verify ORIG_HEAD *> $null
    if ($LASTEXITCODE -ne 0) {
        # No ORIG_HEAD means no range to diff: rebuild rather than guess "unchanged".
        Say "No ORIG_HEAD to diff against - rebuilding the console."
        $needDeps = $true
    } else {
        $changed = @(git diff --name-only ORIG_HEAD HEAD -- frontend/ | Where-Object { $_ })
        if ($changed.Count -eq 0) {
            Say "No frontend changes - console rebuild skipped."
            exit 0
        }
        Say "$($changed.Count) frontend file(s) changed - rebuilding the console."
        if ($changed -contains "frontend/package.json" -or $changed -contains "frontend/package-lock.json") {
            $needDeps = $true
        }
    }
}

# An interrupted earlier run can leave dist moved aside with nothing in its
# place. Put it back before anything else, so a crash never strands the
# console without a build.
if ((Test-Path $Previous) -and -not (Test-Path $Dist)) {
    Say "Restoring frontend/dist left aside by an interrupted run."
    if (-not (Move-Dir $Previous $Dist)) { Fail "an interrupted earlier run left frontend/dist moved aside and it could not be restored" }
}
Remove-Dir $Previous
Remove-Dir $Staging

if ($needDeps) {
    Say "Installing frontend dependencies (pixi run install-node-deps)."
    if ((Invoke-Pixi "run install-node-deps") -ne 0) { Fail "pixi run install-node-deps failed" }
    # `npm install` may rewrite the lockfile, and a dirty lockfile makes the
    # next `git pull --ff-only` refuse to run.
    if ((git status --porcelain -- frontend/package-lock.json | Out-String).Trim()) {
        Write-Host "[post-merge] WARNING: npm install modified frontend/package-lock.json; the next 'git pull --ff-only' may refuse. Inspect it, then 'git checkout -- frontend/package-lock.json'." -ForegroundColor Yellow
    }
}

Say "Building the console into frontend/.dist-staging."
if ((Invoke-Pixi "run build-frontend-staged `"$Staging`"") -ne 0) { Fail "the frontend build failed (output above)" }
if (-not (Test-Path (Join-Path $Staging "index.html"))) { Fail "the build finished but wrote no index.html" }

if (Test-Path $Dist) {
    if (-not (Move-Dir $Dist $Previous)) { Fail "could not move the current frontend/dist aside (a file in it is locked)" }
}
if (-not (Move-Dir $Staging $Dist)) {
    if ((Test-Path $Previous) -and -not (Move-Dir $Previous $Dist)) {
        Fail "could not move the new build into frontend/dist, NOR restore the old one: NO CONSOLE IS BEING SERVED (old build at frontend/.dist-previous)"
    }
    Fail "could not move the new build into frontend/dist"
}
Remove-Dir $Previous

Say "Console rebuilt at $((git rev-parse --short HEAD).Trim()); the running server serves it on the next request. Restart basis-console if the merge also changed backend code."
exit 0
