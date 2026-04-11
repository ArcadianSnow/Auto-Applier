"""Scoring models and decision types.

Scoring is multi-dimensional: every resume-vs-job evaluation produces
one :class:`DimensionScore` per named axis (skills, experience,
seniority, location, compensation, culture, growth). The final
numeric score is a weight-adjusted blend of those dimensions,
computed on the Python side after the LLM returns raw per-dimension
values — so users can re-tune weights without repeating the LLM call.

Backwards compatibility: :class:`JobScore` still exposes an integer
``score`` 1-10 via a property so legacy code keeps working.
"""
from dataclasses import dataclass, field
from enum import Enum


class ScoreDecision(Enum):
    AUTO_APPLY = "auto_apply"
    USER_REVIEW = "user_review"
    SKIP = "skip"


# ---------------------------------------------------------------------------
# Dimensions
# ---------------------------------------------------------------------------

# Default weighted dimensions. Weights need not sum to 1.0 — they are
# normalized at aggregation time. Users can override via
# ``user_config.json:scoring_weights`` without round-tripping the LLM.
DEFAULT_DIMENSIONS: list[tuple[str, float]] = [
    ("skills",       0.35),
    ("experience",   0.20),
    ("seniority",    0.15),
    ("location",     0.10),
    ("compensation", 0.05),
    ("culture",      0.08),
    ("growth",       0.07),
]


@dataclass
class DimensionScore:
    """A single weighted dimension of a job-vs-resume match."""
    name: str
    score: float  # 0.0 - 10.0
    weight: float  # 0.0 - 1.0
    explanation: str = ""

    @property
    def weighted(self) -> float:
        return self.score * self.weight


def weighted_total(dimensions: list[DimensionScore]) -> float:
    """Return the normalized weighted 0-10 total across dimensions."""
    if not dimensions:
        return 0.0
    total_weight = sum(d.weight for d in dimensions)
    if total_weight <= 0:
        return 0.0
    weighted_sum = sum(d.weighted for d in dimensions)
    return weighted_sum / total_weight


def legacy_dimensions_from_score(score: int) -> list[DimensionScore]:
    """Synthesize a single 'overall' dimension from a legacy 1-10 score.

    Used when the LLM fallback returns only a single number (e.g. the
    rule-based backend) so downstream code can still rely on a
    populated ``dimensions`` list.
    """
    return [DimensionScore(name="overall", score=float(score), weight=1.0)]


# ---------------------------------------------------------------------------
# Aggregated scores
# ---------------------------------------------------------------------------


@dataclass
class JobScore:
    """Complete scoring result for a job against the best-matching resume.

    Backwards compatible: the ``score`` property still returns an int
    1-10, while new callers read ``dimensions`` for the full breakdown.
    """
    decision: ScoreDecision
    resume_label: str = ""
    dimensions: list[DimensionScore] = field(default_factory=list)
    explanation: str = ""
    matched_skills: list = field(default_factory=list)
    missing_skills: list = field(default_factory=list)
    deal_breakers: list = field(default_factory=list)
    all_resume_scores: list = field(default_factory=list)  # list[ResumeScore]

    @property
    def total(self) -> float:
        """Weighted 0-10 score across all dimensions (float)."""
        return weighted_total(self.dimensions)

    @property
    def score(self) -> int:
        """Legacy int 1-10 rounded from the weighted total."""
        return max(1, min(10, round(self.total)))

    def dimension(self, name: str) -> DimensionScore | None:
        for d in self.dimensions:
            if d.name == name:
                return d
        return None
