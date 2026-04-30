@echo off
setlocal EnableDelayedExpansion
title Auto Applier - Update

REM ===================================================================
REM One-click updater for Auto Applier (Windows)
REM
REM No git required. No PowerShell knowledge required. Friend just
REM double-clicks update.bat — same UX as setup.bat / run.bat.
REM
REM Steps:
REM   1. Ask GitHub for the latest commit SHA on master.
REM   2. Compare against the local .version marker (written when
REM      setup.bat or update.bat last ran).
REM   3. If different, download the latest source zip from
REM      GitHub, extract, copy code files into place — but never
REM      touch data\ or .env.
REM   4. Re-run pip install -e . to pick up any new dependencies.
REM   5. Save the new SHA into .version.
REM
REM Uses Windows-built-in PowerShell under the hood; the user never
REM sees a PowerShell prompt. All output stays in the cmd window.
REM ===================================================================

color 0B

echo.
echo  ============================================================
echo    Auto Applier - Update
echo  ============================================================
echo.

REM ---- Step 1: ask GitHub for the current master commit SHA ---------
echo  [1/5] Checking for updates...
powershell -NoProfile -Command ^
    "try { (Invoke-RestMethod -UseBasicParsing 'https://api.github.com/repos/ArcadianSnow/Auto-Applier/commits/master').sha } catch { exit 1 }" ^
    > "%TEMP%\aa_remote_sha.txt" 2>nul
if errorlevel 1 (
    color 0C
    echo        Could not reach GitHub. Check your internet, then
    echo        try update.bat again.
    pause
    exit /b 1
)
set /p REMOTE_SHA=<"%TEMP%\aa_remote_sha.txt"
del "%TEMP%\aa_remote_sha.txt" >nul 2>nul

if "!REMOTE_SHA!"=="" (
    color 0C
    echo        GitHub returned an empty response. Try again later.
    pause
    exit /b 1
)

REM ---- Step 2: compare with the locally-stored SHA ------------------
set "LOCAL_SHA="
if exist ".version" (
    set /p LOCAL_SHA=<.version
)

if "!LOCAL_SHA!"=="!REMOTE_SHA!" (
    color 0A
    echo        Already up to date ^(version !REMOTE_SHA:~0,7!^).
    echo.
    pause
    exit /b 0
)

echo        Update available.
echo          your version: !LOCAL_SHA:~0,7!
echo          latest:       !REMOTE_SHA:~0,7!
echo.

REM ---- Step 3: confirm ----------------------------------------------
set /p CONFIRM="  Download and apply this update? [Y/n]: "
if /i "!CONFIRM!"=="n" (
    echo.
    echo  Update cancelled. Run update.bat again whenever you're ready.
    pause
    exit /b 0
)

REM ---- Step 4: download + extract -----------------------------------
echo.
echo  [2/5] Downloading the latest version ^(~5 MB^)...
powershell -NoProfile -Command ^
    "try { Invoke-WebRequest -UseBasicParsing 'https://github.com/ArcadianSnow/Auto-Applier/archive/refs/heads/master.zip' -OutFile '%TEMP%\aa_update.zip' } catch { exit 1 }"
if errorlevel 1 (
    color 0C
    echo        Download failed. Check your internet connection.
    pause
    exit /b 1
)
echo        Done.

echo.
echo  [3/5] Extracting...
if exist "%TEMP%\aa_update_extracted" rmdir /S /Q "%TEMP%\aa_update_extracted" >nul 2>nul
powershell -NoProfile -Command ^
    "try { Expand-Archive -Force -Path '%TEMP%\aa_update.zip' -DestinationPath '%TEMP%\aa_update_extracted' } catch { exit 1 }"
if errorlevel 1 (
    color 0C
    echo        Extract failed. The download may be corrupted.
    pause
    exit /b 1
)

set "SRC=%TEMP%\aa_update_extracted\Auto-Applier-master"
if not exist "!SRC!" (
    color 0C
    echo        Extract layout unexpected — looking for: !SRC!
    echo        Try update.bat again.
    pause
    exit /b 1
)
echo        Done.

REM ---- Step 5: copy code files (NEVER touch data\ or .env) ----------
echo.
echo  [4/5] Applying update ^(your data\, .env, and resumes are NOT touched^)...

REM xcopy flags:
REM   /E  copy subdirectories including empty ones
REM   /Y  overwrite without prompting
REM   /Q  quiet (no per-file output)
REM   /I  if dest doesn't exist + multiple files, treat dest as folder
xcopy "!SRC!\auto_applier" "auto_applier" /E /Y /Q /I >nul
xcopy "!SRC!\tests" "tests" /E /Y /Q /I >nul
xcopy "!SRC!\scripts" "scripts" /E /Y /Q /I >nul

REM Top-level files we always overwrite
copy /Y "!SRC!\setup.bat" "setup.bat" >nul 2>nul
copy /Y "!SRC!\run.bat" "run.bat" >nul 2>nul
copy /Y "!SRC!\update.bat" "update.bat" >nul 2>nul
copy /Y "!SRC!\run.py" "run.py" >nul 2>nul
copy /Y "!SRC!\build.py" "build.py" >nul 2>nul
copy /Y "!SRC!\README.md" "README.md" >nul 2>nul
copy /Y "!SRC!\CLAUDE.md" "CLAUDE.md" >nul 2>nul
copy /Y "!SRC!\pyproject.toml" "pyproject.toml" >nul 2>nul
copy /Y "!SRC!\.gitignore" ".gitignore" >nul 2>nul

REM .env.example is a template — overwrite it (it's safe; user's
REM real secrets live in .env which we never touch).
copy /Y "!SRC!\.env.example" ".env.example" >nul 2>nul

echo        Code refreshed.

REM ---- Step 6: refresh Python dependencies --------------------------
echo.
echo  [5/5] Refreshing dependencies...

REM Re-resolve Python the same way setup.bat does.
set "PY_CMD="
where python >nul 2>nul
if not errorlevel 1 (
    set "PY_CMD=python"
) else (
    for %%P in (
        "%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
        "%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
        "%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
        "%ProgramFiles%\Python313\python.exe"
        "%ProgramFiles%\Python312\python.exe"
        "%ProgramFiles%\Python311\python.exe"
    ) do (
        if exist "%%~P" if "!PY_CMD!"=="" set "PY_CMD=%%~P"
    )
)
if "!PY_CMD!"=="" (
    color 0E
    echo        Python isn't on PATH and isn't where setup.bat usually
    echo        puts it. Code is already updated; run setup.bat once
    echo        to repair the Python install + dependencies.
) else (
    "!PY_CMD!" -m pip install -e . > update.log 2>&1
    if errorlevel 1 (
        color 0E
        echo        Dependency refresh hit an error. See update.log.
        echo        Code is updated; if something misbehaves, run
        echo        setup.bat to repair.
    ) else (
        echo        Done.
    )
)

REM ---- Save the new SHA so the next check can compare ---------------
> .version echo !REMOTE_SHA!

REM ---- Cleanup -------------------------------------------------------
del "%TEMP%\aa_update.zip" >nul 2>nul
rmdir /S /Q "%TEMP%\aa_update_extracted" >nul 2>nul

color 0A
echo.
echo  ============================================================
echo    Update complete ^(version !REMOTE_SHA:~0,7!^).
echo    Run run.bat to launch the new version.
echo  ============================================================
echo.
pause
exit /b 0
