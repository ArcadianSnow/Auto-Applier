"""Manual / human-apply tracking (the discover+score-only operating mode, spec §5).

In manual mode the pipeline discovers + scores but a **human applies externally**, then
records it so the job stops being surfaced. This is the write side of ``av3 applied``:
transition ``DECIDED``/``REVIEW`` → ``APPLIED`` and write an :class:`Application` row with
``mode = MANUAL``. ``APPLIED`` is the same terminal/dedup state the bot uses, so everything
downstream (dedup, retention-never-prunes, analytics, ``av3 outcome``) works unchanged.

A manual apply is a human-*attested* positive confirmation — not an inference from a click —
so it honors the spec §5 "APPLIED only on positive confirmation" invariant.

Idempotent + batch-safe: each job is marked in its own transaction and every failure mode
returns a status instead of raising, so one bad id never aborts a batch.

Non-goal (documented): ``canonical_hash`` is deliberately coarse (title+company), so two
distinct DECIDED rows can share one. Marking one APPLIED does NOT auto-skip its siblings —
"apply to one collapses the rest" would be a separate, explicit feature.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from auto_applier.db.engine import tx
from auto_applier.db.repositories import ApplicationRepo, JobRepo
from auto_applier.domain.models import Application, utcnow_iso
from auto_applier.domain.state import (
    ApplicationStatus,
    ApplyMode,
    InvalidTransition,
    JobState,
)
from auto_applier.telemetry import get_sink

__all__ = ["ManualApplyResult", "mark_manually_applied"]

#: A job state a human may attest a manual apply from.
_MANUAL_APPLY_FROM = frozenset({JobState.DECIDED, JobState.REVIEW})


@dataclass(frozen=True)
class ManualApplyResult:
    """Outcome of one ``mark_manually_applied`` call."""

    job_id: str
    status: str   # "applied" | "already" | "error"
    detail: str


def _emit(status: str, context: dict) -> None:
    """Emit a manual_apply event (drops silently when no sink is configured)."""
    sink = get_sink()
    if sink is not None:
        sink.emit(stage="manual_apply", status=status, context=context)


def mark_manually_applied(
    conn: sqlite3.Connection, job_id: str, *, resume_path: str = ""
) -> ManualApplyResult:
    """Record that the user applied to ``job_id`` externally → ``APPLIED`` (mode=MANUAL).

    Never raises: unknown id / already-applied / illegal source state all return a
    :class:`ManualApplyResult` so a batch loop can continue.
    """
    job_repo = JobRepo(conn)
    job = job_repo.get(job_id)
    if job is None:
        _emit("skip", {"job_id": job_id, "reason": "not_found"})
        return ManualApplyResult(job_id, "error", "job not found")
    if job.state is JobState.APPLIED:
        return ManualApplyResult(job_id, "already", "already APPLIED")
    if job.state not in _MANUAL_APPLY_FROM:
        return ManualApplyResult(
            job_id, "error", f"cannot mark applied from {job.state.value}"
        )
    try:
        with tx(conn):
            # Application row FIRST (mirrors the apply worker), so a crash after it still
            # leaves a record of what was attempted; then the validated state transition.
            ApplicationRepo(conn).add(
                Application(
                    job_id=job_id,
                    mode=ApplyMode.MANUAL,
                    status=ApplicationStatus.APPLIED,
                    generated_resume_path=resume_path,
                    submitted_at=utcnow_iso(),
                )
            )
            job_repo.set_state(job_id, JobState.APPLIED)
    except (InvalidTransition, KeyError) as exc:  # belt-and-suspenders; rolls back the tx
        return ManualApplyResult(job_id, "error", str(exc))
    _emit("ok", {"job_id": job_id, "company": job.company, "title": job.title})
    return ManualApplyResult(job_id, "applied", f"{job.company} — {job.title}")
