"""Refresh the per-ATS HTML fixtures in ``tests_v3/fixtures/`` (spec section 10 + 11b).

Manual-run helper to capture real apply-form HTML from a live ATS posting and
overwrite the matching ``tests_v3/fixtures/<ats>/apply_form.html`` fixture so
the selector-drift test (``tests_v3/test_selector_drift.py``) is checking
*current* selectors, not a stale snapshot from the day the test landed.

Usage:
  python scripts/refresh_fixtures.py greenhouse \
      "https://job-boards.greenhouse.io/anthropic/jobs/4429834008"
  python scripts/refresh_fixtures.py lever \
      "https://jobs.lever.co/matchgroup/abc-123/apply"
  python scripts/refresh_fixtures.py ashby \
      "https://jobs.ashbyhq.com/ramp/abc-uuid/application"

The script:
  1. Opens the headed Chrome via the same ``BrowserSession`` stack the apply
     drivers use (so anti-detect doesn't kick the URL to a login wall).
  2. Lets the page settle, then dumps ``page.content()`` to the fixture file.
  3. Prints a diff summary so the operator can sanity-check what changed.

When to re-run:
  * After ``av3 run`` produces a sudden spike in apply errors on a specific source.
  * When ``test_selector_drift.py`` fails on a fixture you know is stale.
  * Periodically (every few months) as preventative maintenance.

What this script does NOT do:
  * Submit the form.
  * Capture multiple postings per ATS — one canonical fixture per ATS is the
    contract. Refresh the same URL family each time so diffs are meaningful.
  * Auto-commit the change. Inspect the diff, run the test, then commit
    manually.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURES_DIR = REPO_ROOT / "tests_v3" / "fixtures"
SUPPORTED_ATSES = ("greenhouse", "lever", "ashby")


def _usage_and_exit(code: int = 1) -> None:
    print(__doc__, file=sys.stderr)
    sys.exit(code)


async def _refresh(ats: str, url: str) -> None:
    if ats not in SUPPORTED_ATSES:
        print(f"unsupported ATS {ats!r}; must be one of {SUPPORTED_ATSES}", file=sys.stderr)
        sys.exit(2)

    # Import here so a `--help` invocation doesn't require Playwright be installed.
    from av3.config import load_settings
    from av3.sources.browser.session import BrowserSession

    settings = load_settings()
    session = BrowserSession(settings.browser_profile_dir)
    await session.start()
    try:
        page = await session.new_page()
        await page.goto(url, wait_until="domcontentloaded")
        # Let SPAs (Ashby) finish rendering; let scripts (recaptcha) attach.
        await asyncio.sleep(3.0)
        html = await page.content()
    finally:
        await session.stop()

    fixture_path = FIXTURES_DIR / ats / "apply_form.html"
    fixture_path.parent.mkdir(parents=True, exist_ok=True)
    old_size = fixture_path.stat().st_size if fixture_path.exists() else 0
    fixture_path.write_text(html, encoding="utf-8")
    new_size = fixture_path.stat().st_size

    print(f"Refreshed {fixture_path}")
    print(f"  old size: {old_size:>8} bytes")
    print(f"  new size: {new_size:>8} bytes")
    print(f"  source:   {url}")
    print()
    print("Next: run `pytest tests_v3/test_selector_drift.py -v` to verify "
          "all selectors still resolve.")


def main() -> None:
    if len(sys.argv) != 3 or sys.argv[1] in ("-h", "--help"):
        _usage_and_exit(0 if sys.argv[1:] in (["-h"], ["--help"]) else 1)
    ats, url = sys.argv[1], sys.argv[2]
    asyncio.run(_refresh(ats, url))


if __name__ == "__main__":
    main()
