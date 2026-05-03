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

    # Silence DEBUG noise from third-party libraries that flood
    # the log without diagnostic value:
    #
    # - pdfminer: re-parses every PDF and dumps xref tables, font
    #   stream descriptors, etc. on every read. ~500 lines per
    #   resume parse.
    # - httpcore / httpx / hpack / h11 / urllib3: per-request
    #   socket lifecycle DEBUG (~12 lines per LLM call). With 50+
    #   LLM calls per run that's 600+ noise lines.
    # - asyncio: proactor pump events.
    # - PIL / pdfplumber internals: pure noise.
    #
    # Live-run logs from 2026-05-02 hit 4-7 MB / 49K+ lines for
    # ~30 minutes of activity; the friend reported it as bloated
    # and hard to scan. With these silenced, expected size drops
    # to ~500 KB / 5K lines for the same run — every line is then
    # actually relevant.
    #
    # Override via the AUTO_APPLIER_VERBOSE_LOGS env var if you
    # ever need the raw firehose for httpcore/pdfminer debugging.
    import os as _os
    _verbose = bool(_os.environ.get("AUTO_APPLIER_VERBOSE_LOGS", "").strip())
    if not _verbose:
        for noisy in (
            "pdfminer", "pdfminer.psparser", "pdfminer.pdfdocument",
            "pdfminer.pdfinterp", "pdfminer.pdfparser",
            "pdfminer.cmapdb",
            "pdfplumber",
            "httpcore", "httpcore.connection", "httpcore.http11",
            "httpx",
            "hpack", "h11",
            "urllib3", "urllib3.connectionpool",
            "asyncio",
            "PIL", "PIL.PngImagePlugin",
        ):
            logging.getLogger(noisy).setLevel(logging.WARNING)

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

    Output format prefers a human-readable date-based identifier
    over the bare 7-char sha — friend reading "Run version:
    2026.05.03-2 (e207a66)" instantly knows when the build is
    from. Sha kept in parens for git-precise diagnosis.

    Resolution order:

    1. **Git checkout** — ``git rev-parse --short HEAD`` for the
       sha, ``git log --pretty=format:%cs`` filtered by today's
       date for the count of commits today (the ``-N``). Output:
       ``YYYY.MM.DD-N (sha)`` or ``YYYY.MM.DD-N (sha)-dirty`` when
       the working tree has uncommitted changes.

    2. **VERSION file** at ``PROJECT_ROOT`` — for distributions
       built without a ``.git`` directory (zip installs, PyInstaller
       bundles). The ``scripts/write_version.py`` script writes
       this file before packaging. If the file exists, return its
       contents verbatim — assume the writer formatted it correctly.

    3. ``"(unknown)"`` — when neither is available. Sam's zip
       install was hitting this prior to 2026-05-03.
    """
    import subprocess
    from auto_applier.config import PROJECT_ROOT
    # Try git first — works on dev machines and any user who
    # cloned via git rather than zip-downloaded.
    try:
        sha = subprocess.run(
            ["git", "-C", str(PROJECT_ROOT), "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=2,
        )
        if sha.returncode == 0 and sha.stdout.strip():
            sha_short = sha.stdout.strip()
            # Compute the date-based component from today's commit count.
            # `--date=short` gives YYYY-MM-DD; we filter for that
            # date to get N (1-based: first commit today is -1).
            today_iso = datetime.now().strftime("%Y-%m-%d")
            today_dot = today_iso.replace("-", ".")
            commits_today = subprocess.run(
                ["git", "-C", str(PROJECT_ROOT), "log",
                 "--pretty=format:%cs",
                 f"--since={today_iso} 00:00:00",
                 f"--until={today_iso} 23:59:59"],
                capture_output=True, text=True, timeout=2,
            )
            count_today = 0
            if commits_today.returncode == 0:
                count_today = sum(
                    1 for ln in commits_today.stdout.splitlines()
                    if ln.strip() == today_iso
                )
            # Position of HEAD among today's commits — ``-1`` is
            # the most recent. If HEAD isn't from today, the count
            # is 0 and we omit the -N suffix (HEAD's date is then
            # appended instead).
            ver_human: str
            if count_today > 0:
                ver_human = f"{today_dot}-{count_today}"
            else:
                # HEAD is older than today — fetch its commit date.
                head_date = subprocess.run(
                    ["git", "-C", str(PROJECT_ROOT), "log", "-1",
                     "--pretty=format:%cs"],
                    capture_output=True, text=True, timeout=2,
                )
                d = head_date.stdout.strip() if head_date.returncode == 0 else ""
                ver_human = d.replace("-", ".") if d else "older"
            ver = f"{ver_human} ({sha_short})"
            # -dirty suffix on uncommitted local edits.
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
    # Fallback for non-git installs (zip download from GitHub
    # update.bat path): read the VERSION file written at packaging
    # time. Sam's install was here pre-2026-05-03, returning
    # "(unknown)" because no VERSION file was being shipped.
    try:
        version_file = PROJECT_ROOT / "VERSION"
        if version_file.exists():
            content = version_file.read_text(encoding="utf-8").strip()
            if content:
                return content
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
