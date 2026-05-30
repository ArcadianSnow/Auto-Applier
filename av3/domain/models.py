"""Pure domain dataclasses (no I/O). Mirror the SQLite schema in ``db/schema.sql`` (spec §4).

Repositories (``db/repositories.py``) map rows ↔ these. Keeping them I/O-free makes the
domain testable in isolation and keeps the state machine (``state.py``) the only place
lifecycle logic lives.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

from av3.domain.state import ApplicationStatus, ApplyMode, JobState, OutcomeKind


def new_id() -> str:
    """Internal uuid for primary keys."""
    return uuid.uuid4().hex


def utcnow_iso() -> str:
    """UTC timestamp as ISO-8601 string (stored as TEXT, spec §4)."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class Job:
    """A discovered job and its lifecycle position (spec §4 ``jobs`` table)."""

    source: str                       # 'greenhouse' | 'lever' | ...
    source_job_id: str
    title: str
    company: str
    id: str = field(default_factory=new_id)
    canonical_hash: str = ""          # normalize_title+company (cross-source dedup)
    location: str = ""
    url: str = ""
    description: str = ""
    compensation: str = ""
    posted_at: str = ""
    ghost_score: float | None = None
    state: JobState = JobState.DISCOVERED
    discovered_at: str = field(default_factory=utcnow_iso)
    updated_at: str = field(default_factory=utcnow_iso)


@dataclass
class JobScore:
    """One score per job: the JD scored against the master profile (spec §6b — no multi-résumé)."""

    job_id: str
    total: float
    dimensions: dict[str, float] = field(default_factory=dict)  # {skills, experience, ...}
    model: str = ""
    scored_at: str = field(default_factory=utcnow_iso)


@dataclass
class Application:
    """An apply attempt against a job (spec §4 ``applications`` table)."""

    job_id: str
    mode: ApplyMode
    status: ApplicationStatus
    id: str = field(default_factory=new_id)
    cover_letter_path: str = ""
    generated_resume_path: str = ""    # per-job résumé generated from the fact bank (§6b)
    submitted_at: str = ""


@dataclass
class Outcome:
    """A recorded post-apply outcome for a job (spec §8e ``outcomes`` table).

    Several outcomes can accrue per job over time (response → interview → offer); the
    feedback analytics derives the furthest-reached stage. ``kind`` is an
    :class:`OutcomeKind`. Region/identity-neutral; carries no PII beyond a free-text note
    the user types.
    """

    job_id: str
    kind: OutcomeKind
    id: str = field(default_factory=new_id)
    noted_at: str = field(default_factory=utcnow_iso)
    note: str = ""


@dataclass
class SkillGap:
    """A skill seen in JDs but not (confidently) in the bank (spec §4)."""

    skill: str
    count: int = 1
    first_seen: str = field(default_factory=utcnow_iso)
    last_seen: str = field(default_factory=utcnow_iso)
    status: str = "open"  # open | learning | certified | dismissed


@dataclass
class Answer:
    """A known form-question answer (spec §4, §8b two-tier resolver).

    ``embedding`` enables semantic matching of differently-worded questions.
    """

    question: str
    answer: str
    source: str = "user"  # user | inferred | default
    embedding: bytes | None = None
    updated_at: str = field(default_factory=utcnow_iso)
