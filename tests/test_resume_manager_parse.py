"""Tests for ResumeManager._parse_dimensions — LLM response parser."""
from auto_applier.resume.manager import ResumeManager
from auto_applier.scoring.models import DEFAULT_DIMENSIONS


class TestParseDimensions:
    def test_full_response_parses_all_axes(self):
        result = {
            name: {"score": 7.0, "reason": f"{name} is fine"}
            for name, _ in DEFAULT_DIMENSIONS
        }
        dims = ResumeManager._parse_dimensions(result)
        assert len(dims) == len(DEFAULT_DIMENSIONS)
        assert {d.name for d in dims} == {name for name, _ in DEFAULT_DIMENSIONS}
        assert all(d.weight > 0 for d in dims)

    def test_missing_axes_still_parse_if_majority_present(self):
        # 4 of 7 present — above the floor of max(3, 3) = 3
        partial = {
            "skills":     {"score": 8, "reason": "ok"},
            "experience": {"score": 7, "reason": "ok"},
            "seniority":  {"score": 6, "reason": "ok"},
            "location":   {"score": 5, "reason": "ok"},
        }
        dims = ResumeManager._parse_dimensions(partial)
        assert len(dims) == 4

    def test_too_few_axes_returns_empty(self):
        # Only 2 present — below the floor
        sparse = {
            "skills":     {"score": 8, "reason": "ok"},
            "experience": {"score": 7, "reason": "ok"},
        }
        assert ResumeManager._parse_dimensions(sparse) == []

    def test_clamps_out_of_range_scores(self):
        result = {
            name: {"score": 99, "reason": "too high"}
            for name, _ in DEFAULT_DIMENSIONS
        }
        dims = ResumeManager._parse_dimensions(result)
        assert all(d.score == 10.0 for d in dims)

    def test_clamps_negative_scores(self):
        result = {
            name: {"score": -5, "reason": "too low"}
            for name, _ in DEFAULT_DIMENSIONS
        }
        dims = ResumeManager._parse_dimensions(result)
        assert all(d.score == 0.0 for d in dims)

    def test_ignores_non_numeric_scores(self):
        result = {
            name: {"score": "n/a", "reason": "bad"}
            for name, _ in DEFAULT_DIMENSIONS
        }
        assert ResumeManager._parse_dimensions(result) == []

    def test_ignores_non_dict_cells(self):
        result = {
            "skills":     8,  # not a dict
            "experience": {"score": 7, "reason": "ok"},
            "seniority":  {"score": 6, "reason": "ok"},
            "location":   {"score": 5, "reason": "ok"},
            "compensation": {"score": 5, "reason": "ok"},
        }
        dims = ResumeManager._parse_dimensions(result)
        assert len(dims) == 4
        assert "skills" not in {d.name for d in dims}

    def test_reason_preserved(self):
        result = {
            name: {"score": 7, "reason": f"reason for {name}"}
            for name, _ in DEFAULT_DIMENSIONS
        }
        dims = ResumeManager._parse_dimensions(result)
        skills = next(d for d in dims if d.name == "skills")
        assert skills.explanation == "reason for skills"

    def test_empty_response_returns_empty(self):
        assert ResumeManager._parse_dimensions({}) == []
