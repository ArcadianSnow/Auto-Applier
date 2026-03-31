"""Tests for the scoring pipeline."""
import pytest
from auto_applier.scoring.models import JobScore, ScoreDecision


class TestScoreDecision:
    def test_auto_apply(self):
        score = JobScore(score=8, decision=ScoreDecision.AUTO_APPLY, resume_label="analyst")
        assert score.decision == ScoreDecision.AUTO_APPLY
        assert score.score == 8

    def test_user_review(self):
        score = JobScore(score=5, decision=ScoreDecision.USER_REVIEW, resume_label="engineer")
        assert score.decision == ScoreDecision.USER_REVIEW

    def test_skip(self):
        score = JobScore(score=2, decision=ScoreDecision.SKIP, resume_label="entry")
        assert score.decision == ScoreDecision.SKIP

    def test_override_decision(self):
        from auto_applier.scoring.scorer import JobScorer
        from unittest.mock import MagicMock

        scorer = JobScorer(resume_manager=MagicMock())
        score = JobScore(score=5, decision=ScoreDecision.USER_REVIEW, resume_label="test")

        updated = scorer.override_decision(score, ScoreDecision.AUTO_APPLY)
        assert updated.decision == ScoreDecision.AUTO_APPLY
