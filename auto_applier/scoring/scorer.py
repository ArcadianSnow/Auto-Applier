"""Job scoring pipeline — scores jobs against all resumes and makes apply decisions."""
import logging

from auto_applier.config import (
    DEFAULT_AUTO_APPLY_MIN,
    DEFAULT_CLI_AUTO_APPLY_MIN,
    DEFAULT_REVIEW_MIN,
)
from auto_applier.resume.archetypes import (
    ArchetypeClassifier, CONFIDENCE_THRESHOLD, load_archetypes,
)
from auto_applier.resume.manager import ResumeManager
from auto_applier.scoring.models import JobScore, ScoreDecision

logger = logging.getLogger(__name__)


class JobScorer:
    """Scores jobs against all resumes and decides: auto-apply, review, or skip."""

    def __init__(
        self,
        resume_manager: ResumeManager,
        auto_apply_min: int = DEFAULT_AUTO_APPLY_MIN,
        review_min: int = DEFAULT_REVIEW_MIN,
        cli_auto_apply_min: int = DEFAULT_CLI_AUTO_APPLY_MIN,
    ):
        self.resume_manager = resume_manager
        self.auto_apply_min = auto_apply_min
        self.review_min = review_min
        self.cli_auto_apply_min = cli_auto_apply_min
        self._classifier = ArchetypeClassifier(resume_manager.router)

    async def score(self, job_description: str, cli_mode: bool = False) -> JobScore:
        """Score a job against all resumes and return a decision.

        Scores every resume, picks the best match, then applies thresholds:
        - score >= auto_apply_min (or cli_auto_apply_min in CLI) -> AUTO_APPLY
        - score >= review_min -> USER_REVIEW
        - score < review_min -> SKIP
        """
        # Archetype routing: classify the JD first and restrict scoring
        # to matching resumes if confidence is high enough. No-op when
        # data/archetypes.json is absent or empty — feature is opt-in.
        archetype_filter = ""
        archs = load_archetypes()
        if archs:
            result = await self._classifier.classify(job_description, archs)
            if result.confidence >= CONFIDENCE_THRESHOLD and result.archetype:
                archetype_filter = result.archetype
                logger.debug(
                    "Archetype '%s' (conf=%.2f) — filtering resumes",
                    result.archetype, result.confidence,
                )

        resume_scores = await self.resume_manager.score_all(
            job_description, archetype_filter=archetype_filter,
        )

        if not resume_scores:
            return JobScore(
                decision=ScoreDecision.SKIP,
                resume_label="",
                explanation="No resumes loaded",
            )

        best = resume_scores[0]
        threshold = self.cli_auto_apply_min if cli_mode else self.auto_apply_min

        if best.score >= threshold:
            decision = ScoreDecision.AUTO_APPLY
        elif best.score >= self.review_min:
            decision = ScoreDecision.USER_REVIEW
        else:
            decision = ScoreDecision.SKIP

        return JobScore(
            decision=decision,
            resume_label=best.resume.label,
            dimensions=best.dimensions,
            explanation=best.explanation,
            matched_skills=best.matched_skills,
            missing_skills=best.missing_skills,
            deal_breakers=[],
            all_resume_scores=resume_scores,
        )

    def override_decision(
        self, job_score: JobScore, new_decision: ScoreDecision
    ) -> JobScore:
        """Allow user to override a scoring decision (e.g., approve a USER_REVIEW job)."""
        job_score.decision = new_decision
        return job_score
