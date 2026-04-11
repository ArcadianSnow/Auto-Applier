"""Data models for Auto Applier v2."""
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class ApplicationStatus(Enum):
    APPLIED = "applied"
    FAILED = "failed"
    SKIPPED = "skipped"
    DRY_RUN = "dry_run"


class ScoreDecision(Enum):
    AUTO_APPLY = "auto_apply"
    USER_REVIEW = "user_review"
    SKIP = "skip"


@dataclass
class Job:
    job_id: str
    title: str
    company: str
    url: str
    description: str = ""
    search_keyword: str = ""
    source: str = ""  # "linkedin", "indeed", etc.
    canonical_hash: str = ""  # cross-source dedup key (see storage/dedup.py)
    liveness: str = "unknown"  # "live" | "dead" | "unknown" (see browser/liveness.py)
    found_at: str = field(default_factory=_now_iso)

    def __post_init__(self) -> None:
        """Populate canonical_hash from company + title if not set explicitly."""
        if not self.canonical_hash and self.company and self.title:
            from auto_applier.storage.dedup import canonical_job_hash
            self.canonical_hash = canonical_job_hash(self.company, self.title)


@dataclass
class Application:
    job_id: str
    status: str = "applied"  # store as string for CSV compatibility
    source: str = ""
    resume_used: str = ""  # label of the resume that was selected
    score: int = 0
    cover_letter_generated: bool = False
    failure_reason: str = ""
    fields_filled: int = 0
    fields_total: int = 0
    used_llm: bool = False
    applied_at: str = field(default_factory=_now_iso)


@dataclass
class SkillGap:
    job_id: str
    field_label: str  # the question/field that was asked
    category: str = "other"  # skill, certification, experience, other
    resume_label: str = ""  # which resume this gap applies to
    source: str = ""
    first_seen: str = field(default_factory=_now_iso)


@dataclass
class Followup:
    """A scheduled follow-up reminder for a submitted application.

    Created automatically when an Application is recorded with status
    "applied". Default cadence is day 7, 14, 21 from the applied_at
    date (configurable via FOLLOWUP_CADENCE_DAYS env var).
    """
    job_id: str
    source: str
    due_date: str  # ISO date string, e.g. "2026-04-18"
    status: str = "pending"  # pending | done | dismissed
    channel: str = "email"  # email | linkedin | phone | none
    notes: str = ""
    created_at: str = field(default_factory=_now_iso)


@dataclass
class ApplyResult:
    """Rich result from a platform apply attempt."""
    success: bool
    gaps: list = field(default_factory=list)  # list[SkillGap]
    resume_used: str = ""
    cover_letter_generated: bool = False
    failure_reason: str = ""
    fields_filled: int = 0
    fields_total: int = 0
    used_llm: bool = False
