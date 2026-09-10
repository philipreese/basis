<#
.SYNOPSIS
    Pins the hook split (#988) and its diff scoping (#936, #997).
.DESCRIPTION
    Builds a throwaway bare remote plus a clone, installs the real hooks into
    the clone with scripts/install-hooks.ps1, puts a fake `pixi` shim first on
    PATH (no environment solve, no network), then drives real `git commit` and
    `git push` calls through the hooks:
      1. A docs-only commit is accepted without running lint or any test.
      2. A commit with a lint error is refused at commit time.
      3. A commit with a failing test is accepted at commit time and refused
         at push time; fixing the test lets the push through, and the push
         phase skips test-frontend and git/workflow checks when range is known.
      4. A pushed frontend change with no frontend/node_modules fails fast
         with the one-line "npm ci --prefix frontend" instruction, never a
         vitest-not-found trace.
      5. A push whose local history shares no common ancestor with
         origin/main (merge-base fails) falls back to running both suites
         unscoped, rather than silently skipping one for lack of a matched
         file pattern, and full secrets/workflow checks unscoped.
      6. Scoped secrets scan catches secrets in pushed files at push time.
    The shim's `lint` task fails when any backend/*.py contains LINT-ERROR;
    `test-backend` fails when any contains TEST-FAIL.
#>
$ErrorActionPreference = "Stop"
$RepoRoot = (git rev-parse --show-toplevel).Trim()

$Failures = @()

# Merge stderr into stdout inside cmd so PowerShell never sees a native
# stderr write as an error record; $LASTEXITCODE is git's own.
function Invoke-Git {
    param([string]$ArgLine)
    $out = cmd /c "git $ArgLine 2>&1" | Out-String
    return @{ Out = $out; Exit = $LASTEXITCODE }
}

function Write-Shim {
    param([string]$BinDir)
    New-Item -ItemType Directory -Path $BinDir | Out-Null
    # goto-based on purpose: `exit /b N` inside a parenthesised `&&` group
    # does not propagate N as cmd's exit code.
    $shim = @(
        '@echo off',
        'if not "%1"=="run" goto :unsupported',
        'if "%2"=="lint" goto :lint',
        'if "%2"=="test-backend" goto :testbackend',
        'if "%2"=="test-frontend" goto :testfrontend',
        'goto :unsupported',
        ':lint',
        'findstr /s /m /c:"LINT-ERROR" backend\*.py >nul 2>nul',
        'if not errorlevel 1 goto :lintfail',
        'echo shim lint: clean',
        'exit /b 0',
        ':lintfail',
        'echo shim lint: LINT-ERROR found',
        'exit /b 1',
        ':testbackend',
        'findstr /s /m /c:"TEST-FAIL" backend\*.py >nul 2>nul',
        'if not errorlevel 1 goto :testfail',
        'echo shim test-backend: passed',
        'exit /b 0',
        ':testfail',
        'echo shim test-backend: TEST-FAIL found',
        'exit /b 1',
        ':testfrontend',
        'echo shim test-frontend: vitest',
        'exit /b 0',
        ':unsupported',
        'echo pixi-shim: unsupported %*',
        'exit /b 2'
    ) -join "`r`n"
    Set-Content -Path (Join-Path $BinDir "pixi.cmd") -Value $shim
}

function Invoke-Selftest {
    param([string]$TempDir)

    $remote = Join-Path $TempDir "remote.git"
    $work = Join-Path $TempDir "work"
    git init -q --bare -b main $remote
    git init -q -b main $work

    Push-Location $work
    try {
        git remote add origin $remote
        git config user.email "selftest@example.com"
        git config user.name "selftest"
        git config core.autocrlf false

        New-Item -ItemType Directory -Path "scripts", "backend", "frontend" | Out-Null
        Copy-Item (Join-Path $RepoRoot "scripts/verify-project.ps1") "scripts/"
        Copy-Item (Join-Path $RepoRoot "scripts/install-hooks.ps1") "scripts/"
        Set-Content -Path "pixi.toml" -Value "[tasks]`nlint = `"shim`"`ntest-backend = `"shim`"`ntest-frontend = `"shim`"`n"
        Set-Content -Path "README.md" -Value "# selftest`n"
        Set-Content -Path "backend/ok.py" -Value "x = 1`n"
        git add .
        git commit -q -m "chore(selftest): Seed repo"
        git push -q -u origin main
        git checkout -q -b 999-selftest

        & powershell.exe -ExecutionPolicy Bypass -File "scripts/install-hooks.ps1" | Out-Null
        foreach ($hook in @("commit-msg", "pre-commit", "pre-push")) {
            if (-not (Test-Path ".git/hooks/$hook")) { $script:Failures += "install-hooks did not write .git/hooks/$hook" }
        }

        # Scenario 1: docs-only commit - lint skipped, no test, accepted.
        Add-Content -Path "README.md" -Value "docs change"
        git add README.md
        $r = Invoke-Git 'commit -m "docs(selftest): Docs only"'
        if ($r.Exit -ne 0) { $script:Failures += "Scenario 1: docs-only commit refused (exit $($r.Exit)):`n$($r.Out)" }
        if ($r.Out -notmatch "No staged backend/pixi files - skipping lint") { $script:Failures += "Scenario 1: expected the lint-skip message, got:`n$($r.Out)" }
        if ($r.Out -match "shim (lint|test-backend|test-frontend)") { $script:Failures += "Scenario 1: docs-only commit must run no pixi task, got:`n$($r.Out)" }

        # Scenario 1b: the commit-msg hook strips AI attribution but keeps a
        # HUMAN co-author (#1016). Pinned here because the rule is invisible
        # otherwise: a stripped trailer leaves no trace in the commit it was
        # removed from, so a hook that silently stopped working would look
        # exactly like an agent that had stopped adding them.
        Add-Content -Path "README.md" -Value "attribution change"
        git add README.md
        $msgFile = Join-Path ([System.IO.Path]::GetTempPath()) "basis-selftest-msg.txt"
        $lf = "docs(selftest): Attribution strip`n`nBody.`n`nCo-authored-by: Jane Dev <jane@example.com>`nCo-Authored-By: Claude Opus 5 <noreply@anthropic.com>`nClaude-Session: https://claude.ai/code/session_x`n"
        [System.IO.File]::WriteAllText($msgFile, $lf.Replace("`r`n", "`n"), (New-Object System.Text.UTF8Encoding($false)))
        $r = Invoke-Git "commit -F `"$msgFile`""
        Remove-Item -Path $msgFile -ErrorAction SilentlyContinue
        if ($r.Exit -ne 0) { $script:Failures += "Scenario 1b: commit refused (exit $($r.Exit)):`n$($r.Out)" }
        $body = (git log -1 --format=%B).Trim()
        if ($body -match "Claude-Session|Co-Authored-By:\s*Claude") { $script:Failures += "Scenario 1b: AI attribution survived the commit-msg hook:`n$body" }
        if ($body -notmatch "Jane Dev") { $script:Failures += "Scenario 1b: the human co-author was stripped too:`n$body" }

        # Scenario 2: lint error - refused at commit.
        $before = (git rev-parse HEAD).Trim()
        Set-Content -Path "backend/bad.py" -Value "# LINT-ERROR`n"
        git add backend/bad.py
        $r = Invoke-Git 'commit -m "feat(selftest): Lint error"'
        if ($r.Exit -eq 0) { $script:Failures += "Scenario 2: commit with a lint error was accepted:`n$($r.Out)" }
        if ($r.Out -notmatch "shim lint: LINT-ERROR found") { $script:Failures += "Scenario 2: expected the shim lint failure, got:`n$($r.Out)" }
        if ($r.Out -match "shim test-backend") { $script:Failures += "Scenario 2: commit phase must never run test-backend, got:`n$($r.Out)" }
        if ((git rev-parse HEAD).Trim() -ne $before) { $script:Failures += "Scenario 2: HEAD moved despite the refused commit" }
        git rm -q -f --cached backend/bad.py
        Remove-Item backend/bad.py

        # Scenario 3: failing test - accepted at commit, refused at push, then fixed.
        Set-Content -Path "backend/broken.py" -Value "# TEST-FAIL`n"
        git add backend/broken.py
        $r = Invoke-Git 'commit -m "feat(selftest): Failing test"'
        if ($r.Exit -ne 0) { $script:Failures += "Scenario 3: commit with a failing test must be accepted at commit time (exit $($r.Exit)):`n$($r.Out)" }
        if ($r.Out -notmatch "shim lint: clean") { $script:Failures += "Scenario 3: expected lint to run at commit, got:`n$($r.Out)" }
        if ($r.Out -match "shim test-backend") { $script:Failures += "Scenario 3: commit phase must never run test-backend, got:`n$($r.Out)" }

        $r = Invoke-Git 'push -u origin 999-selftest'
        if ($r.Exit -eq 0) { $script:Failures += "Scenario 3: push with a failing test was accepted:`n$($r.Out)" }
        if ($r.Out -notmatch "shim test-backend: TEST-FAIL found") { $script:Failures += "Scenario 3: expected the shim test-backend failure at push, got:`n$($r.Out)" }
        if ($r.Out -notmatch "No pushed frontend files - skipping test-frontend") { $script:Failures += "Scenario 3: push phase should skip test-frontend for a backend-only push, got:`n$($r.Out)" }
        if ($r.Out -notmatch [regex]::Escape("Branch naming check passed (999-selftest).")) { $script:Failures += "Scenario 3: a known-range push must still run the branch guard, got:`n$($r.Out)" }
        if ((git ls-remote --heads origin 999-selftest | Out-String).Trim()) { $script:Failures += "Scenario 3: remote received the branch despite the refused push" }

        Set-Content -Path "backend/broken.py" -Value "# fixed`n"
        git add backend/broken.py
        $r = Invoke-Git 'commit -m "fix(selftest): Fix test"'
        if ($r.Exit -ne 0) { $script:Failures += "Scenario 3: fix commit refused (exit $($r.Exit)):`n$($r.Out)" }
        $r = Invoke-Git 'push -u origin 999-selftest'
        if ($r.Exit -ne 0) { $script:Failures += "Scenario 3: push after the fix refused (exit $($r.Exit)):`n$($r.Out)" }
        if ($r.Out -notmatch "shim test-backend: passed") { $script:Failures += "Scenario 3: expected test-backend to run and pass at push, got:`n$($r.Out)" }
        if (-not (git ls-remote --heads origin 999-selftest | Out-String).Trim()) { $script:Failures += "Scenario 3: remote did not receive the branch after the passing push" }

        # Scenario 4: pushed frontend change, no node_modules - fail fast, one line.
        Set-Content -Path "frontend/package.json" -Value '{"name":"selftest-frontend"}'
        git add frontend/package.json
        $r = Invoke-Git 'commit -m "feat(selftest): Frontend change"'
        if ($r.Exit -ne 0) { $script:Failures += "Scenario 4: frontend commit refused at commit time (exit $($r.Exit)):`n$($r.Out)" }
        $r = Invoke-Git 'push origin 999-selftest'
        if ($r.Exit -eq 0) { $script:Failures += "Scenario 4: push with missing frontend deps was accepted:`n$($r.Out)" }
        if ($r.Out -notmatch [regex]::Escape("frontend deps missing - run: npm ci --prefix frontend")) { $script:Failures += "Scenario 4: expected the one-line frontend-deps-missing message, got:`n$($r.Out)" }
        if ($r.Out -match "vitest") { $script:Failures += "Scenario 4: must fail before ever invoking vitest, got:`n$($r.Out)" }
        if ($r.Out -notmatch "No pushed backend/pixi files - skipping test-backend") { $script:Failures += "Scenario 4: a frontend-only push should skip test-backend, got:`n$($r.Out)" }

        # Scenario 5: a push whose local history shares no common ancestor with
        # origin/main (git merge-base fails) - the pre-push hook cannot derive a
        # range at all, and the fail-closed reading must run both suites
        # unscoped, not skip either one for lack of a touched-file match.
        # Orphan checkout keeps the working tree as-is (including pixi.toml,
        # so Verify-Python's -PrePush branch still runs) - only the git
        # history is disconnected from main, which is the one thing this
        # scenario needs to exercise.
        git checkout -q --orphan 998-selftest-orphan
        Set-Content -Path "backend/orphaned.py" -Value "x = 1`n"
        git add -A
        $r = Invoke-Git 'commit -m "chore(selftest): Orphan branch, no shared history with main"'
        if ($r.Exit -ne 0) { $script:Failures += "Scenario 5: orphan commit refused (exit $($r.Exit)):`n$($r.Out)" }
        $r = Invoke-Git 'push origin 998-selftest-orphan'
        if ($r.Out -notmatch "No push range supplied - running test-backend and test-frontend unscoped") { $script:Failures += "Scenario 5: an unresolvable merge-base must fall back to the unscoped message, got:`n$($r.Out)" }
        if ($r.Out -notmatch "No push range supplied - running secrets scan and workflow checks unscoped") { $script:Failures += "Scenario 5: an unresolvable merge-base must fall back to unscoped secrets and workflow checks, got:`n$($r.Out)" }
        if ($r.Out -notmatch "shim test-backend") { $script:Failures += "Scenario 5: unscoped fallback must still run test-backend even though nothing matched a backend pattern by scoped diff, got:`n$($r.Out)" }
        if ($r.Out -notmatch [regex]::Escape("frontend deps missing - run: npm ci --prefix frontend")) { $script:Failures += "Scenario 5: unscoped fallback must also attempt test-frontend (forced true), which fails fast on missing deps here; got:`n$($r.Out)" }

        # Scenario 6 (#997): scoped secrets scan on push - a secret in a pushed
        # file is caught and blocks push, while a clean push skips/passes fast.
        git checkout -q 999-selftest
        Set-Content -Path "backend/leaky.py" -Value 'api_key = "super-secret-token"'
        git add backend/leaky.py
        $r = Invoke-Git 'commit -m "feat(selftest): Leaked secret" --no-verify'
        $r = Invoke-Git 'push origin 999-selftest'
        if ($r.Exit -eq 0) { $script:Failures += "Scenario 6: push with a secret should be refused:`n$($r.Out)" }
        if ($r.Out -notmatch "Security Audit Failed: Potential hardcoded secrets found!") { $script:Failures += "Scenario 6: expected secret scan failure on push, got:`n$($r.Out)" }
        git rm -q -f backend/leaky.py
        # Scenario 4's push was refused, so frontend/package.json never reached
        # the remote; drop it here too so this push's range carries no
        # frontend file (the point being tested is the secrets scan, not
        # frontend scoping).
        git rm -q -f frontend/package.json
        $r = Invoke-Git 'commit -m "fix(selftest): Remove secret" --no-verify'
        $r = Invoke-Git 'push origin 999-selftest'
        if ($r.Exit -ne 0) { $script:Failures += "Scenario 6: clean push after secret removal was refused (exit $($r.Exit)):`n$($r.Out)" }

        # An unpushed secret elsewhere in the worktree must not poison a clean
        # in-scope push. This distinguishes the scoped scan from the old
        # full-tree scan, which would reject this push.
        New-Item -ItemType Directory -Path "outside" | Out-Null
        Set-Content -Path "outside/unpushed-secret.py" -Value 'api_key = "super-secret-token"'
        Set-Content -Path "backend/clean.py" -Value "x = 2`n"
        git add backend/clean.py
        $r = Invoke-Git 'commit -m "feat(selftest): Clean scoped push" --no-verify'
        if ($r.Exit -ne 0) { $script:Failures += "Scenario 6: clean in-scope commit refused (exit $($r.Exit)):`n$($r.Out)" }
        $r = Invoke-Git 'push origin 999-selftest'
        if ($r.Exit -ne 0) { $script:Failures += "Scenario 6: clean in-scope push was refused by an unpushed secret elsewhere (exit $($r.Exit)):`n$($r.Out)" }
    } finally {
        Pop-Location
    }
}

$TempDir = Join-Path ([System.IO.Path]::GetTempPath()) ("basis-hook-selftest-" + [System.Guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $TempDir | Out-Null
$SavedPath = $env:PATH
try {
    $binDir = Join-Path $TempDir "bin"
    Write-Shim -BinDir $binDir
    $env:PATH = "$binDir;$SavedPath"
    Invoke-Selftest -TempDir $TempDir
} finally {
    $env:PATH = $SavedPath
    Remove-Item -Recurse -Force $TempDir -ErrorAction SilentlyContinue
}

if ($Failures.Count -gt 0) {
    Write-Host "[-] verify-hook-selftest FAILED:" -ForegroundColor Red
    foreach ($f in $Failures) { Write-Host $f -ForegroundColor Red }
    Exit 1
} else {
    Write-Host "[+] verify-hook-selftest passed: lint at commit, tests at push, scoped to the diff." -ForegroundColor Green
    Exit 0
}
