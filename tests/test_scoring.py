"""Tests for the multi-dimensional scoring pipeline."""
import pytest

from auto_applier.scoring.models import (
    DEFAULT_DIMENSIONS,
    DimensionScore,
    JobScore,
    ScoreDecision,
    legacy_dimensions_from_score,
    weighted_total,
)


def _dims(skills=8.0, experience=7.0, seniority=6.0, location=5.0,
          compensation=5.0, culture=7.0, growth=6.0) -> list[DimensionScore]:
    """Build a full 7-dimension list with default weights."""
    values = {
        "skills": skills, "experience": experience, "seniority": seniority,
        "location": location, "compensation": compensation,
        "culture": culture, "growth": growth,
    }
    return [
        DimensionScore(name=name, score=values[name], weight=weight)
        for name, weight in DEFAULT_DIMENSIONS
    ]


class TestWeightedTotal:
    def test_empty_is_zero(self):
        assert weighted_total([]) == 0.0

    def test_all_same_score_returns_that_score(self):
        dims = [
            DimensionScore("a", 7.0, 0.5),
            DimensionScore("b", 7.0, 0.5),
        ]
        assert weighted_total(dims) == 7.0

    def test_zero_weights_returns_zero(self):
        dims = [
            DimensionScore("a", 9.0, 0.0),
            DimensionScore("b", 3.0, 0.0),
        ]
        assert weighted_total(dims) == 0.0

    def test_normalizes_weights_that_dont_sum_to_one(self):
        # 8*2 + 4*2 = 24, total_weight=4, result=6.0
        dims = [
            DimensionScore("a", 8.0, 2.0),
            DimensionScore("b", 4.0, 2.0),
        ]
        assert weighted_total(dims) == 6.0

    def test_heavy_skills_pulls_score_up(self):
        dims = _dims(skills=10.0, experience=2.0)
        result = weighted_total(dims)
        assert result > 5.5  # skills has highest default weight (0.35)


class TestDimensionScore:
    def test_weighted_property(self):
        d = DimensionScore("skills", 8.0, 0.35)
        assert d.weighted == pytest.approx(2.8)

    def test_score_and_weight_preserved(self):
        d = DimensionScore("experience", 7.5, 0.20, explanation="solid")
        assert d.score == 7.5
        assert d.weight == 0.20
        assert d.explanation == "solid"


class TestJobScoreBackwardCompat:
    def test_score_property_rounds_total(self):
        js = JobScore(decision=ScoreDecision.AUTO_APPLY, dimensions=_dims(skills=9.0))
        assert isinstance(js.score, int)
        assert 1 <= js.score <= 10

    def test_score_with_no_dimensions_is_one(self):
        js = JobScore(decision=ScoreDecision.SKIP)
        assert js.score == 1

    def test_score_clamps_to_1_minimum(self):
        dims = [DimensionScore("a", 0.0, 1.0)]
        js = JobScore(decision=ScoreDecision.SKIP, dimensions=dims)
        assert js.score == 1

    def test_score_clamps_to_10_maximum(self):
        dims = [DimensionScore("a", 15.0, 1.0)]
        js = JobScore(decision=ScoreDecision.AUTO_APPLY, dimensions=dims)
        assert js.score == 10

    def test_dimension_lookup_by_name(self):
        js = JobScore(decision=ScoreDecision.AUTO_APPLY, dimensions=_dims())
        d = js.dimension("skills")
        assert d is not None
        assert d.name == "skills"
        assert js.dimension("nonexistent") is None


class TestLegacyDimensions:
    def test_builds_overall_dimension(self):
        dims = legacy_dimensions_from_score(7)
        assert len(dims) == 1
        assert dims[0].name == "overall"
        assert dims[0].score == 7.0
        assert dims[0].weight == 1.0

    def test_score_round_trips(self):
        js = JobScore(
            decision=ScoreDecision.AUTO_APPLY,
            dimensions=legacy_dimensions_from_score(8),
        )
        assert js.score == 8


class TestDefaultDimensions:
    def test_seven_axes(self):
        assert len(DEFAULT_DIMENSIONS) == 7

    def test_weights_sum_to_one(self):
        total = sum(w for _, w in DEFAULT_DIMENSIONS)
        assert total == pytest.approx(1.0, abs=0.01)

    def test_skills_has_highest_weight(self):
        sorted_by_weight = sorted(DEFAULT_DIMENSIONS, key=lambda x: x[1], reverse=True)
        assert sorted_by_weight[0][0] == "skills"


class TestOverrideDecision:
    def test_override_preserves_dimensions(self):
        from auto_applier.scoring.scorer import JobScorer
        from unittest.mock import MagicMock

        scorer = JobScorer(resume_manager=MagicMock())
        js = JobScore(
            decision=ScoreDecision.USER_REVIEW,
            resume_label="test",
            dimensions=_dims(),
        )
        updated = scorer.override_decision(js, ScoreDecision.AUTO_APPLY)
        assert updated.decision == ScoreDecision.AUTO_APPLY
        assert updated.dimensions == js.dimensions
