<#
.SYNOPSIS
    Installs the repo's pre-commit hook, scoped to the staged diff.
.DESCRIPTION
    Git worktrees share the main checkout's .git/hooks directory, and hooks
    are never tracked by git itself, so every worktree needs this run once
    (or after the hook script's logic changes).
#>
$ErrorActionPreference = "Stop"

$hooksDir = (git rev-parse --git-common-dir).Trim()
$hookPath = Join-Path $hooksDir "hooks/pre-commit"

$hookContent = @'
#!/bin/sh
# Run the project verification pipeline before commit, scoped to the staged diff
powershell.exe -ExecutionPolicy Bypass -File ./scripts/verify-project.ps1 -StagedOnly
if [ $? -ne 0 ]; then
    echo 'Pre-commit verification failed! Commit aborted.'
    exit 1
fi
'@

Set-Content -Path $hookPath -Value $hookContent -NoNewline:$false
Write-Host "[+] Installed pre-commit hook at $hookPath" -ForegroundColor Green
