"""CSV-based data storage. Files are human-readable and openable in Excel."""

import csv
from dataclasses import asdict, fields
from pathlib import Path
from typing import Type, TypeVar

from auto_applier.config import APPLICATIONS_CSV, JOBS_CSV, SKILL_GAPS_CSV
from auto_applier.storage.models import Application, Job, SkillGap

T = TypeVar("T", Job, Application, SkillGap)


def _csv_path_for(model_type: Type[T]) -> Path:
    return {
        Job: JOBS_CSV,
        Application: APPLICATIONS_CSV,
        SkillGap: SKILL_GAPS_CSV,
    }[model_type]


def _ensure_csv(path: Path, model_type: Type[T]) -> None:
    """Create the CSV with headers if it doesn't exist."""
    if not path.exists():
        headers = [f.name for f in fields(model_type)]
        with open(path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(headers)


def save(record: T) -> None:
    """Append a single record to the appropriate CSV file."""
    model_type = type(record)
    path = _csv_path_for(model_type)
    _ensure_csv(path, model_type)

    row = asdict(record)
    with open(path, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=row.keys())
        writer.writerow(row)


def load_all(model_type: Type[T]) -> list[T]:
    """Load all records of a given type from its CSV file."""
    path = _csv_path_for(model_type)
    _ensure_csv(path, model_type)

    records = []
    model_field_names = {f.name for f in fields(model_type)}
    with open(path, "r", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            # Filter to known fields only (forward/backward CSV compatibility)
            filtered = {k: v for k, v in row.items() if k in model_field_names}
            records.append(model_type(**filtered))
    return records


def job_already_applied(job_id: str, source: str = "") -> bool:
    """Check if we've already applied to (or attempted) a job."""
    applications = load_all(Application)
    if source:
        return any(a.job_id == job_id and a.source == source for a in applications)
    return any(a.job_id == job_id for a in applications)


def get_todays_application_count() -> int:
    """Count how many applications were submitted today."""
    from datetime import date

    today = date.today().isoformat()
    applications = load_all(Application)
    return sum(
        1
        for a in applications
        if a.applied_at.startswith(today) and a.status in ("applied", "dry_run")
    )
