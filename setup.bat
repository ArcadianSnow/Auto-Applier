@echo off
setlocal EnableDelayedExpansion
title Auto Applier - First-Time Setup

REM ===================================================================
REM Auto Applier setup launcher (Windows)
REM
REM Detects Python; if missing, tries `winget` for a silent
REM background install (no User-clicks-checkbox failure mode).
REM Falls back to opening python.org if winget isn't available.
REM Then installs project deps, Playwright, and hands off to the
REM GUI wizard for everything else (LLM, personal info, resumes,
REM preferences, answers).
REM
REM Output piped to setup.log so a Defender false-positive scroll
REM can't drown out useful errors.
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

REM ---- Resolve a Python interpreter -----------------------------------
REM
REM PY_CMD ends up holding the command we'll use the rest of the
REM script. Order:
REM   1. python on PATH (most installs)
REM   2. py launcher on PATH (Windows official installer ships it)
REM   3. winget silent-install Python 3.12, then probe known paths
REM   4. fall back to opening python.org and asking for a re-run
REM
echo  [1/4] Looking for Python 3.11 or newer...
set "PY_CMD="

where python >nul 2>nul
if not errorlevel 1 (
    set "PY_CMD=python"
)

if "!PY_CMD!"=="" (
    where py >nul 2>nul
    if not errorlevel 1 (
        set "PY_CMD=py -3"
    )
)

REM ---- If still no Python, try winget for a silent install -----------
if "!PY_CMD!"=="" (
    echo        Python isn't installed. I'll try to install it for you.
    echo.
    where winget >nul 2>nul
    if not errorlevel 1 (
        echo        Calling winget to install Python 3.12 in the background.
        echo        This can take 1-3 minutes; watch progress below:
        echo.
        echo        ----------------------------------------------------------
        REM Show winget's output live so the user sees progress instead of
        REM staring at a frozen window for 3 minutes. Tee a copy to
        REM setup.log too in case troubleshooting is needed afterward.
        winget install --id Python.Python.3.12 -e ^
            --accept-source-agreements --accept-package-agreements ^
            --override "/quiet PrependPath=1 InstallAllUsers=0"
        set "WINGET_RC=!errorlevel!"
        echo        ----------------------------------------------------------
        echo.
        if not "!WINGET_RC!"=="0" (
            color 0E
            echo        winget exited with code !WINGET_RC! — install probably failed.
            echo        Common causes:
            echo          * No internet connection ^(winget needs to reach github.com^)
            echo          * Corporate firewall / VPN blocks winget
            echo          * Windows Defender held the installer for review
            echo.
            echo        Falling back to manual install instructions below.
            echo.
        ) else (
            REM PATH in the CURRENT cmd doesn't reflect the just-installed
            REM Python — the install updated the registry, not this shell's
            REM env block. Probe the known install locations directly so we
            REM don't have to ask the user to relaunch the script.
            for %%P in (
                "%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
                "%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
                "%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
                "%ProgramFiles%\Python313\python.exe"
                "%ProgramFiles%\Python312\python.exe"
                "%ProgramFiles%\Python311\python.exe"
            ) do (
                if exist "%%~P" if "!PY_CMD!"=="" (
                    set "PY_CMD=%%~P"
                    echo        Python installed and detected at %%~P
                )
            )
            if "!PY_CMD!"=="" (
                color 0E
                echo        winget reported success but I can't find python.exe
                echo        in the usual install locations. Falling back to
                echo        manual install instructions.
                echo.
            )
        )
    ) else (
        color 0E
        echo        winget is not available on this Windows version.
        echo        ^(winget ships with Windows 10 1909+ and all Windows 11.
        echo         You may have a much older version — falling back to
        echo         manual install.^)
        echo.
    )
)

if not "!PY_CMD!"=="" goto :have_python

:have_python

REM ---- If still no Python, manual fallback ----------------------------
if "!PY_CMD!"=="" (
    echo.
    color 0C
    echo   Python could not be installed automatically.
    echo.
    echo   Opening the Python download page in your browser. When the
    echo   installer runs, MAKE SURE to check the box that says
    echo   "Add python.exe to PATH" before clicking Install.
    echo.
    echo   After it finishes, double-click setup.bat again.
    echo.
    start "" "https://www.python.org/downloads/"
    pause
    exit /b 1
)

REM ---- Validate version ----------------------------------------------
for /f "tokens=2" %%v in ('!PY_CMD! --version 2^>^&1') do set PYVER=%%v
echo        Using Python !PYVER!.

!PY_CMD! -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>nul
if errorlevel 1 (
    echo.
    color 0C
    echo   ERROR: Python !PYVER! is too old. Need 3.11 or newer.
    echo.
    echo   Please uninstall the old Python and install 3.11+ from python.org,
    echo   then double-click setup.bat again.
    echo.
    start "" "https://www.python.org/downloads/"
    pause
    exit /b 1
)

REM ---- Step 2: Project deps -------------------------------------------
echo.
echo  [2/4] Installing Auto Applier dependencies...
echo        ^(detailed output in setup.log^)
!PY_CMD! -m pip install --upgrade pip >> setup.log 2>&1
!PY_CMD! -m pip install -e . >> setup.log 2>&1
if errorlevel 1 (
    echo.
    color 0C
    echo   ERROR: pip install failed. See setup.log for details.
    echo.
    echo   Most common cause: no internet connection, or your antivirus
    echo   is blocking pip. Check both, then re-run setup.bat.
    echo.
    pause
    exit /b 1
)
echo        Done.

REM ---- Step 3: Playwright browser engine ------------------------------
echo.
echo  [3/4] Installing the browser engine ^(~150 MB download^)...
echo        ^(detailed output in setup.log^)
!PY_CMD! -m playwright install chromium >> setup.log 2>&1
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

REM ---- Record the version we just installed --------------------------
REM update.bat compares this against the latest GitHub commit SHA
REM to decide whether an update is available. Best-effort — if the
REM API call fails (offline first-run, etc.) we just skip writing
REM .version and update.bat will treat the install as "version
REM unknown" and offer the next refresh.
powershell -NoProfile -Command ^
    "try { (Invoke-RestMethod -UseBasicParsing 'https://api.github.com/repos/ArcadianSnow/Auto-Applier/commits/master').sha | Out-File -FilePath '.version' -Encoding ASCII -NoNewline } catch {}" 2>nul

REM ---- Step 4: Hand off to the GUI wizard -----------------------------
echo.
echo  [4/4] Launching the setup wizard...
echo.
echo        From here on, everything happens in a window — close this
echo        black box once the wizard window opens.
echo.

REM Prefer pythonw (no console) for a clean handoff. If we resolved
REM Python via an absolute path, derive pythonw.exe from it.
set "PYW_CMD="
if "!PY_CMD!"=="python" (
    where pythonw >nul 2>nul
    if not errorlevel 1 set "PYW_CMD=pythonw"
) else if "!PY_CMD!"=="py -3" (
    where pythonw >nul 2>nul
    if not errorlevel 1 set "PYW_CMD=pythonw"
) else (
    REM PY_CMD is an absolute path to python.exe — swap to pythonw.exe.
    set "PYW_CMD=!PY_CMD:python.exe=pythonw.exe!"
    if not exist "!PYW_CMD!" set "PYW_CMD="
)

if not "!PYW_CMD!"=="" (
    start "" "!PYW_CMD!" -m auto_applier
) else (
    start "" !PY_CMD! -m auto_applier
)

REM Give the wizard a moment to claim the screen before this window closes.
ping 127.0.0.1 -n 3 >nul
exit /b 0
