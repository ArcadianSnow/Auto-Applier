@echo off
setlocal EnableDelayedExpansion
title Auto Applier - First-Time Setup

REM ===================================================================
REM Auto Applier setup launcher (Windows)
REM
REM Detects Python, installs project deps, installs the Playwright
REM browser engine, then hands off to the GUI wizard for everything
REM else (LLM setup, personal info, resumes, preferences, answers).
REM
REM Output piped to setup.log so a Defender false-positive scroll
REM can't drown out useful errors. The user sees a tidy summary.
REM ===================================================================

color 0B

echo.
echo  ============================================================
echo    Auto Applier - First-Time Setup
echo  ============================================================
echo.
echo   This will install everything Auto Applier needs to run.
echo   It only needs to run once. Subsequent launches use run.bat.
echo.

REM ---- Step 1: Python detection ---------------------------------------
echo  [1/4] Checking for Python 3.11+...
where python >nul 2>nul
if errorlevel 1 (
    echo.
    color 0C
    echo   ERROR: Python is not installed or not on your PATH.
    echo.
    echo   Auto Applier needs Python 3.11 or newer. The Microsoft
    echo   Store version works, or download the official installer.
    echo.
    echo   Opening the Python download page in your browser now.
    echo   When the installer runs, MAKE SURE to check the box
    echo   that says "Add Python to PATH" — without it the next
    echo   run of setup.bat won't find Python.
    echo.
    echo   After Python is installed, double-click setup.bat again.
    echo.
    start "" "https://www.python.org/downloads/"
    pause
    exit /b 1
)

for /f "tokens=2" %%v in ('python --version 2^>^&1') do set PYVER=%%v
echo        Found Python !PYVER!.

REM Verify >= 3.11. python -c exits non-zero if check fails.
python -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>nul
if errorlevel 1 (
    echo.
    color 0C
    echo   ERROR: Python !PYVER! is too old. Need 3.11 or newer.
    echo.
    echo   Please install Python 3.11+ from python.org.
    echo.
    start "" "https://www.python.org/downloads/"
    pause
    exit /b 1
)

REM ---- Step 2: Project deps -------------------------------------------
echo.
echo  [2/4] Installing Auto Applier dependencies...
echo        ^(detailed output in setup.log^)
python -m pip install --upgrade pip > setup.log 2>&1
python -m pip install -e . >> setup.log 2>&1
if errorlevel 1 (
    echo.
    color 0C
    echo   ERROR: pip install failed. See setup.log for details.
    echo.
    echo   Most common cause: no internet connection, or your
    echo   antivirus is blocking pip. Check both, then re-run
    echo   setup.bat.
    echo.
    pause
    exit /b 1
)
echo        Done.

REM ---- Step 3: Playwright browser engine ------------------------------
echo.
echo  [3/4] Installing the browser engine ^(~150 MB download^)...
echo        ^(detailed output in setup.log^)
python -m playwright install chromium >> setup.log 2>&1
if errorlevel 1 (
    echo.
    color 0E
    echo   WARNING: playwright browser install reported an error.
    echo   The wizard will tell you if it actually matters.
    echo   See setup.log for details.
    echo.
) else (
    echo        Done.
)

REM ---- Step 4: Hand off to the GUI wizard -----------------------------
echo.
echo  [4/4] Launching the setup wizard...
echo.
echo        From here on, everything happens in a window — close
echo        this black box once the wizard window opens.
echo.

start "" pythonw -m auto_applier
REM ^ pythonw (no console window) so the wizard owns the screen.
REM   If pythonw isn't available (rare), fall back to python.
if errorlevel 1 (
    start "" python -m auto_applier
)

REM Give the wizard a moment to claim the screen before this window closes.
ping 127.0.0.1 -n 3 >nul
exit /b 0
