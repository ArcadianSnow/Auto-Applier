<#
.SYNOPSIS
  Register (or remove) a Windows Scheduled Task that runs the live ATS smoke tests.

.DESCRIPTION
  ATS vendors reshuffle their apply-form markup without notice, and when they do the bot stops
  finding fields — silently, because a form that renders fine still "works". Selector drift was
  the #1 v2 bug source. The smoke suite (tests/test_live_smoke.py) catches it by driving real
  postings: it checks the public APIs still return jobs, the standard selectors still resolve,
  and each driver's real custom-question discovery still finds what the page actually renders.

  It is GATHER work — read-only, and it NEVER submits an application — so it's safe unattended.

  WEEKLY by default, on purpose: the run opens a real Chrome window (the production browser
  stack), so a daily task would interrupt you for no benefit — drift moves far slower than that.
  Use -Schedule Daily if you'd rather know sooner.

  A failure is a signal to look, not an emergency: read the log (it names the ATS, the URL, and
  the selector), then follow the "WHEN IT FAILS" steps in scripts/run_smoke.py.

.PARAMETER Schedule
  "Weekly" (default) or "Daily".

.PARAMETER DayOfWeek
  Day for a weekly schedule (default Monday). Ignored when -Schedule Daily.

.PARAMETER Time
  Local time of day to run, "HH:mm" (default 09:00).

.PARAMETER DataDir
  Optional AV3_DATA_DIR for the task. Also decides where smoke.log lands.

.PARAMETER TaskName
  Scheduled Task name (default "AutoApplierSmoke").

.PARAMETER Unregister
  Remove the task instead of creating it.

.EXAMPLE
  pwsh ./scripts/register-smoke-task.ps1
  pwsh ./scripts/register-smoke-task.ps1 -Schedule Daily -Time 07:00
  pwsh ./scripts/register-smoke-task.ps1 -DataDir C:\Users\me\JobSearch\av3data
  pwsh ./scripts/register-smoke-task.ps1 -Unregister
#>
[CmdletBinding()]
param(
    [ValidateSet("Weekly", "Daily")]
    [string]$Schedule = "Weekly",
    [ValidateSet("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")]
    [string]$DayOfWeek = "Monday",
    [string]$Time = "09:00",
    [string]$DataDir = "",
    [string]$TaskName = "AutoApplierSmoke",
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

# Resolve repo root + prefer the repo's .venv python (matches register-discovery-task.ps1).
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot = Resolve-Path (Join-Path $ScriptDir "..")
$VenvPython = Join-Path $RepoRoot ".venv\Scripts\python.exe"
$Python = if (Test-Path $VenvPython) { $VenvPython } else { "python" }

$Runner = Join-Path $RepoRoot "scripts\run_smoke.py"
if (-not (Test-Path $Runner)) { throw "runner not found at $Runner" }

# Invoke through powershell so we can set AV3_DATA_DIR (scheduled-task actions can't carry env
# vars directly). run_smoke.py appends to <data_dir>/smoke.log and exits non-zero on failure,
# so Task Scheduler's "Last Run Result" reflects drift.
$envPrefix = if ($DataDir) { "`$env:AV3_DATA_DIR = '$DataDir'; " } else { "" }
$inner = "$envPrefix& '$Python' '$Runner'"
$argument = "-NoProfile -WindowStyle Hidden -Command `"$inner`""

$action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $argument -WorkingDirectory $RepoRoot
$trigger = if ($Schedule -eq "Daily") {
    New-ScheduledTaskTrigger -Daily -At $Time
} else {
    New-ScheduledTaskTrigger -Weekly -DaysOfWeek $DayOfWeek -At $Time
}
# StartWhenAvailable so a missed run (laptop asleep) still happens; the browser makes this
# slower than discovery, hence the longer ceiling.
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -DontStopOnIdleEnd `
    -ExecutionTimeLimit (New-TimeSpan -Hours 1)

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings `
    -Description "Auto Applier: live ATS smoke tests (selector-drift guard; read-only, never applies)." `
    -Force | Out-Null

$when = if ($Schedule -eq "Daily") { "daily at $Time" } else { "every $DayOfWeek at $Time" }
Write-Host "Registered scheduled task '$TaskName' - runs the live smoke suite $when."
if ($DataDir) { Write-Host "  AV3_DATA_DIR = $DataDir" }
Write-Host "  Python: $Python"
Write-Host "  Log:    <data_dir>\smoke.log  (exit code is non-zero on drift)"
Write-Host "It opens a real Chrome window and NEVER submits an application."
Write-Host "Remove it later with:  pwsh ./scripts/register-smoke-task.ps1 -Unregister"
