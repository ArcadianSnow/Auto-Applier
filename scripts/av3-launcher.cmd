@echo off
REM Auto Applier v3 — one-click launcher (Phase 4 (6/M), spec section 11a).
REM
REM Double-click target for non-technical users:
REM   1. Walks up to find the repo's .venv (preferred) or any .venv on PATH.
REM   2. Runs `av3 launch` which spawns the server and opens the browser.
REM   3. Keeps this window open so the user can see logs / close to stop.
REM
REM Power users keep running `av3 serve` directly. This script just wraps
REM that for the "double-click and walk away" workflow.

setlocal

REM Resolve the directory holding this script so the venv lookup is
REM relative to the repo root, not the user's pwd (matters when the
REM script lives in a shortcut on the desktop).
set "SCRIPT_DIR=%~dp0"
set "REPO_ROOT=%SCRIPT_DIR%.."

REM Prefer the repo's own .venv if present (e.g. after `python -m venv .venv`
REM in the repo root). Falls through to whatever `python` is on PATH if
REM there is no venv yet.
set "VENV_PYTHON=%REPO_ROOT%\.venv\Scripts\python.exe"
if exist "%VENV_PYTHON%" (
  set "PYTHON=%VENV_PYTHON%"
) else (
  set "PYTHON=python"
)

echo Starting Auto Applier v3 ...
echo (Close this window to stop the server.)
echo.

REM `av3 launch` does the rest: spawns serve, port-probes, opens browser.
"%PYTHON%" -m auto_applier.cli.main launch

REM Pause so the user sees any error output before the window closes.
echo.
echo (Server stopped. Press a key to close this window.)
pause >nul

endlocal
