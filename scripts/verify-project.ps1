<#
.SYNOPSIS
    Standardized project verification script to run linters, tests, and security scans.
.DESCRIPTION
    Auto-detects project runtime (Node.js, Python, .NET, Go) and executes local verification quality gates.
#>
[CmdletBinding()]
Param(
    [switch]$SkipSecrets,
    # Scope Pixi's lint/test tasks to what the staged diff actually touches
    # (used by the pre-commit hook). Without this switch, the full unscoped
    # suite runs, matching what CI runs on the PR.
    [switch]$StagedOnly
)

$ErrorActionPreference = "Stop"
$Global:HasErrors = $false

function Get-StagedFiles {
    $files = git diff --name-only --cached
    if ($null -eq $files) { return @() }
    return @($files)
}

function Test-AnyPathMatches {
    param([string[]]$Paths, [string[]]$Patterns)
    foreach ($path in $Paths) {
        foreach ($pattern in $Patterns) {
            if ($path -match $pattern) { return $true }
        }
    }
    return $false
}

# Helper to run external commands and track status
function Invoke-External {
    param(
        [string]$Name,
        [scriptblock]$Command
    )
    Write-Host "[i] Running $Name..." -ForegroundColor Yellow
    try {
        & $Command
        if ($null -ne $LASTEXITCODE -and $LASTEXITCODE -ne 0) {
            Write-Warning "[-] $Name failed with exit code $LASTEXITCODE"
            $Global:HasErrors = $true
        } else {
            Write-Host "[+] $Name passed." -ForegroundColor Green
        }
    } catch {
        Write-Warning "[-] Error running ${Name}: $_"
        $Global:HasErrors = $true
    }
}

# Resolves a Scheduled Task action's Execute string to a real file, tolerating the
# four shapes Task Scheduler accepts: a quoted path (schtasks round-trips quotes into
# the value), %VAR% (cmd syntax that PowerShell's -LiteralPath will not expand), a
# bare name found on PATH, and a path relative to the action's WorkingDirectory.
# Returns the resolved full path, or $null when nothing on disk answers to it. A malformed
# Execute (illegal path characters, over-long path) makes Test-Path/Join-Path throw under the
# script's Stop preference; that is a miss, not a reason to abort the whole verification run.
function Resolve-TaskActionExecutable {
    param([string]$Execute, [string]$WorkingDirectory)

    try {
        if ([string]::IsNullOrWhiteSpace($Execute)) { return $null }
        $path = [Environment]::ExpandEnvironmentVariables($Execute.Trim().Trim('"').Trim())
        if ([string]::IsNullOrWhiteSpace($path)) { return $null }

        if ([System.IO.Path]::IsPathRooted($path)) {
            if (Test-Path -LiteralPath $path -PathType Leaf) { return $path }
            return $null
        }

        if (-not [string]::IsNullOrWhiteSpace($WorkingDirectory)) {
            $relative = Join-Path ([Environment]::ExpandEnvironmentVariables($WorkingDirectory.Trim().Trim('"'))) $path
            if (Test-Path -LiteralPath $relative -PathType Leaf) { return $relative }
        }

        $onPath = Get-Command $path -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($onPath) { return $onPath.Source }
        return $null
    } catch {
        return $null
    }
}

# Full-gate only, never on the pre-commit path: the health of a live Scheduled Task is
# a property of this machine's registry, not of the commit being made. Running it in the
# hook would let a broken task block the very commit that fixes it, and would turn the
# hermetic -StagedOnly hook selftest (#936) red for reasons unrelated to staged-diff scoping.
function Verify-ScheduledTaskExecutables {
    if (-not (Get-Command Get-ScheduledTask -ErrorAction SilentlyContinue)) {
        Write-Host "[i] Get-ScheduledTask unavailable - skipping basis task executable checks (non-Windows CI)." -ForegroundColor DarkGray
        return
    }

    Write-Host "[i] Checking basis scheduled task executables..." -ForegroundColor Yellow

    # Filter client-side rather than with -TaskName basis-*: a wildcard matching nothing
    # must read as an empty set, so that any exception here is unambiguously a query failure.
    try {
        $tasks = @(Get-ScheduledTask -ErrorAction Stop | Where-Object { $_.TaskName -like 'basis-*' })
    } catch {
        Write-Warning "[-] Could not query basis scheduled tasks: $_"
        $Global:HasErrors = $true
        return
    }

    if ($tasks.Count -eq 0) {
        Write-Host "[i] Skipped: no basis-* tasks registered on this machine (nothing verified)." -ForegroundColor DarkGray
        return
    }

    # Actions without an Execute (e.g. ComHandler) carry no executable to verify and are
    # skipped; a task with no verifiable action at all is reported, not silently passed.
    $missing = @()
    $checked = 0
    foreach ($task in $tasks) {
        $actions = @($task.Actions | Where-Object { $_ -and $_.Execute })
        if ($actions.Count -eq 0) {
            $missing += "$($task.TaskName): no executable action to verify"
            continue
        }
        foreach ($action in $actions) {
            $checked++
            if (-not (Resolve-TaskActionExecutable -Execute $action.Execute -WorkingDirectory $action.WorkingDirectory)) {
                $missing += "$($task.TaskName): '$($action.Execute)'"
            }
        }
    }

    if ($missing.Count -gt 0) {
        Write-Warning "[-] Scheduled task executable missing: $($missing -join '; ')"
        $Global:HasErrors = $true
        return
    }
    Write-Host "[+] basis scheduled task executables passed ($($tasks.Count) task(s), $checked action(s) checked)." -ForegroundColor Green
}

# Secret Scanning (excluding dependency/build dirs)
function Scan-Secrets {
    if ($SkipSecrets) { return }
    Write-Host "[i] Scanning for hardcoded secrets..." -ForegroundColor Yellow
    $ExcludeDirs = @('.git', 'node_modules', '.venv', '.pixi', 'bin', 'obj', 'dist', 'build')
    $files = Get-ChildItem -Recurse -File | Where-Object {
        $path = $_.FullName
        $ex = $false
        foreach ($d in $ExcludeDirs) { if ($path -like "*\$d\*") { $ex = $true; break } }
        -not $ex -and $_.Extension -notin @('.md', '.png', '.jpg', '.gif', '.pdf', '.cmd', '.ps1')
    }
    $secrets = $false
    foreach ($f in $files) {
        $content = Get-Content -Path $f.FullName -Raw -ErrorAction SilentlyContinue
        if ($null -ne $content -and $content -match '(?i)(api[_-]?key|client[_-]?secret|password|db[_-]?conn|private[_-]?key)\s*[:=]\s*[''"].+[''"]') {
            Write-Warning "Potential secret found in $($f.FullName)"
            $secrets = $true
        }
    }
    if ($secrets) {
        Write-Warning "Security Audit Failed: Potential hardcoded secrets found!"
        $Global:HasErrors = $true
    } else {
        Write-Host "[+] Secret scan passed. No obvious credentials leaked." -ForegroundColor Green
    }
}

# Node.js project verification
function Verify-Node {
    if (-not (Test-Path "package.json")) { return $false }
    Write-Host "[i] Node.js project detected." -ForegroundColor Cyan
    try {
        $pkg = Get-Content "package.json" -Raw | ConvertFrom-Json
        if ($pkg.scripts -and $pkg.scripts.lint) {
            Invoke-External -Name "npm run lint" -Command { npm run lint }
        }
        if ($pkg.scripts -and $pkg.scripts.test) {
            Invoke-External -Name "npm run test" -Command { npm run test }
        }
    } catch {
        Write-Warning "[-] Failed to read/parse package.json: $_"
        $Global:HasErrors = $true
    }
    return $true
}

# Python project verification
function Verify-Python {
    if (-not ((Test-Path "requirements.txt") -or (Test-Path "pyproject.toml") -or (Test-Path "setup.py"))) { return $false }
    Write-Host "[i] Python project detected." -ForegroundColor Cyan

    # When Pixi is present, Verify-Pixi handles linting and tests
    if ((Test-Path "pixi.toml") -and (Get-Command "pixi" -ErrorAction SilentlyContinue)) {
        Write-Host "[i] Pixi detected - linting and tests delegated to Verify-Pixi." -ForegroundColor Cyan
        return $true
    }

    if (Get-Command "flake8" -ErrorAction SilentlyContinue) {
        Invoke-External -Name "flake8" -Command { flake8 . }
    } elseif (Get-Command "pylint" -ErrorAction SilentlyContinue) {
        Invoke-External -Name "pylint" -Command { pylint . }
    }

    if (Get-Command "pytest" -ErrorAction SilentlyContinue) {
        Invoke-External -Name "pytest" -Command { pytest --cov }
    }
    return $true
}

# .NET project verification
function Verify-DotNet {
    $csproj = Get-ChildItem -Filter "*.csproj" -Recurse | Where-Object { $_.FullName -notlike "*\obj\*" -and $_.FullName -notlike "*\bin\*" }
    $sln = Get-ChildItem -Filter "*.sln" -Recurse
    if (-not ($csproj -or $sln)) { return $false }
    Write-Host "[i] .NET project detected." -ForegroundColor Cyan
    
    Invoke-External -Name "dotnet format" -Command { dotnet format --verify-no-changes }
    Invoke-External -Name "dotnet test" -Command { dotnet test /p:CollectCoverage=true }
    return $true
}

# Pixi environment verification (language-agnostic; takes priority over raw binary checks)
function Verify-Pixi {
    if (-not (Test-Path "pixi.toml")) { return $false }
    Write-Host "[i] Pixi environment detected." -ForegroundColor Cyan

    if (-not (Get-Command "pixi" -ErrorAction SilentlyContinue)) {
        Write-Warning "[-] pixi.toml found but 'pixi' is not in PATH. Falling through to raw linter checks."
        return $false
    }

    if ($StagedOnly) {
        $staged = Get-StagedFiles
        $backendTouched = Test-AnyPathMatches -Paths $staged -Patterns @('^backend/', '^pixi\.toml$', '^pyproject\.toml$', '^pixi\.lock$')
        $frontendTouched = Test-AnyPathMatches -Paths ($staged | Where-Object { $_ -notmatch '^frontend/e2e/' }) -Patterns @('^frontend/')

        if ($backendTouched) {
            Invoke-External -Name "pixi run lint" -Command { pixi run lint }
            Invoke-External -Name "pixi run test-backend" -Command { pixi run test-backend }
        } else {
            Write-Host "[i] No staged backend/pixi files - skipping lint and test-backend." -ForegroundColor DarkGray
        }

        if ($frontendTouched) {
            if (-not (Test-Path "frontend/node_modules")) {
                Write-Host "frontend deps missing - run: npm ci --prefix frontend" -ForegroundColor Red
                $Global:HasErrors = $true
            } else {
                Invoke-External -Name "pixi run test-frontend" -Command { pixi run test-frontend }
            }
        } else {
            Write-Host "[i] No staged frontend files - skipping test-frontend." -ForegroundColor DarkGray
        }

        return $true
    }

    $pixiToml = Get-Content "pixi.toml" -Raw
    if ($pixiToml -match '(?m)^\s*lint\s*=') {
        Invoke-External -Name "pixi run lint" -Command { pixi run lint }
    }
    if ($pixiToml -match '(?m)^\s*test\s*=') {
        Invoke-External -Name "pixi run test" -Command { pixi run test }
    }
    return $true
}

# Go project verification
function Verify-Go {
    if (-not (Test-Path "go.mod")) { return $false }
    Write-Host "[i] Go project detected." -ForegroundColor Cyan
    
    Invoke-External -Name "go fmt" -Command { go fmt ./... }
    Invoke-External -Name "go test" -Command { go test -cover ./... }
    return $true
}

# Git Naming, Conventional Commit and Documentation Sync validations
function Verify-GitAndWorkflow {
    Write-Host "[i] Running Git Naming & Workflow Checks..." -ForegroundColor Yellow
    
    # 1. Branch Naming check
    try {
        $branch = (git rev-parse --abbrev-ref HEAD).Trim()
        if ($branch -eq "main" -or $branch -eq "master") {
            Write-Warning "[CRITICAL] Committing directly to main/master branch is strictly prohibited by Rule 07!"
            $Global:HasErrors = $true
        } elseif ($branch -notmatch '^(feat|fix|refactor|docs|test|chore)/' -and $branch -notmatch '^\d+-') {
            # `<issue-number>-slug` is what `gh issue develop` creates — the
            # issue-driven workflow's native shape. The old 'sprint' allowance
            # died with the sprint era (#358).
            Write-Warning "Branch '$branch' does not follow conventions (expected feat/, fix/, refactor/, docs/, test/, chore/ prefix or an issue branch like 123-slug)."
        } else {
            Write-Host "[+] Branch naming check passed ($branch)." -ForegroundColor Green
        }
    } catch {
        Write-Warning "Failed to check Git branch: $_"
    }

    # 2. Conventional Commit checks on the last local commit (Warning only to avoid blocking future commits during pre-commit hooks)
    try {
        $lastCommitMsg = (git log -n 1 --format=%s).Trim()
        if ($lastCommitMsg -match '^[a-z]+(\([a-zA-Z0-9_-]+\))?:\s[A-Z]') {
            if ($lastCommitMsg -match '\.$') {
                Write-Warning "[CRITICAL] Conventional Commit standard violated: Commit message should not end with a period."
            } else {
                Write-Host "[+] Conventional Commit check passed ($lastCommitMsg)." -ForegroundColor Green
            }
        } else {
            Write-Warning "[CRITICAL] Conventional Commit standard violated! Commit message description MUST start with a CAPITAL letter."
            Write-Warning "  Current message: '$lastCommitMsg'"
            Write-Warning "  Expected format: 'type(scope): Capitalized Description'"
        }
    } catch {
        Write-Warning "Failed to check last Git commit message: $_"
    }

    # 3. Documentation Sync check
    try {
        $stagedCode = (git diff --name-only --cached)
        $unstagedCode = (git diff --name-only)
        $allChanged = $stagedCode + $unstagedCode
        
        $sourceChanged = $false
        $docsChanged = $false
        
        foreach ($file in $allChanged) {
            if ($file -match '\.(py|ts|svelte|js|cs|go)$') {
                $sourceChanged = $true
            }
            if ($file -like "*README.md" -or $file -like "*CHANGELOG.md") {
                $docsChanged = $true
            }
        }
        
        if ($sourceChanged -and -not $docsChanged) {
            Write-Warning "[WARNING] Source code files modified, but neither README.md nor CHANGELOG.md was updated (Rule 06)."
        } else {
            Write-Host "[+] Documentation sync check passed." -ForegroundColor Green
        }
    } catch {
        Write-Warning "Failed to verify documentation sync: $_"
    }
}

# --- Main Execution ---
Write-Host "==================================================" -ForegroundColor Cyan
Write-Host "[i] Starting Code Quality & Verification Pipelines" -ForegroundColor Cyan
Write-Host "==================================================" -ForegroundColor Cyan

Scan-Secrets
Verify-GitAndWorkflow
if (-not $StagedOnly) { Verify-ScheduledTaskExecutables }

# #971: both console/backend ends stay pinned to IPv4. `localhost` resolves to
# ::1 first on Node 17+, and Vite has bound [::1] only across a restart — a ~2 s
# per-request timeout in the first case, a hard refusal of IPv4 clients and the
# tailnet proxy in the second. Three literals carry that, so three assertions.
Invoke-External -Name "Console proxy IPv4 default" -Command {
    # These are file reads, not external commands; clear the exit code the
    # previous external command left behind so Invoke-External reads ours.
    $Global:LASTEXITCODE = 0
    if (Test-Path "frontend/vite.config.ts") {
        $viteConfig = Get-Content "frontend/vite.config.ts" -Raw
        # Tolerates ?? and ||, and ' " ` quoting of the default.
        if ($viteConfig -match 'VITE_API_PROXY_TARGET\s*(\?\?|\|\|)\s*[''"`]https?://localhost(?=[:/''"`])') {
            throw "The console proxy must default to 127.0.0.1 to avoid the IPv6 timeout."
        }
        if ($viteConfig -notmatch 'host:\s*[''"`]127\.0\.0\.1[''"`]') {
            throw "The console dev server must pin server.host to 127.0.0.1; without it Vite can bind [::1] only and refuse IPv4 clients."
        }
    }
    if (Test-Path "pixi.toml") {
        $pixiManifest = Get-Content "pixi.toml" -Raw
        if ($pixiManifest -match '--host\s+localhost') {
            throw "Backend tasks must bind --host 127.0.0.1; the console proxy defaults to IPv4 and localhost lets the resolver decide per restart."
        }
    }
}

$projectDetected = $false
if (Verify-Pixi)   { $projectDetected = $true }
if (Verify-Node)   { $projectDetected = $true }
if (Verify-Python) { $projectDetected = $true }
if (Verify-DotNet) { $projectDetected = $true }
if (Verify-Go)     { $projectDetected = $true }

if (-not $projectDetected) {
    Write-Host "No supported package environments (Node, Python, .NET, Go) detected in root path. Running standalone validations only." -ForegroundColor Yellow
}

Write-Host "==================================================" -ForegroundColor Cyan
if ($Global:HasErrors) {
    Write-Host "[-] Verification Pipeline Failed!" -ForegroundColor Red
    Exit 1
} else {
    Write-Host "[+] All Quality and Safety Pipeline checks passed successfully!" -ForegroundColor Green
    Exit 0
}
