"""Pure domain layer: dataclasses + the job/application state machine. No I/O."""

from auto_applier.domain.models import (
    Answer,
    Application,
    Job,
    JobScore,
    SkillGap,
    new_id,
    utcnow_iso,
)
from auto_applier.domain.state import (
    ALLOWED_TRANSITIONS,
    EPHEMERAL_STATES,
    TERMINAL_STATES,
    ApplicationStatus,
    ApplyMode,
    InvalidTransition,
    JobState,
    can_transition,
    transition,
)

__all__ = [
    "ALLOWED_TRANSITIONS",
    "EPHEMERAL_STATES",
    "TERMINAL_STATES",
    "Answer",
    "Application",
    "ApplicationStatus",
    "ApplyMode",
    "InvalidTransition",
    "Job",
    "JobScore",
    "JobState",
    "SkillGap",
    "can_transition",
    "new_id",
    "transition",
    "utcnow_iso",
]
