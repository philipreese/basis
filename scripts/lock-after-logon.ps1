# Locks the workstation shortly after sign-in (#1039).
#
# Unattended recovery after a power cut needs Windows to sign in by itself
# (auto-logon): the scheduled entrypoints are Interactive tasks and only run
# inside a signed-in session. This script closes the side effect, a desktop
# left open to anyone at the keyboard, by locking it as soon as Windows will
# accept a lock. Launched from the HKCU Run key by
# scripts/register-lock-after-logon.ps1.
#
# Two measured details (2026-10-03, Victus 15L):
#   - Windows launches sign-in items ~40 s after auto-logon, so the whole
#     window from sign-in to lock is ~45-50 s. An AtLogOn scheduled task fired
#     ~2.5 min late, and a Startup-folder shortcut fired late or not at all.
#   - LockWorkStation called before the desktop is interactive is silently
#     ignored, hence the wait for Explorer plus a 5 s margin.
#
# Every run is logged, because a lock that silently fails looks exactly like
# no lock: %LOCALAPPDATA%\basis\lock-after-logon.log

$logDir = Join-Path $env:LOCALAPPDATA 'basis'
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$log = Join-Path $logDir 'lock-after-logon.log'
"$(Get-Date -Format o) start" | Add-Content $log
for ($i = 0; $i -lt 90 -and -not (Get-Process explorer -ErrorAction SilentlyContinue); $i++) { Start-Sleep -Seconds 1 }
Start-Sleep -Seconds 5
Add-Type -Namespace Basis -Name User32 -MemberDefinition '[DllImport("user32.dll")] public static extern bool LockWorkStation();'
$ok = [Basis.User32]::LockWorkStation()
"$(Get-Date -Format o) LockWorkStation returned $ok (explorer running: $([bool](Get-Process explorer -ErrorAction SilentlyContinue)))" | Add-Content $log
