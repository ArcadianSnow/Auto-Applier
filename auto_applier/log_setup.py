"""File-based logging for runs.

The Tk activity log is readable but limited — it only shows events
the pipeline explicitly emits, and everything is formatted as short
human strings. When something goes wrong mid-form-fill, the user
needs a full audit trail: which field was detected, which priority
layer answered it, what value was returned, whether the fill
succeeded, how long each step took.

This module sets up a rotating timestamped log file at DEBUG level
under ``data/logs/run-YYYYMMDD-HHMMSS.log`` every time
:func:`start_run_logging` is called. The file captures everything
that any module emits via the standard ``logging`` module — form
filler, platform adapters, LLM router, scoring, orchestrator —
without any code changes in those modules.

Default setup (no file handler) keeps DEBUG out of stdout so the
CLI stays readable; the file handler is DEBUG and the console
handler is INFO. Callers can flip this via the ``debug_console``
argument for headless debugging.
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

from auto_applier.config import LOGS_DIR


_FILE_HANDLER: logging.Handler | None = None
_CURRENT_LOG_PATH: Path | None = None


def start_run_logging(debug_console: bool = False) -> Path:
    """Attach a timestamped DEBUG-level log file to the root logger.

    Returns the path of the log file so callers can surface it to
    the user (the dashboard logs the path so users know where to
    look when something breaks).

    Calling this multiple times within the same process rotates to
    a new file — useful when the dashboard is used to run multiple
    sessions without restarting.
    """
    global _FILE_HANDLER, _CURRENT_LOG_PATH

    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    log_path = LOGS_DIR / f"run-{timestamp}.log"

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)

    # Remove any previous file handler we installed — don't touch
    # handlers other code (pytest, user scripts) installed.
    if _FILE_HANDLER is not None:
        root.removeHandler(_FILE_HANDLER)
        try:
            _FILE_HANDLER.close()
        except Exception:
            pass

    fh = logging.FileHandler(log_path, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)-5s %(name)-40s %(message)s",
        datefmt="%H:%M:%S",
    ))
    root.addHandler(fh)
    _FILE_HANDLER = fh
    _CURRENT_LOG_PATH = log_path

    # Ensure a console handler exists too so tests and CLI runs
    # see meaningful output. If one is already attached, leave it
    # alone — the user or pytest may have configured it.
    has_stream = any(
        isinstance(h, logging.StreamHandler)
        and not isinstance(h, logging.FileHandler)
        for h in root.handlers
    )
    if not has_stream:
        sh = logging.StreamHandler()
        sh.setLevel(logging.DEBUG if debug_console else logging.INFO)
        sh.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
        root.addHandler(sh)

    log = logging.getLogger(__name__)
    log.info("Run logging enabled. Debug output: %s", log_path)
    # Stamp the build identity so audits know which code produced
    # the log — friend's logs were ambiguous about whether they
    # included recent fixes. Tries (in order) git commit hash from
    # the source checkout, then a packaged VERSION file. Falls
    # back to "(unknown)" so a missing git binary never breaks
    # logging.
    log.info("Run version: %s", _detect_version())
    return log_path


def _detect_version() -> str:
    """Best-effort build identity for log stamping.

    1. ``git rev-parse --short HEAD`` from PROJECT_ROOT — most
       precise; works when the user is running from a git checkout.
    2. ``VERSION`` text file at PROJECT_ROOT — for distributions
       built without a git directory (PyInstaller bundles, zip
       installs).
    3. ``"(unknown)"`` if neither is available.

    Output also includes a ``-dirty`` suffix when the working tree
    has uncommitted changes. Diagnoses two real-world cases:
    "did the friend update?" (commit drift) and "did they edit
    something locally?" (dirty flag).
    """
    import subprocess
    from auto_applier.config import PROJECT_ROOT
    try:
        sha = subprocess.run(
            ["git", "-C", str(PROJECT_ROOT), "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=2,
        )
        if sha.returncode == 0 and sha.stdout.strip():
            ver = sha.stdout.strip()
            # Check for uncommitted changes — fast porcelain query.
            dirty = subprocess.run(
                ["git", "-C", str(PROJECT_ROOT), "status",
                 "--porcelain", "--untracked-files=no"],
                capture_output=True, text=True, timeout=2,
            )
            if dirty.returncode == 0 and dirty.stdout.strip():
                ver += "-dirty"
            return ver
    except Exception:
        pass
    try:
        version_file = PROJECT_ROOT / "VERSION"
        if version_file.exists():
            return version_file.read_text(encoding="utf-8").strip()
    except Exception:
        pass
    return "(unknown)"


def current_log_path() -> Path | None:
    """Return the path of the active run log, if any."""
    return _CURRENT_LOG_PATH


def stop_run_logging() -> None:
    """Detach the file handler and close it cleanly."""
    global _FILE_HANDLER, _CURRENT_LOG_PATH
    if _FILE_HANDLER is not None:
        logging.getLogger().removeHandler(_FILE_HANDLER)
        try:
            _FILE_HANDLER.close()
        except Exception:
            pass
        _FILE_HANDLER = None
        _CURRENT_LOG_PATH = None
