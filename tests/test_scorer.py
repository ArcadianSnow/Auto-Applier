"""Tests for scoring/scorer.py — JobScorer threshold decisions."""

import asyncio
from unittest.mock import MagicMock, AsyncMock

import pytest

from auto_applier.scoring.models import (
    ScoreDecision, DimensionScore, JobScore, legacy_dimensions_from_score,
)
from auto_applier.scoring.scorer import JobScorer
from auto_applier.resume.manager import ResumeInfo, ResumeScore


def _resume_info(label="test_resume"):
    return ResumeInfo(
        label=label,
        file_path="fake.pdf",
        profile_path="fake.json",
        raw_text="Python developer with 5 years experience",
    )


def _resume_score(score_val, label="test_resume"):
    """Build a ResumeScore with a legacy single-dimension score."""
    return ResumeScore(
        resume=_resume_info(label),
        dimensions=legacy_dimensions_from_score(score_val),
        explanation=f"Score: {score_val}",
    )


def _mock_manager(scores: list[ResumeScore]):
    """Build a mock ResumeManager that returns pre-set scores."""
    mgr = MagicMock()
    mgr.router = MagicMock()
    mgr.list_resumes.return_value = [s.resume for s in scores]
    mgr.score_all = AsyncMock(return_value=scores)
    return mgr


class TestScoreDecisions:
    def test_auto_apply_high_score(self):
        mgr = _mock_manager([_resume_score(9)])
        scorer = JobScorer(mgr, auto_apply_min=7, review_min=4)
        result = asyncio.run(scorer.score("some job description"))
        assert result.decision == ScoreDecision.AUTO_APPLY

    def test_auto_apply_at_threshold(self):
        mgr = _mock_manager([_resume_score(7)])
        scorer = JobScorer(mgr, auto_apply_min=7, review_min=4)
        result = asyncio.run(scorer.score("jd"))
        assert result.decision == ScoreDecision.AUTO_APPLY

    def test_user_review_mid_score(self):
        mgr = _mock_manager([_resume_score(5)])
        scorer = JobScorer(mgr, auto_apply_min=7, review_min=4)
        result = asyncio.run(scorer.score("jd"))
        assert result.decision == ScoreDecision.USER_REVIEW

    def test_review_at_threshold(self):
        mgr = _mock_manager([_resume_score(4)])
        scorer = JobScorer(mgr, auto_apply_min=7, review_min=4)
        result = asyncio.run(scorer.score("jd"))
        assert result.decision == ScoreDecision.USER_REVIEW

    def test_skip_low_score(self):
        mgr = _mock_manager([_resume_score(2)])
        scorer = JobScorer(mgr, auto_apply_min=7, review_min=4)
        result = asyncio.run(scorer.score("jd"))
        assert result.decision == ScoreDecision.SKIP

    def test_skip_no_resumes(self):
        mgr = _mock_manager([])
        scorer = JobScorer(mgr, auto_apply_min=7, review_min=4)
        result = asyncio.run(scorer.score("jd"))
        assert result.decision == ScoreDecision.SKIP
        assert result.resume_label == ""

    def test_picks_best_resume(self):
        scores = [_resume_score(3, "weak"), _resume_score(8, "strong")]
        scores.sort(key=lambda s: s.score, reverse=True)
        mgr = _mock_manager(scores)
        scorer = JobScorer(mgr, auto_apply_min=7, review_min=4)
        result = asyncio.run(scorer.score("jd"))
        assert result.resume_label == "strong"
        assert result.decision == ScoreDecision.AUTO_APPLY


class TestCLIMode:
    def test_cli_mode_uses_cli_threshold(self):
        mgr = _mock_manager([_resume_score(6)])
        scorer = JobScorer(mgr, auto_apply_min=7, review_min=4, cli_auto_apply_min=5)
        result = asyncio.run(scorer.score("jd", cli_mode=True))
        assert result.decision == ScoreDecision.AUTO_APPLY

    def test_gui_mode_uses_gui_threshold(self):
        mgr = _mock_manager([_resume_score(6)])
        scorer = JobScorer(mgr, auto_apply_min=7, review_min=4, cli_auto_apply_min=5)
        result = asyncio.run(scorer.score("jd", cli_mode=False))
        assert result.decision == ScoreDecision.USER_REVIEW


class TestOverrideDecision:
    def test_override_to_auto_apply(self):
        mgr = _mock_manager([])
        scorer = JobScorer(mgr)
        job_score = JobScore(decision=ScoreDecision.USER_REVIEW, resume_label="test")
        updated = scorer.override_decision(job_score, ScoreDecision.AUTO_APPLY)
        assert updated.decision == ScoreDecision.AUTO_APPLY

    def test_override_to_skip(self):
        mgr = _mock_manager([])
        scorer = JobScorer(mgr)
        job_score = JobScore(decision=ScoreDecision.AUTO_APPLY, resume_label="test")
        updated = scorer.override_decision(job_score, ScoreDecision.SKIP)
        assert updated.decision == ScoreDecision.SKIP

    def test_override_returns_same_object(self):
        mgr = _mock_manager([])
        scorer = JobScorer(mgr)
        job_score = JobScore(decision=ScoreDecision.SKIP)
        updated = scorer.override_decision(job_score, ScoreDecision.AUTO_APPLY)
        assert updated is job_score


class TestAllResumeScores:
    def test_all_scores_preserved(self):
        scores = [_resume_score(8, "a"), _resume_score(5, "b")]
        scores.sort(key=lambda s: s.score, reverse=True)
        mgr = _mock_manager(scores)
        scorer = JobScorer(mgr, auto_apply_min=7, review_min=4)
        result = asyncio.run(scorer.score("jd"))
        assert len(result.all_resume_scores) == 2
