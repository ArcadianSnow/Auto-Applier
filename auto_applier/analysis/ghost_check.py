"""Ghost job detection via LLM analysis of a job posting.

A 'ghost listing' is a job posting left up on a board without real
intent to hire. Companies use them to collect resumes, rotate
recycled reqs every quarter, benchmark the market, or simply forget
to take down closed postings. Applying to ghost listings wastes
daily quota and LLM calls for zero upside.

This module runs a single focused LLM call per scored job, asking
the model to rate ghost likelihood 0-10 based on concrete signals
in the job description. Jobs scoring at or above
``GHOST_SKIP_THRESHOLD`` are flagged for skipping in the orchestrator
before the more expensive multi-dimensional scoring runs.

Design principles mirror our other LLM gates:

- **Fail open.** If the LLM is unavailable or returns garbage, the
  check returns ``None`` and the orchestrator proceeds normally.
  A missed ghost is much cheaper than a dropped real job.
- **One short call.** Short prompt, short response, small latency
  hit. Runs in parallel with nothing else.
- **Stored on the Job.** ``ghost_score`` and ``ghost_verdict`` are
  persisted so future runs can skip the check for jobs already
  classified, and ``cli show`` can display the verdict.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from auto_applier.llm.prompts import GHOST_JOB_CHECK
from auto_applier.llm.router import LLMRouter

logger = logging.getLogger(__name__)


@dataclass
class GhostCheckResult:
    score: int        # 0-10, 0=clearly real, 10=clearly ghost
    confidence: str   # "low" | "medium" | "high"
    signals: list[str]
    verdict: str


class GhostJobChecker:
    """Classifies job postings as likely ghost vs likely real."""

    def __init__(self, router: LLMRouter) -> None:
        self.router = router

    async def check(
        self,
        job_description: str,
        company_name: str,
        job_title: str,
    ) -> GhostCheckResult | None:
        """Run the ghost check. Returns None on any failure (fail open).

        Callers should treat None as "unknown" and proceed as if the
        check never ran — the orchestrator's skip gate only fires on
        a real high-confidence ghost classification.
        """
        if not job_description or len(job_description.strip()) < 100:
            # Too little text to classify — fail open.
            return None

        try:
            result = await self.router.complete_json(
                prompt=GHOST_JOB_CHECK.format(
                    company_name=company_name or "(unspecified)",
                    job_title=job_title or "(unspecified)",
                    job_description=job_description[:3000],
                ),
                system_prompt=GHOST_JOB_CHECK.system,
            )
        except Exception as e:
            logger.debug("Ghost check LLM call failed: %s", e)
            return None

        raw_score = result.get("ghost_score")
        try:
            score = int(raw_score)
        except (TypeError, ValueError):
            return None
        score = max(0, min(10, score))

        confidence = str(result.get("confidence", "")).lower().strip()
        if confidence not in ("low", "medium", "high"):
            confidence = "low"

        signals = result.get("signals", [])
        if not isinstance(signals, list):
            signals = []
        signals = [str(s).strip() for s in signals if str(s).strip()]

        verdict = str(result.get("verdict", "")).strip()
        if not verdict:
            return None

        return GhostCheckResult(
            score=score,
            confidence=confidence,
            signals=signals,
            verdict=verdict,
        )


def should_skip_ghost(score: int, confidence: str, threshold: int) -> bool:
    """Decide whether a job should be skipped based on ghost signals.

    Only skips on a HIGH-confidence score at or above the threshold.
    A low- or medium-confidence ghost classification is logged but
    not acted on — the cost of dropping a real job is much higher
    than the cost of applying to one ghost.
    """
    if score < 0:  # unchecked sentinel
        return False
    if confidence != "high":
        return False
    return score >= threshold
