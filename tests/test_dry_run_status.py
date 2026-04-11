"""Regression test for dry_run status assignment.

The bug: pipeline.apply_to_job set status='dry_run' whenever the
caller passed dry_run=True, IGNORING the actual success flag from
the platform's ApplyResult. External redirects, failed form walks,
missing apply buttons, and honeypot-blocked forms all got marked
as 'applied' (dry_run is counted as applied in the dashboard).

Every single 'apply' in the developer's applications.csv history
was actually a failure because of this.

The fix: dry_run status only applies when result.success is True.
Failures get status='failed' regardless of whether it was a
dry run or a real run.
"""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.fixture
def temp_csvs(tmp_path):
    from auto_applier.storage import repository
    from auto_applier.storage.models import Application, Followup, Job, SkillGap
    with patch.object(repository, "_CSV_MAP", {
        Job: tmp_path / "jobs.csv",
        Application: tmp_path / "applications.csv",
        SkillGap: tmp_path / "skill_gaps.csv",
        Followup: tmp_path / "followups.csv",
    }):
        yield tmp_path


def _run(coro):
    import asyncio
    return asyncio.run(coro)


class TestDryRunStatus:
    """Every cell of the (dry_run × success) matrix must produce the
    right status — applied/failed/dry_run. No more mixing them up."""

    def _build_platform(self, success: bool, reason: str = ""):
        from auto_applier.storage.models import ApplyResult
        platform = MagicMock()
        platform.source_id = "testsite"
        platform.display_name = "TestSite"
        platform.context.pages = []
        platform.apply_to_job = AsyncMock(return_value=ApplyResult(
            success=success,
            failure_reason=reason,
            fields_filled=0,
            fields_total=0,
            used_llm=False,
        ))
        platform.get_page = AsyncMock(return_value=MagicMock())
        return platform

    def test_dry_run_success_is_dry_run(self, temp_csvs):
        from auto_applier.orchestrator.pipeline import apply_to_job
        from auto_applier.storage.models import Job
        job = Job(
            job_id="j1", title="T", company="C", url="u",
            source="testsite", description="d",
        )
        platform = self._build_platform(success=True)
        # Patch reading_pause and random_delay to no-ops so the test
        # doesn't sleep
        with patch("auto_applier.orchestrator.pipeline.reading_pause",
                   new=AsyncMock()), \
             patch("auto_applier.orchestrator.pipeline.random_delay",
                   new=AsyncMock()):
            app = _run(apply_to_job(
                platform, job, "resume.pdf", "text", "r",
                personal_info={}, router=MagicMock(), dry_run=True,
            ))
        assert app.status == "dry_run"
        assert app.failure_reason == ""

    def test_dry_run_failure_is_failed(self, temp_csvs):
        """The real bug: external redirect during dry run was
        recorded as dry_run (= counted as applied)."""
        from auto_applier.orchestrator.pipeline import apply_to_job
        from auto_applier.storage.models import Job
        job = Job(
            job_id="j1", title="T", company="C", url="u",
            source="testsite", description="d",
        )
        platform = self._build_platform(
            success=False,
            reason="External application -- redirects to company site",
        )
        with patch("auto_applier.orchestrator.pipeline.reading_pause",
                   new=AsyncMock()), \
             patch("auto_applier.orchestrator.pipeline.random_delay",
                   new=AsyncMock()):
            app = _run(apply_to_job(
                platform, job, "resume.pdf", "text", "r",
                personal_info={}, router=MagicMock(), dry_run=True,
            ))
        assert app.status == "failed"
        assert "External application" in app.failure_reason

    def test_real_run_success_is_applied(self, temp_csvs):
        from auto_applier.orchestrator.pipeline import apply_to_job
        from auto_applier.storage.models import Job
        job = Job(
            job_id="j1", title="T", company="C", url="u",
            source="testsite", description="d",
        )
        platform = self._build_platform(success=True)
        with patch("auto_applier.orchestrator.pipeline.reading_pause",
                   new=AsyncMock()), \
             patch("auto_applier.orchestrator.pipeline.random_delay",
                   new=AsyncMock()):
            app = _run(apply_to_job(
                platform, job, "resume.pdf", "text", "r",
                personal_info={}, router=MagicMock(), dry_run=False,
            ))
        assert app.status == "applied"

    def test_real_run_failure_is_failed(self, temp_csvs):
        from auto_applier.orchestrator.pipeline import apply_to_job
        from auto_applier.storage.models import Job
        job = Job(
            job_id="j1", title="T", company="C", url="u",
            source="testsite", description="d",
        )
        platform = self._build_platform(
            success=False, reason="Honeypot hang",
        )
        with patch("auto_applier.orchestrator.pipeline.reading_pause",
                   new=AsyncMock()), \
             patch("auto_applier.orchestrator.pipeline.random_delay",
                   new=AsyncMock()):
            app = _run(apply_to_job(
                platform, job, "resume.pdf", "text", "r",
                personal_info={}, router=MagicMock(), dry_run=False,
            ))
        assert app.status == "failed"
        assert app.failure_reason == "Honeypot hang"
