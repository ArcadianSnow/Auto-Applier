<#
.SYNOPSIS
  Register (or remove) a Windows Scheduled Task that runs `av3 discover` on a daily schedule.

.DESCRIPTION
  Auto Applier finds + scores new jobs continuously while `av3 run` / `av3 launch` is running.
  If you DON'T keep that process alive 24/7, this helper sets up a lightweight daily sweep so the
  job list stays fresh without leaving a terminal open. It only runs DISCOVERY (read-only gather —
  it never submits an application), so it's safe to schedule unattended.

  Pairs with the refresh-cadence guidance in the README: discovery daily is plenty (ATS boards
  post a few times a day at most); re-run `av3 seed-boards` yourself when your targeting changes.

.PARAMETER Time
  Local time of day to run, "HH:mm" (default 08:00).

.PARAMETER DataDir
  Optional AV3_DATA_DIR for the task (use this if your data lives outside the default location).

.PARAMETER TaskName
  Scheduled Task name (default "AutoApplierDiscovery").

.PARAMETER Unregister
  Remove the task instead of creating it.

.EXAMPLE
  pwsh ./scripts/register-discovery-task.ps1 -Time 07:30
  pwsh ./scripts/register-discovery-task.ps1 -DataDir C:\Users\me\JobSearch\av3data
  pwsh ./scripts/register-discovery-task.ps1 -Unregister
#>
[CmdletBinding()]
param(
    [string]$Time = "08:00",
    [string]$DataDir = "",
    [string]$TaskName = "AutoApplierDiscovery",
    [switch]$Unregister
)

$ErrorActionPreference = "Stop"

if ($Unregister) {
    try {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Host "Removed scheduled task '$TaskName'."
    }
    catch {
        Write-Host "No task named '$TaskName' to remove (or removal failed): $($_.Exception.Message)"
    }
    return
}

# Resolve repo root + prefer the repo's .venv python (matches av3-launcher.cmd).
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot = Resolve-Path (Join-Path $ScriptDir "..")
$VenvPython = Join-Path $RepoRoot ".venv\Scripts\python.exe"
$Python = if (Test-Path $VenvPython) { $VenvPython } else { "python" }

# Build the command the task runs. We invoke through powershell so we can set AV3_DATA_DIR
# (scheduled-task actions can't carry env vars directly). discover is the module entry point
# (`av3` console-script equivalent) — gather only, never an apply.
$envPrefix = if ($DataDir) { "`$env:AV3_DATA_DIR = '$DataDir'; " } else { "" }
$inner = "$envPrefix& '$Python' -m auto_applier.cli.main discover"
$argument = "-NoProfile -WindowStyle Hidden -Command `"$inner`""

$action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $argument -WorkingDirectory $RepoRoot
$trigger = New-ScheduledTaskTrigger -Daily -At $Time
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -DontStopOnIdleEnd `
    -ExecutionTimeLimit (New-TimeSpan -Hours 1)

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings `
    -Description "Auto Applier: daily job discovery sweep (gather only, never applies)." -Force | Out-Null

Write-Host "Registered scheduled task '$TaskName' — runs 'av3 discover' daily at $Time."
if ($DataDir) { Write-Host "  AV3_DATA_DIR = $DataDir" }
Write-Host "  Python: $Python"
Write-Host "Remove it later with:  pwsh ./scripts/register-discovery-task.ps1 -Unregister"
