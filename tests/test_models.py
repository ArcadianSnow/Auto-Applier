"""Tests for storage models and repository."""
import os
import tempfile
import pytest
from pathlib import Path

from auto_applier.storage.models import Job, Application, SkillGap, ApplyResult


class TestModels:
    def test_job_creation(self):
        job = Job(job_id="123", title="Data Analyst", company="Acme", url="https://example.com")
        assert job.job_id == "123"
        assert job.title == "Data Analyst"
        assert job.found_at  # Should have auto-generated timestamp

    def test_application_defaults(self):
        app = Application(job_id="123")
        assert app.status == "applied"
        assert app.score == 0
        assert app.cover_letter_generated == False
        assert app.used_llm == False

    def test_skill_gap_creation(self):
        gap = SkillGap(job_id="123", field_label="Kubernetes", category="skill", resume_label="data_analyst")
        assert gap.field_label == "Kubernetes"
        assert gap.resume_label == "data_analyst"

    def test_apply_result(self):
        result = ApplyResult(success=True, gaps=[], resume_used="data_analyst", cover_letter_generated=True)
        assert result.success
        assert result.resume_used == "data_analyst"
        assert result.cover_letter_generated
