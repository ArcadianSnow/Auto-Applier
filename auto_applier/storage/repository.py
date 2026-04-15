"""CSV-backed persistence layer with Excel compatibility."""
import csv
import os
from dataclasses import asdict, fields as dc_fields
from datetime import datetime, timezone
from pathlib import Path
from typing import TypeVar, Type


def _atomic_rewrite(path: Path, headers: list[str], rows: list[dict]) -> None:
    """Rewrite a CSV file atomically.

    Writes to ``<path>.tmp`` then os.replace()s over the original.
    os.replace is atomic on POSIX and (since Python 3.3) on Windows.
    A crash mid-write leaves the original intact — the partial tmp
    file is orphaned but never visible to readers.
    """
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=headers)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
        f.flush()
        try:
            os.fsync(f.fileno())
        except OSError:
            pass  # fsync not available on all filesystems — best-effort
    os.replace(tmp, path)

from auto_applier.config import (
    JOBS_CSV, APPLICATIONS_CSV, SKILL_GAPS_CSV, FOLLOWUPS_CSV,
)
from auto_applier.storage.migrations import migrate_csv, record_current_schema
from auto_applier.storage.models import Job, Application, SkillGap, Followup

T = TypeVar("T", Job, Application, SkillGap, Followup)

_CSV_MAP: dict[type, Path] = {
    Job: JOBS_CSV,
    Application: APPLICATIONS_CSV,
    SkillGap: SKILL_GAPS_CSV,
    Followup: FOLLOWUPS_CSV,
}


def _ensure_csv(path: Path, model_type: type) -> None:
    """Create the CSV file with headers if needed, migrating on schema drift."""
    if not path.exists():
        headers = [f.name for f in dc_fields(model_type)]
        with open(path, "w", newline="", encoding="utf-8") as f:
            csv.DictWriter(f, fieldnames=headers).writeheader()
        # Record the baseline schema for first-time installs so future
        # drift is detectable. Safe to call repeatedly.
        record_current_schema([Job, Application, SkillGap, Followup])
        return
    # Existing file: migrate in place if fields have shifted since last run.
    migrate_csv(path, model_type)


def save(record: T) -> None:
    """Append a single record to its corresponding CSV file."""
    model_type = type(record)
    path = _CSV_MAP[model_type]
    _ensure_csv(path, model_type)
    row = asdict(record)
    # Convert non-string types to their string representation for CSV storage
    for k, v in row.items():
        if isinstance(v, bool):
            row[k] = str(v)
        elif isinstance(v, list):
            row[k] = str(v)
    with open(path, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=row.keys())
        writer.writerow(row)


def load_all(model_type: Type[T]) -> list[T]:
    """Load every record from the CSV file for the given model type."""
    path = _CSV_MAP[model_type]
    _ensure_csv(path, model_type)
    model_field_names = {f.name for f in dc_fields(model_type)}
    records: list[T] = []
    with open(path, "r", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            # Filter to known fields only (forward/backward compat)
            filtered = {k: v for k, v in row.items() if k in model_field_names}
            # Convert string representations back to native types
            for field_info in dc_fields(model_type):
                if field_info.name not in filtered:
                    continue
                ft = field_info.type
                if ft is bool or ft == "bool":
                    filtered[field_info.name] = filtered[field_info.name] == "True"
                elif ft is int or ft == "int":
                    try:
                        filtered[field_info.name] = int(filtered[field_info.name] or 0)
                    except (ValueError, TypeError):
                        filtered[field_info.name] = 0
            records.append(model_type(**filtered))
    return records


def job_already_processed(job_id: str, source: str) -> bool:
    """Return True if any Application exists for this (job_id, source).

    "Processed" covers every terminal state the scoring pipeline can
    reach: applied, dry_run, skipped (low score / ghost / external /
    discovery-only). The job has already been seen and graded, so
    re-running scoring in a subsequent cycle would just burn LLM time
    for the same verdict.

    Unscored Jobs in jobs.csv — those scraped but never picked up by
    the engine's inner loop (e.g. because the per-platform budget was
    hit mid-batch in an earlier cycle) — intentionally do NOT dedupe
    here. Continuous mode needs to be able to come back to them next
    cycle.
    """
    for app in load_all(Application):
        if app.job_id == job_id and app.source == source:
            return True
    return False


# Kept as an alias so external callers / old scripts keep working.
# Prefer ``job_already_processed`` for new code.
job_already_applied = job_already_processed


def processed_pairs() -> set[tuple[str, str]]:
    """Return every (job_id, source) pair with any Application row.

    Batch-friendly companion to ``job_already_processed``. Call once
    per batch and check pair membership in-memory instead of
    re-reading applications.csv per job.
    """
    return {(a.job_id, a.source) for a in load_all(Application)}


def processed_canonical_hashes() -> set[str]:
    """Return canonical_hashes of Jobs that have at least one Application.

    Batch-friendly companion to ``job_seen_canonically``. Joins
    jobs → applications so unscored jobs don't dedupe — which is
    required for continuous-run mode where cycle 1 may only score
    the first 3 of 99 scraped jobs and cycle 2 must be free to reach
    the remaining 96.
    """
    processed_ids = {a.job_id for a in load_all(Application)}
    return {
        j.canonical_hash
        for j in load_all(Job)
        if j.canonical_hash and j.job_id in processed_ids
    }


def job_seen_canonically(canonical_hash: str) -> bool:
    """Return True if this canonical_hash has been PROCESSED before.

    Used to skip cross-posted duplicates — the same "Senior Data
    Analyst at Acme Corp" listing appearing on both LinkedIn and
    Indeed, within or across runs.

    "Processed" means any Application row exists for any Job with
    this hash. A Job that was scraped but never scored (budget ran
    out mid-batch) does NOT dedupe — continuous-run mode relies on
    this so later cycles can pick up where earlier cycles stopped.

    Empty hash is always False (unknown identity — callers fall back
    to per-source ``job_already_processed``).
    """
    if not canonical_hash:
        return False
    return canonical_hash in processed_canonical_hashes()


def schedule_followups(
    job_id: str,
    source: str,
    applied_at_iso: str,
    cadence_days: list[int] | None = None,
) -> list[Followup]:
    """Create one Followup per cadence offset for a submitted application.

    ``applied_at_iso`` is the full ISO timestamp from ``Application.applied_at``.
    Each followup's due_date is computed by adding ``offset_days`` to the
    date portion. Returns the list of created Followup records (already
    persisted to ``followups.csv``).
    """
    from datetime import date, timedelta
    from auto_applier.config import FOLLOWUP_CADENCE_DAYS

    cadence = cadence_days if cadence_days is not None else FOLLOWUP_CADENCE_DAYS
    try:
        base = datetime.fromisoformat(applied_at_iso).date()
    except ValueError:
        base = date.today()

    created: list[Followup] = []
    for offset in cadence:
        due = (base + timedelta(days=offset)).isoformat()
        f = Followup(job_id=job_id, source=source, due_date=due)
        save(f)
        created.append(f)
    return created


def list_followups(status: str | None = None) -> list[Followup]:
    """Return follow-ups, optionally filtered by status."""
    items = load_all(Followup)
    if status is None:
        return items
    return [f for f in items if f.status == status]


def get_due_followups(as_of: str | None = None) -> list[Followup]:
    """Return pending follow-ups whose due_date is on or before ``as_of``.

    ``as_of`` is an ISO date string; defaults to today (UTC).
    """
    from datetime import date

    cutoff = as_of or date.today().isoformat()
    return [
        f for f in load_all(Followup)
        if f.status == "pending" and f.due_date <= cutoff
    ]


def update_followups_for_job(
    job_id: str,
    new_status: str,
    source: str | None = None,
) -> int:
    """Set every pending follow-up for ``job_id`` to ``new_status``.

    Rewrites the whole followups.csv in place. Returns the number of
    records updated. ``source`` further narrows the match when given.
    """
    path = _CSV_MAP[Followup]
    _ensure_csv(path, Followup)
    followups = load_all(Followup)
    updated = 0
    for f in followups:
        if f.status != "pending":
            continue
        if f.job_id != job_id:
            continue
        if source and f.source != source:
            continue
        f.status = new_status
        updated += 1
    # Rewrite the file atomically so a crash mid-write can't leave
    # followups.csv truncated or half-written.
    headers = [fld.name for fld in dc_fields(Followup)]
    rows = []
    for f in followups:
        row = {k: v for k, v in asdict(f).items()}
        for k, v in row.items():
            if isinstance(v, bool):
                row[k] = str(v)
            elif isinstance(v, list):
                row[k] = str(v)
        rows.append(row)
    _atomic_rewrite(path, headers, rows)
    return updated


def get_todays_application_count(
    include_dry_run: bool = False, source: str = "",
) -> int:
    """Return the number of applications made today (UTC).

    By default, counts ONLY real submissions — dry runs don't
    consume daily quota, because no application was actually sent.
    Callers that want to count both (e.g. audit reports, pattern
    analysis) can pass ``include_dry_run=True``.

    Pass ``source`` to narrow the count to a specific platform
    (e.g. "linkedin", "indeed"). Used by the engine to enforce
    per-platform daily budgets.
    """
    today = datetime.now(timezone.utc).date().isoformat()
    counted_statuses = {"applied"}
    if include_dry_run:
        counted_statuses.add("dry_run")
    return sum(
        1
        for app in load_all(Application)
        if app.applied_at.startswith(today)
        and app.status in counted_statuses
        and (not source or app.source == source)
    )
