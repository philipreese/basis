<#
.SYNOPSIS
    Pins the -StagedOnly scoping behavior of verify-project.ps1 (#936).
.DESCRIPTION
    Builds a throwaway git repo, then exercises verify-project.ps1 -StagedOnly
    against it in two scenarios:
      1. A docs-only staged change - test-backend/test-frontend must both be
         skipped (no pixi invocation at all).
      2. A staged frontend change with no frontend/node_modules - must fail
         fast with the one-line "npm ci --prefix frontend" instruction, never
         a vitest-not-found trace.
#>
$ErrorActionPreference = "Stop"
$RepoRoot = (git rev-parse --show-toplevel).Trim()
$ScriptPath = Join-Path $RepoRoot "scripts/verify-project.ps1"

$Failures = @()

function Invoke-Selftest {
    param([string]$TempDir)

    Push-Location $TempDir
    try {
        git init -q .
        git config user.email "selftest@example.com"
        git config user.name "selftest"
        git config core.autocrlf false
        git checkout -q -b 999-selftest

        Set-Content -Path "pixi.toml" -Value "[tasks]`nlint = `"echo lint`"`ntest = `"echo test`"`n"
        Set-Content -Path "README.md" -Value "# selftest`n"
        New-Item -ItemType Directory -Path "backend" | Out-Null
        New-Item -ItemType Directory -Path "frontend" | Out-Null
        git add .
        git commit -q -m "chore(selftest): Seed repo"

        # Scenario 1: docs-only staged change - both steps skipped, fast exit.
        Add-Content -Path "README.md" -Value "docs change"
        git add README.md
        $out1 = & powershell.exe -ExecutionPolicy Bypass -File $ScriptPath -StagedOnly -SkipSecrets 2>&1 | Out-String
        $exit1 = $LASTEXITCODE

        if ($out1 -notmatch "No staged backend/pixi files - skipping lint and test-backend") {
            $script:Failures += "Scenario 1: expected backend-skip message, got:`n$out1"
        }
        if ($out1 -notmatch "No staged frontend files - skipping test-frontend") {
            $script:Failures += "Scenario 1: expected frontend-skip message, got:`n$out1"
        }
        if ($out1 -match "vitest") {
            $script:Failures += "Scenario 1: docs-only run must never mention vitest, got:`n$out1"
        }
        if ($exit1 -ne 0) {
            $script:Failures += "Scenario 1: expected exit 0 for a docs-only commit, got $exit1"
        }
        git reset -q HEAD README.md

        # Scenario 2: staged frontend change, no node_modules - fail fast, one line.
        Set-Content -Path "frontend/package.json" -Value '{"name":"selftest-frontend"}'
        git add frontend/package.json
        $out2 = & powershell.exe -ExecutionPolicy Bypass -File $ScriptPath -StagedOnly -SkipSecrets 2>&1 | Out-String
        $exit2 = $LASTEXITCODE

        if ($out2 -notmatch [regex]::Escape("frontend deps missing - run: npm ci --prefix frontend")) {
            $script:Failures += "Scenario 2: expected the one-line frontend-deps-missing message, got:`n$out2"
        }
        if ($out2 -match "vitest") {
            $script:Failures += "Scenario 2: must fail before ever invoking vitest, got:`n$out2"
        }
        if ($exit2 -eq 0) {
            $script:Failures += "Scenario 2: expected a non-zero exit when frontend deps are missing, got $exit2"
        }
    } finally {
        Pop-Location
    }
}

$TempDir = Join-Path ([System.IO.Path]::GetTempPath()) ("basis-hook-selftest-" + [System.Guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $TempDir | Out-Null
try {
    Invoke-Selftest -TempDir $TempDir
} finally {
    Remove-Item -Recurse -Force $TempDir -ErrorAction SilentlyContinue
}

if ($Failures.Count -gt 0) {
    Write-Host "[-] verify-hook-selftest FAILED:" -ForegroundColor Red
    foreach ($f in $Failures) { Write-Host $f -ForegroundColor Red }
    Exit 1
} else {
    Write-Host "[+] verify-hook-selftest passed: staged-only scoping behaves as pinned." -ForegroundColor Green
    Exit 0
}
