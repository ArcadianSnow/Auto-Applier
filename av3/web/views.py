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

from av3.domain.models import Application, Job, JobScore
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


def job_detail(
    job: Job,
    score: JobScore | None,
    applications: list[Application],
) -> dict:
    """Full per-job payload for ``/api/jobs/<id>``.

    Bundles three repo reads (job + score + per-job applications) into one
    dict so the per-job page does ONE fetch. Cover-letter / generated-resume
    paths come from the most recent Application — the optimize worker writes
    derived paths into the application row at apply time.
    """
    return {
        "job": {
            **job_brief(job),
            "description": job.description,
            "compensation": job.compensation,
            "posted_at": job.posted_at,
            "ghost_score": job.ghost_score,
            "canonical_hash": job.canonical_hash,
        },
        "score": {
            "total": score.total,
            "dimensions": dict(score.dimensions),
            "model": score.model,
            "scored_at": score.scored_at,
        } if score is not None else None,
        "applications": [application_dto(a) for a in applications],
    }


def application_dto(app: Application) -> dict:
    """One application row — used in history + per-job detail."""
    return {
        "id": app.id,
        "job_id": app.job_id,
        "mode": app.mode.value,
        "status": app.status.value,
        "cover_letter_path": app.cover_letter_path,
        "generated_resume_path": app.generated_resume_path,
        "submitted_at": app.submitted_at,
    }


def history_row(app: Application, job: Job | None, score: JobScore | None) -> dict:
    """One entry in ``/api/history``. The job and score may be missing if the
    job was pruned out of retention — the application row is the durable
    record, so we still surface what we have rather than dropping the row."""
    base = application_dto(app)
    base["job"] = job_brief(job) if job is not None else None
    base["score_total"] = score.total if score is not None else None
    return base


def event_payload(row: sqlite3.Row) -> dict:
    """Shape one events.db row for the SSE stream / fallback poll endpoint.

    The full ``error_msg`` stays in app.db (it can be long); the stream
    surfaces error_type + a short error_msg snippet for the UI's recent-
    activity panel. The context_json is parsed so the UI doesn't have to."""
    ctx = json.loads(row["context_json"]) if row["context_json"] else {}
    error_msg = row["error_msg"]
    if error_msg is not None and len(error_msg) > 200:
        # Truncate long stack traces — the dashboard is a glance surface,
        # not a debugger; the full message is queryable in events.db.
        error_msg = error_msg[:197] + "..."
    return {
        "id": row["id"],
        "ts": row["ts"],
        "stage": row["stage"],
        "status": row["status"],
        "platform": row["platform"],
        "job_id": row["job_id"],
        "duration_ms": row["duration_ms"],
        "error_type": row["error_type"],
        "error_msg": error_msg,
        "context": ctx,
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
