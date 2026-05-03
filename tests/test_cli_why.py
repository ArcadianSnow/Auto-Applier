"""Tests for ``cli why <job_id>`` — reasoning trace command.

The command reads ``dimensions_json`` from the persisted Application
row, decodes it, and renders the per-axis breakdown sorted by
weighted contribution. Tests cover:

  - Missing job: clean error message, no exception.
  - Job with no Application: explicit "never scored" message.
  - Application with malformed dimensions_json: graceful fallback.
  - Application with empty/missing dimensions: graceful fallback.
  - Application with full dimensions: each dimension rendered with
    score, weight, contribution, and explanation.
  - Multiple Applications for one job: most recent wins.
"""
from __future__ import annotations

import json as _json
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from auto_applier.main import cli


@pytest.fixture
def runner():
    return CliRunner()


def _detail(jobs=None, apps=None):
    return {
        "jobs": jobs or [],
        "applications": apps or [],
        "skill_gaps": [],
        "followups": [],
    }


class TestCliWhy:
    def test_missing_job(self, runner):
        with patch("auto_applier.analysis.observability.get_job_detail",
                   return_value=None):
            result = runner.invoke(cli, ["why", "nope"])
        assert result.exit_code == 0
        assert "No job with id" in result.output

    def test_job_without_application(self, runner):
        detail = _detail(jobs=[{"title": "T", "company": "C"}], apps=[])
        with patch("auto_applier.analysis.observability.get_job_detail",
                   return_value=detail):
            result = runner.invoke(cli, ["why", "j1"])
        assert result.exit_code == 0
        assert "never scored" in result.output

    def test_application_renders_dimensions_sorted_by_contribution(self, runner):
        dimensions = [
            {"name": "skills", "score": 9.0, "weight": 0.35,
             "explanation": "Strong Python and Postgres match."},
            {"name": "experience", "score": 6.0, "weight": 0.20,
             "explanation": "Slightly junior for the role."},
            {"name": "compensation", "score": 4.0, "weight": 0.05,
             "explanation": "Salary range below candidate floor."},
        ]
        app = {
            "applied_at": "2026-05-03T10:00:00",
            "status": "applied",
            "source": "indeed",
            "resume_used": "data_engineer",
            "score": 7,
            "dimensions_json": _json.dumps(dimensions),
            "outcome": "pending",
        }
        detail = _detail(
            jobs=[{"title": "Senior Data Eng", "company": "Acme"}],
            apps=[app],
        )
        with patch("auto_applier.analysis.observability.get_job_detail",
                   return_value=detail):
            result = runner.invoke(cli, ["why", "j1"])
        assert result.exit_code == 0
        # Header
        assert "Senior Data Eng" in result.output
        assert "Acme" in result.output
        assert "7 / 10" in result.output
        assert "data_engineer" in result.output
        # Dimensions all present
        assert "skills" in result.output
        assert "experience" in result.output
        assert "compensation" in result.output
        # Sorted by weighted contribution descending — skills
        # (9.0 * 0.35 = 3.15) leads experience (6.0 * 0.20 = 1.20)
        # leads compensation (4.0 * 0.05 = 0.20).
        skills_idx = result.output.find("skills")
        exp_idx = result.output.find("experience")
        comp_idx = result.output.find("compensation")
        assert 0 < skills_idx < exp_idx < comp_idx
        # Explanations rendered (wrapped) under each dimension
        assert "Strong Python and Postgres" in result.output
        assert "Salary range below" in result.output

    def test_malformed_dimensions_json(self, runner):
        app = {
            "applied_at": "2026-05-03T10:00:00",
            "status": "applied",
            "source": "indeed",
            "score": 5,
            "dimensions_json": "{not json}",
        }
        detail = _detail(
            jobs=[{"title": "T", "company": "C"}], apps=[app],
        )
        with patch("auto_applier.analysis.observability.get_job_detail",
                   return_value=detail):
            result = runner.invoke(cli, ["why", "j1"])
        assert result.exit_code == 0
        assert "malformed" in result.output

    def test_empty_dimensions_list(self, runner):
        app = {
            "applied_at": "2026-05-03",
            "status": "applied",
            "source": "indeed",
            "score": 5,
            "dimensions_json": "[]",
        }
        detail = _detail(
            jobs=[{"title": "T", "company": "C"}], apps=[app],
        )
        with patch("auto_applier.analysis.observability.get_job_detail",
                   return_value=detail):
            result = runner.invoke(cli, ["why", "j1"])
        assert result.exit_code == 0
        assert "No dimensional breakdown" in result.output

    def test_legacy_application_without_dimensions(self, runner):
        app = {
            "applied_at": "2026-04-01",
            "status": "applied",
            "source": "indeed",
            "score": 7,
            # No dimensions_json field at all
        }
        detail = _detail(
            jobs=[{"title": "T", "company": "C"}], apps=[app],
        )
        with patch("auto_applier.analysis.observability.get_job_detail",
                   return_value=detail):
            result = runner.invoke(cli, ["why", "j1"])
        assert result.exit_code == 0
        assert "No dimensional breakdown saved" in result.output

    def test_most_recent_application_wins(self, runner):
        """Continuous-run mode can produce multiple Application rows
        per job over time. The command should show the latest
        decision so the user sees the current state, not history."""
        old_app = {
            "applied_at": "2026-04-01T08:00:00",
            "status": "skipped",
            "source": "indeed",
            "resume_used": "old_resume",
            "score": 3,
            "dimensions_json": _json.dumps([
                {"name": "skills", "score": 3.0, "weight": 1.0},
            ]),
        }
        new_app = {
            "applied_at": "2026-05-03T10:00:00",
            "status": "applied",
            "source": "ats_greenhouse",
            "resume_used": "new_resume",
            "score": 8,
            "dimensions_json": _json.dumps([
                {"name": "skills", "score": 8.0, "weight": 1.0},
            ]),
        }
        detail = _detail(
            jobs=[{"title": "T", "company": "C"}],
            apps=[old_app, new_app],
        )
        with patch("auto_applier.analysis.observability.get_job_detail",
                   return_value=detail):
            result = runner.invoke(cli, ["why", "j1"])
        assert result.exit_code == 0
        assert "new_resume" in result.output
        assert "ats_greenhouse" in result.output
        # The older row's resume should NOT dominate the output —
        # we render a single (most recent) decision.
        assert result.output.count("8 / 10") >= 1
