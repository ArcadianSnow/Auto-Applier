"""Convenience runner for the live ATS smoke tests (spec section 11b Phase 3 (9/M)).

Wraps ``pytest -m smoke`` with sensible defaults for cron / Task Scheduler
use: writes a structured summary line at the end so log parsers can grep
for pass/fail status, exits non-zero on any failure for cron alerting.

USAGE
-----

Run interactively (debug / manual):
  python scripts/run_smoke.py

Schedule on Windows (Task Scheduler, daily at 09:00):
  schtasks /create /tn "AutoApplier_Smoke" /sc daily /st 09:00 ^
    /tr "C:\\path\\to\\python.exe C:\\path\\to\\scripts\\run_smoke.py"

Schedule on Linux / macOS (crontab, daily at 09:00):
  0 9 * * * cd /path/to/auto-applier && /usr/bin/python scripts/run_smoke.py \
    >> /var/log/autoapplier-smoke.log 2>&1

WHAT THE SMOKE SUITE DOES
-------------------------

  * Discovery: hits GH / Lever / Ashby public APIs against a small curated
    list of stable tokens (in tests_v3/test_live_smoke.py).
  * Form-load: opens one real apply form per ATS via the production
    BrowserSession stack, asserts the standard selectors our drivers
    depend on are present.
  * **NEVER submits.** Every test ends at form load or earlier.

WHEN IT FAILS
-------------

  * Read the pytest output - it names the missing selector + ATS + URL.
  * Reproduce locally: ``pytest tests_v3/test_live_smoke.py -m smoke -v``.
  * If the live HTML really changed, run ``scripts/refresh_fixtures.py
    <ats> <url>`` against a current posting, update the per-ATS driver
    code, re-run ``tests_v3/test_selector_drift.py``, commit fixture +
    driver changes together.
"""

from __future__ import annotations

import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    started = datetime.now(timezone.utc).isoformat(timespec="seconds")
    print(f"[smoke] started_at={started}")
    cmd = [
        sys.executable, "-m", "pytest",
        "tests_v3/test_live_smoke.py",
        "-m", "smoke",
        "-v",
        "--tb=short",
    ]
    proc = subprocess.run(cmd, cwd=REPO_ROOT)
    finished = datetime.now(timezone.utc).isoformat(timespec="seconds")
    status = "pass" if proc.returncode == 0 else "FAIL"
    print(f"[smoke] finished_at={finished} status={status} exit={proc.returncode}")
    return proc.returncode


if __name__ == "__main__":
    sys.exit(main())
