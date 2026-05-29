"""DTO helpers — domain models → plain dicts for JSON responses.

FastAPI can serialize dataclasses directly, but our domain dataclasses carry
internal state we don't want in the API surface (e.g. discovered_at /
updated_at as ISO strings are fine, but other fields may grow). Centralizing
the shape here keeps the routes thin and the contract testable.

Each helper takes a domain object and returns a small dict. No sqlite handles
travel through here — call sites pull from the repos, then convert.
"""

from __future__ import annotations

import json
import sqlite3

from av3.domain.models import Job
from av3.domain.state import JobState
from av3.sources.health import SourceHealthRecord


def job_brief(job: Job) -> dict:
    """Compact job dict for queue / list endpoints. Drops the full JD —
    callers that need it hit a per-job detail endpoint (Phase 4 (2/M))."""
    return {
        "id": job.id,
        "source": job.source,
        "title": job.title,
        "company": job.company,
        "location": job.location,
        "url": job.url,
        "state": job.state.value,
        "discovered_at": job.discovered_at,
        "updated_at": job.updated_at,
    }


def health_record(rec: SourceHealthRecord) -> dict:
    """Dashboard 'login needed' badge payload (spec §8b). The reason field is
    what the UI shows next to the source name."""
    return {
        "source": rec.source,
        "state": rec.state.value,
        "reason": rec.reason,
        "paused": rec.state.value == "AUTH_REQUIRED",
    }


def recent_scheduler_event(row: sqlite3.Row | None) -> dict | None:
    """Convert one events.db row (filtered to ``stage='scheduler'``) into the
    dict shape ``/api/status`` returns under ``last_cycle``. None pass-through
    so callers can chain ``.fetchone()`` directly."""
    if row is None:
        return None
    ctx = json.loads(row["context_json"]) if row["context_json"] else {}
    return {
        "ts": row["ts"],
        "status": row["status"],
        "cycle": ctx.get("cycle"),
        "duration_ms": row["duration_ms"],
        "errors": ctx.get("errors") or [],
        "reason": ctx.get("reason"),
    }


# The states the dashboard wants to count separately. The status endpoint
# returns ``count_by_state`` verbatim from JobRepo so any new state shows up
# automatically; this list is the *order* the (2/M) dashboard renders them
# (pipeline left-to-right).
PIPELINE_STATES: list[JobState] = [
    JobState.DISCOVERED,
    JobState.DESCRIBED,
    JobState.SCORED,
    JobState.DECIDED,
    JobState.QUEUED_APPLY,
    JobState.APPLYING,
    JobState.REVIEW,
    JobState.APPLIED,
    JobState.SKIPPED,
    JobState.FILTERED,
    JobState.FAILED,
]
