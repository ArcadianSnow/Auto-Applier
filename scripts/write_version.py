"""Write a VERSION file at PROJECT_ROOT for non-git distributions.

Run this before packaging a zip release so users who download via
GitHub's "download zip" (or our update.bat) get a real version
stamp in their run logs instead of "(unknown)".

The script samples the current git state and writes a single line
to ``VERSION`` in the format::

    YYYY.MM.DD-N (sha)

Where ``YYYY.MM.DD`` is today's date, ``N`` is the count of commits
made today (1-based), and ``sha`` is the 7-char short sha of HEAD.
Format matches what ``log_setup._detect_version`` produces for git
checkouts, so logs from zip and git installs are visually identical.

Usage::

    python scripts/write_version.py

Run from any working directory; resolves PROJECT_ROOT via the
project's config module. Idempotent — overwrites VERSION each call.

The VERSION file is gitignored so dev-tree commits don't accumulate
noise; CI/release scripts run this and bundle the file into the
zip artifact.
"""
from __future__ import annotations

import subprocess
import sys
from datetime import datetime
from pathlib import Path


def _git(*args: str) -> str:
    """Run a git command from PROJECT_ROOT, return stripped stdout
    or empty string on any failure.
    """
    here = Path(__file__).resolve().parent.parent
    try:
        result = subprocess.run(
            ["git", "-C", str(here), *args],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode != 0:
            return ""
        return result.stdout.strip()
    except Exception:
        return ""


def compute_version() -> str:
    """Return the version string in the canonical format."""
    sha = _git("rev-parse", "--short", "HEAD")
    if not sha:
        # No git available — caller can decide how to handle.
        # We could read CI env vars here (GITHUB_SHA etc.) but
        # the script is meant to run in a checkout, so bail.
        return ""

    today_iso = datetime.now().strftime("%Y-%m-%d")
    today_dot = today_iso.replace("-", ".")

    log_today = _git(
        "log", "--pretty=format:%cs",
        f"--since={today_iso} 00:00:00",
        f"--until={today_iso} 23:59:59",
    )
    count_today = sum(
        1 for ln in log_today.splitlines() if ln.strip() == today_iso
    )

    if count_today > 0:
        human = f"{today_dot}-{count_today}"
    else:
        head_date = _git("log", "-1", "--pretty=format:%cs")
        human = head_date.replace("-", ".") if head_date else "older"

    return f"{human} ({sha})"


def main() -> int:
    project_root = Path(__file__).resolve().parent.parent
    version = compute_version()
    if not version:
        print("ERROR: could not compute version (no git?)", file=sys.stderr)
        return 1
    out = project_root / "VERSION"
    out.write_text(version + "\n", encoding="utf-8")
    print(f"Wrote {out}: {version}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
