"""Scoring models and decision types."""
from dataclasses import dataclass, field
from enum import Enum


class ScoreDecision(Enum):
    AUTO_APPLY = "auto_apply"
    USER_REVIEW = "user_review"
    SKIP = "skip"


@dataclass
class JobScore:
    """Complete scoring result for a job against the best resume."""
    score: int  # 1-10
    decision: ScoreDecision
    resume_label: str  # which resume scored best
    explanation: str = ""
    matched_skills: list = field(default_factory=list)
    missing_skills: list = field(default_factory=list)
    deal_breakers: list = field(default_factory=list)
    all_resume_scores: list = field(default_factory=list)  # list[ResumeScore] for transparency
