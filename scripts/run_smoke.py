"""Convenience runner for the live ATS smoke tests (spec section 11b Phase 3 (9/M)).

Wraps ``pytest -m smoke`` with sensible defaults for cron / Task Scheduler use: appends a
structured summary line so log parsers can grep pass/fail, tees output to a log file, and
exits non-zero on any failure so the scheduler can alert.

USAGE
-----

Run interactively (debug / manual):
  python scripts/run_smoke.py

Schedule it (Windows) — use the helper, which handles python resolution + AV3_DATA_DIR:
  pwsh ./scripts/register-smoke-task.ps1
  pwsh ./scripts/register-smoke-task.ps1 -Schedule Daily -Time 07:00
  pwsh ./scripts/register-smoke-task.ps1 -Unregister

Schedule on Linux / macOS (crontab, weekly Monday 09:00):
  0 9 * * 1 cd /path/to/auto-applier && /usr/bin/python scripts/run_smoke.py

Options:
  --log <path>   write output here as well as stdout (default: <data_dir>/smoke.log,
                 falling back to ./smoke.log if settings can't be loaded)
  --no-log       stdout only

WHAT THE SMOKE SUITE DOES
-------------------------

  * Discovery: hits GH / Lever / Ashby public APIs against a small curated list of stable
    tokens (in tests/test_live_smoke.py).
  * Form-load: opens one real apply form per ATS via the production BrowserSession stack and
    asserts the standard selectors the drivers depend on are present.
  * Custom-question discovery: runs each driver's real ``discover_custom_questions`` against a
    live form and diffs it against a ground-truth DOM probe — the layer that actually notices
    when an ATS reshuffles its form markup.
  * **NEVER submits.** Every test ends at form load or earlier.

It opens a real Chrome window (the production session stack), so prefer a weekly schedule at a
time you're not working; selector drift moves slowly enough that weekly is plenty.

WHEN IT FAILS
-------------

  * Read the pytest output — it names the missing selector + ATS + URL.
  * Reproduce locally: ``pytest tests/test_live_smoke.py -m smoke -v``.
  * If the live HTML really changed, run ``scripts/refresh_fixtures.py <ats> <url>`` against a
    current posting, update the per-ATS driver code, re-run ``pytest tests/test_selector_drift.py``
    and ``pytest -m browser``, then commit fixture + driver changes together.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
#: The suite this runner exists to run. Guarded by tests/test_scripts_paths.py — this path
#: silently rotted to the pre-rename test directory at the v3→master package rename, so the
#: runner could not have worked since. That is the worst shape for this bug: the runner IS
#: the selector-drift alarm, and a broken alarm looks exactly like 'no drift'.
SMOKE_TESTS = REPO_ROOT / "tests" / "test_live_smoke.py"


def _default_log_path() -> Path:
    """Log next to the user's data (where they'd look), falling back to the repo root."""
    try:
        from auto_applier.config import load_settings

        return Path(load_settings().data_dir) / "smoke.log"
    except Exception:  # noqa: BLE001 — a log location must never block the run
        return REPO_ROOT / "smoke.log"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the live ATS smoke tests.")
    parser.add_argument("--log", default=None, help="also append output to this file")
    parser.add_argument("--no-log", action="store_true", help="stdout only")
    args = parser.parse_args(argv)

    if not SMOKE_TESTS.exists():
        print(f"[smoke] FATAL: smoke suite not found at {SMOKE_TESTS}", file=sys.stderr)
        return 2

    started = datetime.now(timezone.utc).isoformat(timespec="seconds")
    cmd = [
        sys.executable, "-m", "pytest",
        str(SMOKE_TESTS.relative_to(REPO_ROOT)),
        "-m", "smoke",
        "-v",
        "--tb=short",
    ]
    proc = subprocess.run(cmd, cwd=REPO_ROOT, capture_output=True, text=True)
    finished = datetime.now(timezone.utc).isoformat(timespec="seconds")
    status = "pass" if proc.returncode == 0 else "FAIL"

    report = (
        f"[smoke] started_at={started}\n"
        f"{proc.stdout}{proc.stderr}"
        f"[smoke] finished_at={finished} status={status} exit={proc.returncode}\n"
    )
    print(report, end="")

    if not args.no_log:
        log_path = Path(args.log) if args.log else _default_log_path()
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with log_path.open("a", encoding="utf-8") as fh:
                fh.write(report)
            print(f"[smoke] log={log_path}")
        except OSError as exc:
            print(f"[smoke] could not write log to {log_path}: {exc}", file=sys.stderr)

    return proc.returncode


if __name__ == "__main__":
    sys.exit(main())
