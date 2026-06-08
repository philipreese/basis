<#
.SYNOPSIS
    Standardized project verification script to run linters, tests, and security scans.
.DESCRIPTION
    Auto-detects project runtime (Node.js, Python, .NET, Go) and executes local verification quality gates.
#>
[CmdletBinding()]
Param(
    [switch]$SkipSecrets
)

$ErrorActionPreference = "Stop"
$Global:HasErrors = $false

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

# Secret Scanning (excluding dependency/build dirs)
function Scan-Secrets {
    if ($SkipSecrets) { return }
    Write-Host "[i] Scanning for hardcoded secrets..." -ForegroundColor Yellow
    $ExcludeDirs = @('.git', 'node_modules', '.venv', 'bin', 'obj', 'dist', 'build', '.pixi')
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
    if (-not (Test-Path "frontend/package.json")) { return $false }
    Write-Host "[i] Node.js project detected." -ForegroundColor Cyan
    try {
        $pkg = Get-Content "frontend/package.json" -Raw | ConvertFrom-Json
        if ($pkg.scripts -and $pkg.scripts.lint) {
            Invoke-External -Name "npm run lint --prefix frontend" -Command { npm run lint --prefix frontend }
        }
        if ($pkg.scripts -and $pkg.scripts.test) {
            Invoke-External -Name "npm run test --prefix frontend" -Command { npm run test --prefix frontend }
        }
    } catch {
        Write-Warning "[-] Failed to read/parse frontend/package.json: $_"
        $Global:HasErrors = $true
    }
    return $true
}

# Python project verification
function Verify-Python {
    if (-not ((Test-Path "requirements.txt") -or (Test-Path "pyproject.toml") -or (Test-Path "setup.py"))) { return $false }
    Write-Host "[i] Python project detected." -ForegroundColor Cyan
    
    if (Test-Path "pixi.toml") {
        Invoke-External -Name "pixi run test-backend" -Command { pixi run test-backend }
    } else {
        if (Get-Command "flake8" -ErrorAction SilentlyContinue) {
            Invoke-External -Name "flake8" -Command { flake8 . }
        } elseif (Get-Command "pylint" -ErrorAction SilentlyContinue) {
            Invoke-External -Name "pylint" -Command { pylint . }
        }
        
        if (Get-Command "pytest" -ErrorAction SilentlyContinue) {
            Invoke-External -Name "pytest" -Command { pytest --cov }
        }
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

# Go project verification
function Verify-Go {
    if (-not (Test-Path "go.mod")) { return $false }
    Write-Host "[i] Go project detected." -ForegroundColor Cyan
    
    Invoke-External -Name "go fmt" -Command { go fmt ./... }
    Invoke-External -Name "go test" -Command { go test -cover ./... }
    return $true
}

# --- Main Execution ---
Write-Host "==================================================" -ForegroundColor Cyan
Write-Host "[i] Starting Code Quality & Verification Pipelines" -ForegroundColor Cyan
Write-Host "==================================================" -ForegroundColor Cyan

Scan-Secrets

$projectDetected = $false
if (Verify-Node) { $projectDetected = $true }
if (Verify-Python) { $projectDetected = $true }
if (Verify-DotNet) { $projectDetected = $true }
if (Verify-Go) { $projectDetected = $true }

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
