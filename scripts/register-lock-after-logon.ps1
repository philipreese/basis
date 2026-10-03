# Registers (or updates) the lock-after-sign-in step of unattended recovery
# (#1039): an HKCU Run entry that launches scripts/lock-after-logon.ps1 from
# this checkout at every sign-in.
#
#   .\scripts\register-lock-after-logon.ps1              # register / repoint
#   .\scripts\register-lock-after-logon.ps1 -Unregister  # remove the Run entry
#
# Why a Run entry and not a Scheduled Task like the other register-* scripts:
# on this host an AtLogOn task fired ~2.5 minutes after auto-logon, leaving
# the desktop open that long. Run entries launch with the user's sign-in items
# (~40 s), the earliest per-user hook that needs no admin rights. It also
# zeroes Explorer's StartupDelayInMSec, which otherwise staggers sign-in items.
#
# The rest of unattended recovery is operator-only and lives in the README
# ("Operations: unattended recovery after a power cut"): the firmware's
# power-on-after-power-loss setting and Windows auto-logon (Sysinternals
# Autologon, which stores the password as an LSA secret).
#
# The Run entry stores this checkout's absolute path: re-run this script after
# moving or renaming the checkout, along with the register-*-task scripts.

param(
    [switch]$Unregister
)

$ErrorActionPreference = "Stop"
$RunKey = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run"
$EntryName = "basis-lock-after-logon"
$Script = Join-Path $PSScriptRoot "lock-after-logon.ps1"

if ($Unregister) {
    if (Get-ItemProperty -Path $RunKey -Name $EntryName -ErrorAction SilentlyContinue) {
        Remove-ItemProperty -Path $RunKey -Name $EntryName
        Write-Host "Removed Run entry '$EntryName'."
    } else {
        Write-Host "No Run entry '$EntryName' to remove."
    }
    exit 0
}

if (-not (Test-Path $Script)) { throw "Missing $Script" }

$command = "powershell.exe -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$Script`""
Set-ItemProperty -Path $RunKey -Name $EntryName -Value $command

$serialize = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Explorer\Serialize"
New-Item -Path $serialize -Force | Out-Null
Set-ItemProperty -Path $serialize -Name StartupDelayInMSec -Value 0 -Type DWord

Write-Host "Run entry '$EntryName' -> $Script"
Write-Host "Sign-in lock log: $(Join-Path $env:LOCALAPPDATA 'basis\lock-after-logon.log')"
