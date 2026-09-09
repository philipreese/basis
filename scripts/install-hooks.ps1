<#
.SYNOPSIS
    Installs the repo's git hooks: lint at commit, the test suite at push (#988, #997).
.DESCRIPTION
    pre-commit runs scripts/verify-project.ps1 -StagedOnly (ruff check + format,
    seconds, scoped to the staged diff). pre-push runs -PrePush (test-backend and,
    when frontend files are in the pushed commits, test-frontend), scoped to the
    commits git is about to push, with secrets scan scoped to pushed files and
    redundant warning-only workflow checks skipped while the blocking branch
    guard still runs (#997). CI runs the same suite on the PR.

    Git worktrees share the main checkout's .git/hooks directory, and hooks
    are never tracked by git itself, so every worktree needs this run once
    (or after the hook script's logic changes).
#>
$ErrorActionPreference = "Stop"

$hooksDir = Join-Path (git rev-parse --git-common-dir).Trim() "hooks"
if (-not (Test-Path $hooksDir)) { New-Item -ItemType Directory -Path $hooksDir | Out-Null }

$preCommit = @'
#!/bin/sh
# Lint only (#988): the test suite runs on push, and in CI.
powershell.exe -ExecutionPolicy Bypass -File ./scripts/verify-project.ps1 -StagedOnly
if [ $? -ne 0 ]; then
    echo 'Pre-commit lint failed! Commit aborted.'
    exit 1
fi
'@

$prePush = @'
#!/bin/sh
# Test suite on push (#988), scoped to the commits being pushed.
# git feeds one "<local ref> <local sha> <remote ref> <remote sha>" line per ref on stdin.
zero=0000000000000000000000000000000000000000
ranges=""
while read local_ref local_sha remote_ref remote_sha; do
    [ "$local_sha" = "$zero" ] && continue          # ref deletion: nothing to test
    if [ "$remote_sha" != "$zero" ]; then
        base=$remote_sha                             # updating a ref the remote already has
    else
        base=$(git merge-base "$local_sha" origin/main 2>/dev/null)   # new branch: diff from main
    fi
    if [ -n "$base" ]; then
        ranges="$ranges $base..$local_sha"
    else
        ranges=""                                    # no base: verify-project runs everything
        break
    fi
done
powershell.exe -ExecutionPolicy Bypass -File ./scripts/verify-project.ps1 -PrePush -PushRanges "$ranges"
if [ $? -ne 0 ]; then
    echo 'Pre-push tests failed! Push aborted.'
    exit 1
fi
'@

# sh rejects CRLF, and this file may be checked out with CRLF under autocrlf,
# so the hooks are written LF-only, UTF-8 without BOM, regardless.
function Write-Hook {
    param([string]$Path, [string]$Body)
    $lf = ($Body -replace "`r`n", "`n").TrimEnd("`n") + "`n"
    [System.IO.File]::WriteAllText($Path, $lf, (New-Object System.Text.UTF8Encoding($false)))
}

$preCommitPath = Join-Path $hooksDir "pre-commit"
$prePushPath = Join-Path $hooksDir "pre-push"
Write-Hook -Path $preCommitPath -Body $preCommit
Write-Hook -Path $prePushPath -Body $prePush
Write-Host "[+] Installed pre-commit (lint) hook at $preCommitPath" -ForegroundColor Green
Write-Host "[+] Installed pre-push (tests) hook at $prePushPath" -ForegroundColor Green
