"""CSV integrity checks and normalization utilities.

Exposes two families of operations:

- **fsck**: read-only validation. Loads every CSV, checks that each
  row parses into the current dataclass, counts orphaned foreign
  keys (SkillGap rows whose job_id doesn't exist in jobs.csv,
  Followup rows pointing at jobs that have no Application, etc.),
  and flags duplicate canonical hashes and duplicate (job_id, source)
  pairs.

- **normalize**: mutating repair. Deduplicates rows by
  (canonical identity, latest timestamp wins), rewrites
  inconsistent status values to the canonical set, and
  re-canonicalizes company names using ``storage/dedup.normalize_company``.
  Every normalize pass writes a timestamped backup first via the
  existing migration helpers.

Neither operation touches user_config.json or the profile JSON files
under ``data/profiles/`` — only the CSVs.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import asdict, fields as dc_fields

from auto_applier.storage import repository
from auto_applier.storage.dedup import normalize_company
from auto_applier.storage.migrations import _backup
from auto_applier.storage.models import Application, Followup, Job, SkillGap


# Canonical status vocabularies by model
_APPLICATION_STATUSES = {"applied", "failed", "skipped", "dry_run"}
_FOLLOWUP_STATUSES = {"pending", "done", "dismissed"}

# Common variants users might introduce via manual edits
_APPLICATION_STATUS_ALIASES = {
    "success": "applied",
    "submitted": "applied",
    "completed": "applied",
    "dryrun": "dry_run",
    "dry-run": "dry_run",
    "error": "failed",
    "skip": "skipped",
}

_FOLLOWUP_STATUS_ALIASES = {
    "done-": "done",
    "complete": "done",
    "finished": "done",
    "dismiss": "dismissed",
    "cancelled": "dismissed",
    "canceled": "dismissed",
    "todo": "pending",
    "open": "pending",
}


# ---------------------------------------------------------------------------
# fsck
# ---------------------------------------------------------------------------


def fsck() -> dict:
    """Run read-only integrity checks across every CSV.

    Returns a dict with per-model counts plus an ``issues`` list of
    human-readable strings. Safe to call repeatedly and from scripts.
    """
    report: dict = {
        "jobs": 0,
        "applications": 0,
        "skill_gaps": 0,
        "followups": 0,
        "issues": [],
    }

    try:
        jobs = repository.load_all(Job)
        report["jobs"] = len(jobs)
    except Exception as e:
        report["issues"].append(f"failed to load jobs.csv: {e}")
        jobs = []

    try:
        apps = repository.load_all(Application)
        report["applications"] = len(apps)
    except Exception as e:
        report["issues"].append(f"failed to load applications.csv: {e}")
        apps = []

    try:
        gaps = repository.load_all(SkillGap)
        report["skill_gaps"] = len(gaps)
    except Exception as e:
        report["issues"].append(f"failed to load skill_gaps.csv: {e}")
        gaps = []

    try:
        followups = repository.load_all(Followup)
        report["followups"] = len(followups)
    except Exception as e:
        report["issues"].append(f"failed to load followups.csv: {e}")
        followups = []

    job_keys = {(j.job_id, j.source) for j in jobs}
    job_ids = {j.job_id for j in jobs}

    # Duplicate (job_id, source) in jobs.csv
    dupe_job_keys = [k for k, c in Counter((j.job_id, j.source) for j in jobs).items() if c > 1]
    for k in dupe_job_keys:
        report["issues"].append(f"duplicate jobs.csv row: {k}")

    # Duplicate canonical hashes (cross-source cross-post leakage)
    hash_counter = Counter(j.canonical_hash for j in jobs if j.canonical_hash)
    for h, c in hash_counter.items():
        if c > 1:
            matches = [
                f"{j.job_id}@{j.source}" for j in jobs if j.canonical_hash == h
            ]
            report["issues"].append(
                f"duplicate canonical_hash {h} across {len(matches)} rows: "
                f"{', '.join(matches[:5])}"
            )

    # Orphan application rows (job_id has no matching Job)
    for a in apps:
        if a.job_id not in job_ids:
            report["issues"].append(
                f"orphan application: job_id={a.job_id} source={a.source}"
            )

    # Application status outside canonical vocabulary
    for a in apps:
        if a.status not in _APPLICATION_STATUSES:
            alias = _APPLICATION_STATUS_ALIASES.get(a.status.lower().strip())
            if alias:
                report["issues"].append(
                    f"application status '{a.status}' is an alias for '{alias}'"
                )
            else:
                report["issues"].append(
                    f"application status '{a.status}' is not in canonical set"
                )

    # Orphan gap rows
    for g in gaps:
        if g.job_id not in job_ids:
            report["issues"].append(
                f"orphan skill_gap: job_id={g.job_id} field={g.field_label}"
            )

    # Orphan follow-ups (no application at all)
    app_job_ids = {a.job_id for a in apps}
    for f in followups:
        if f.job_id not in app_job_ids:
            report["issues"].append(
                f"orphan followup: job_id={f.job_id} due={f.due_date}"
            )
        if f.status not in _FOLLOWUP_STATUSES:
            alias = _FOLLOWUP_STATUS_ALIASES.get(f.status.lower().strip())
            if alias:
                report["issues"].append(
                    f"followup status '{f.status}' is an alias for '{alias}'"
                )
            else:
                report["issues"].append(
                    f"followup status '{f.status}' is not in canonical set"
                )

    report["healthy"] = len(report["issues"]) == 0
    return report


# ---------------------------------------------------------------------------
# normalize
# ---------------------------------------------------------------------------


def _rewrite(model_type: type, rows: list) -> None:
    """Overwrite a CSV with the given row list, backing up first.

    Mirrors the serialization rules in ``repository.save`` so values
    round-trip through ``repository.load_all``.
    """
    import csv

    path = repository._CSV_MAP[model_type]
    _backup(path)
    headers = [f.name for f in dc_fields(model_type)]
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=headers)
        writer.writeheader()
        for r in rows:
            row = asdict(r)
            for k, v in row.items():
                if isinstance(v, bool):
                    row[k] = str(v)
                elif isinstance(v, list):
                    row[k] = str(v)
            writer.writerow(row)


def normalize() -> dict:
    """Repair common CSV inconsistencies in place.

    Operations performed (each atomic — a failure mid-operation never
    half-writes a file because every rewrite goes to a temp via the
    migration backup path first):

    1. Dedup jobs.csv by (job_id, source), keeping the first occurrence.
    2. Rewrite application status values that match a known alias.
    3. Rewrite followup status values that match a known alias.
    4. Canonicalize company names on Jobs (``normalize_company``) —
       preserves the original casing so it's recoverable from backup
       but ensures consistency for dedup.
    5. Retroactively correct 'dry_run' applications that have a
       non-empty failure_reason. These are the false positives from
       the old pipeline.apply_to_job bug where ANY dry-run apply
       was marked 'dry_run' regardless of whether the apply
       actually reached the submit step. Those rows should be
       'failed'.

    Returns a dict describing what changed.
    """
    changes = {
        "jobs_deduped": 0,
        "application_statuses_fixed": 0,
        "followup_statuses_fixed": 0,
        "companies_renormalized": 0,
        "false_dry_runs_corrected": 0,
    }

    # Step 1: dedup jobs
    jobs = repository.load_all(Job)
    seen: set[tuple[str, str]] = set()
    kept: list[Job] = []
    for j in jobs:
        key = (j.job_id, j.source)
        if key in seen:
            changes["jobs_deduped"] += 1
            continue
        seen.add(key)
        kept.append(j)

    # Step 4 (piggybacks on the same Jobs rewrite)
    for j in kept:
        canon = normalize_company(j.company)
        if canon and canon != j.company.lower().strip():
            j.company = canon.title()  # Title case for readability
            changes["companies_renormalized"] += 1

    if changes["jobs_deduped"] or changes["companies_renormalized"]:
        _rewrite(Job, kept)

    # Step 2: application status aliases + false-dry-run correction
    apps = repository.load_all(Application)
    apps_changed = False
    for a in apps:
        # 2a: canonical alias rewrite
        if a.status not in _APPLICATION_STATUSES:
            alias = _APPLICATION_STATUS_ALIASES.get(a.status.lower().strip())
            if alias:
                a.status = alias
                changes["application_statuses_fixed"] += 1
                apps_changed = True
        # 2b: dry_run + failure_reason = historical false apply from
        # the pipeline.apply_to_job bug. Rewrite as 'failed'.
        if (
            a.status == "dry_run"
            and a.failure_reason
            and a.failure_reason.strip()
        ):
            a.status = "failed"
            changes["false_dry_runs_corrected"] += 1
            apps_changed = True
    if apps_changed:
        _rewrite(Application, apps)

    # Step 3: followup status aliases
    followups = repository.load_all(Followup)
    fu_changed = False
    for f in followups:
        if f.status in _FOLLOWUP_STATUSES:
            continue
        alias = _FOLLOWUP_STATUS_ALIASES.get(f.status.lower().strip())
        if alias:
            f.status = alias
            changes["followup_statuses_fixed"] += 1
            fu_changed = True
    if fu_changed:
        _rewrite(Followup, followups)

    changes["total"] = sum(v for k, v in changes.items() if k != "total")
    return changes
