@echo off
title Auto Applier

REM ===================================================================
REM Daily-use launcher (Windows)
REM
REM Just opens the GUI. Assumes setup.bat has already been run once.
REM If Python somehow disappeared since setup, falls back to a clear
REM error instead of a confusing "command not found" flash.
REM ===================================================================

where pythonw >nul 2>nul
if errorlevel 1 (
    where python >nul 2>nul
    if errorlevel 1 (
        color 0C
        echo.
        echo   ERROR: Python is not installed.
        echo.
        echo   It looks like Python was uninstalled since you last set
        echo   up Auto Applier. Run setup.bat to fix it.
        echo.
        pause
        exit /b 1
    )
    REM No pythonw, but python works — fall through with python.
    start "" python -m auto_applier
    exit /b 0
)

REM pythonw runs without a console window — cleaner for daily use.
start "" pythonw -m auto_applier
exit /b 0
