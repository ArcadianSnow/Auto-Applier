"""Application outcome tracking — what happened after we submitted.

Outcomes form a rough state machine:

    pending ─┬─> acknowledged ─┬─> interview ─┬─> rejected
             │                 │              └─> offer
             │                 └─> rejected
             ├─> interview ────┬─> rejected
             │                 └─> offer
             ├─> rejected
             ├─> offer
             ├─> ghosted (auto-set after 30 days no response)
             └─> withdrawn (user pulled out)

The state machine is advisory — we don't hard-enforce transitions
so the user can correct mistakes (e.g. "I entered rejected but it
was actually ghosted"). See :func:`set_outcome` for validation.
"""
from __future__ import annotations

import csv
import logging
from dataclasses import asdict, fields as dc_fields
from datetime import datetime, timedelta, timezone
from pathlib import Path

from auto_applier.storage.models import Application
from auto_applier.storage import repository

logger = logging.getLogger(__name__)


VALID_OUTCOMES = {
    "pending",        # default — applied, haven't heard back
    "acknowledged",   # "thanks for applying" auto-reply
    "interview",      # interview invitation received
    "rejected",       # employer said no
    "offer",          # received an offer
    "ghosted",        # 30+ days, no response
    "withdrawn",      # candidate pulled out
}

# Outcomes that mean "the application is closed, stop nagging"
CLOSED_OUTCOMES = {"rejected", "offer", "ghosted", "withdrawn"}

# Number of days with no response after which `auto_mark_ghosted`
# flags an application as ghosted.
GHOST_DAYS = 30


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def set_outcome(
    job_id: str,
    outcome: str,
    source: str = "",
    note: str = "",
) -> Application | None:
    """Update the outcome of an application for a specific job.

    ``source`` narrows the match when the user applied to the same
    job on multiple platforms. Empty source matches any row with the
    given ``job_id``.

    Returns the updated Application, or None if no matching record
    was found. Raises ValueError if ``outcome`` is not a valid state.

    Rewrites applications.csv in place — this is cheap at our scale
    (a few hundred rows max in typical use).
    """
    if outcome not in VALID_OUTCOMES:
        raise ValueError(
            f"Invalid outcome '{outcome}'. Must be one of: "
            f"{sorted(VALID_OUTCOMES)}"
        )

    apps = repository.load_all(Application)
    target: Application | None = None
    for app in apps:
        if app.job_id != job_id:
            continue
        if source and app.source != source:
            continue
        # Only update "applied" or "dry_run" rows — skipped/failed
        # rows don't have an outcome worth tracking.
        if app.status not in ("applied", "dry_run"):
            continue
        target = app
        app.outcome = outcome
        app.outcome_at = _now_iso()
        if note:
            app.outcome_note = note
        break

    if target is None:
        return None

    # Rewrite the whole file — stays consistent on crash, trivial at scale
    _rewrite_applications(apps)
    logger.info(
        "Set outcome %s for job_id=%s source=%s", outcome, job_id, source,
    )
    return target


def auto_mark_ghosted(days: int = GHOST_DAYS) -> int:
    """Find stale pending applications and mark them as ghosted.

    An application is considered ghosted if:
    - outcome is still "pending"
    - status is "applied" (not dry_run, not skipped)
    - applied_at is older than ``days`` days

    Returns the count of newly-ghosted applications.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    apps = repository.load_all(Application)

    count = 0
    for app in apps:
        if app.status != "applied":
            continue
        if app.outcome != "pending":
            continue
        try:
            applied = datetime.fromisoformat(app.applied_at)
        except (ValueError, TypeError):
            continue
        # Normalize to UTC if naive
        if applied.tzinfo is None:
            applied = applied.replace(tzinfo=timezone.utc)
        if applied < cutoff:
            app.outcome = "ghosted"
            app.outcome_at = _now_iso()
            app.outcome_note = f"Auto-marked after {days}+ days with no response"
            count += 1

    if count:
        _rewrite_applications(apps)
        logger.info("Auto-marked %d applications as ghosted", count)
    return count


def outcome_summary() -> dict[str, int]:
    """Return counts of each outcome across all submitted rows.

    Counts both "applied" (real submissions) and "dry_run" rows so
    users can track outcomes during testing too. Skipped/failed rows
    have no outcome worth summarizing.

    Keys are outcome strings, values are counts. Useful for status
    dashboards.
    """
    from collections import Counter
    apps = repository.load_all(Application)
    counter: Counter = Counter()
    for app in apps:
        if app.status not in ("applied", "dry_run"):
            continue
        counter[app.outcome or "pending"] += 1
    return dict(counter)


def _rewrite_applications(apps: list[Application]) -> None:
    """Rewrite applications.csv with the full list."""
    path = repository._CSV_MAP[Application]
    headers = [f.name for f in dc_fields(Application)]

    # Ensure parent + header exist
    repository._ensure_csv(path, Application)

    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=headers)
        writer.writeheader()
        for app in apps:
            row = asdict(app)
            for k, v in row.items():
                if isinstance(v, bool):
                    row[k] = str(v)
                elif isinstance(v, list):
                    row[k] = str(v)
            writer.writerow(row)
