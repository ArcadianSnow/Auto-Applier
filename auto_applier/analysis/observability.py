"""CLI observability helpers: per-job detail + full export.

Read-only utilities for introspecting the current data state from
scripts, debuggers, or a quick terminal session. No LLM, no
network, no mutation — cheap to call.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from auto_applier.storage import repository
from auto_applier.storage.models import Application, Followup, Job, SkillGap


def get_job_detail(job_id: str) -> dict:
    """Assemble everything known about a single job_id as a nested dict.

    Returns an empty dict if no Job with that ID exists. Includes the
    matching applications, skill gaps, and follow-ups so callers can
    trace a full lifecycle from one command.
    """
    jobs = [j for j in repository.load_all(Job) if j.job_id == job_id]
    if not jobs:
        return {}

    # A job_id can appear under multiple sources (canonical dedup
    # doesn't merge source rows). Return all of them.
    apps = [a for a in repository.load_all(Application) if a.job_id == job_id]
    gaps = [g for g in repository.load_all(SkillGap) if g.job_id == job_id]
    followups = [f for f in repository.load_all(Followup) if f.job_id == job_id]

    return {
        "job_id": job_id,
        "jobs": [asdict(j) for j in jobs],
        "applications": [asdict(a) for a in apps],
        "skill_gaps": [asdict(g) for g in gaps],
        "followups": [asdict(f) for f in followups],
    }


def export_all() -> dict:
    """Dump every CSV plus metadata into a single dict for JSON export."""
    return {
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "schema_hints": {
            "jobs": [f for f in Job.__dataclass_fields__],
            "applications": [f for f in Application.__dataclass_fields__],
            "skill_gaps": [f for f in SkillGap.__dataclass_fields__],
            "followups": [f for f in Followup.__dataclass_fields__],
        },
        "jobs": [asdict(j) for j in repository.load_all(Job)],
        "applications": [asdict(a) for a in repository.load_all(Application)],
        "skill_gaps": [asdict(g) for g in repository.load_all(SkillGap)],
        "followups": [asdict(f) for f in repository.load_all(Followup)],
    }


def write_export(out_path: Path) -> Path:
    """Write a full export to ``out_path`` as pretty-printed JSON."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(export_all(), indent=2, default=str),
        encoding="utf-8",
    )
    return out_path
