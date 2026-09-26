# Start the AI Company dashboard automatically when you log in to Windows.
#
#   powershell -ExecutionPolicy Bypass -File scripts\install_autostart.ps1            # install
#   powershell -ExecutionPolicy Bypass -File scripts\install_autostart.ps1 -Remove    # uninstall
#
# Creates a per-user Scheduled Task "AI Company Dashboard" (no admin rights needed) that
# runs `pythonw run_dashboard.py` hidden in this folder and restarts it if it crashes.
# The work loop still starts PAUSED: press Start in the dashboard when you want it to work.

param([switch]$Remove, [string]$HostAddress = "0.0.0.0", [int]$Port = 8765)

$ErrorActionPreference = "Stop"
$name = "AI Company Dashboard"
$root = Split-Path -Parent $PSScriptRoot

if ($Remove) {
    Unregister-ScheduledTask -TaskName $name -Confirm:$false -ErrorAction SilentlyContinue
    Write-Host "Removed scheduled task '$name'."
    exit 0
}

$python = (Get-Command python -ErrorAction Stop).Source
$pythonw = Join-Path (Split-Path $python) "pythonw.exe"
if (-not (Test-Path $pythonw)) { $pythonw = $python }
if (-not (Test-Path (Join-Path $root "data\dashboard_auth.json"))) {
    throw "Set a dashboard password first:  python run_dashboard.py --set-password"
}

$action = New-ScheduledTaskAction -Execute $pythonw `
    -Argument "run_dashboard.py --host $HostAddress --port $Port" -WorkingDirectory $root
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet -RestartCount 5 -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -StartWhenAvailable -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName $name -Action $action -Trigger $trigger -Settings $settings `
    -Description "AI Company dashboard + work loop ($root)" -Force | Out-Null
Write-Host "Installed '$name': starts at logon on port $Port. Start it now with:"
Write-Host "  Start-ScheduledTask -TaskName '$name'"
