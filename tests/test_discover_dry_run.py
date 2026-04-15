"""Regression tests for discover_jobs dry-run bypass.

Real runs use dedup to avoid re-applying to the same listing.
Dry runs need to REPROCESS every job so users can iterate on the
pipeline without constantly wiping jobs.csv.
"""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from auto_applier.orchestrator.pipeline import discover_jobs
from auto_applier.storage import repository
from auto_applier.storage.models import Application, Followup, Job, SkillGap


@pytest.fixture
def temp_csvs(tmp_path):
    with patch.object(repository, "_CSV_MAP", {
        Job: tmp_path / "jobs.csv",
        Application: tmp_path / "applications.csv",
        SkillGap: tmp_path / "skill_gaps.csv",
        Followup: tmp_path / "followups.csv",
    }):
        yield tmp_path


def _fake_platform(jobs_to_return: list[Job]):
    p = MagicMock()
    p.source_id = "indeed"
    p.search_jobs = AsyncMock(return_value=jobs_to_return)
    return p


def _run(coro):
    import asyncio
    return asyncio.run(coro)


class TestRealRunDedup:
    """Real runs must skip jobs already in the store."""

    def test_filters_previously_seen_canonical(self, temp_csvs):
        # Pre-populate: one job from a previous run, AND an Application
        # row marking it as processed. Phase A semantics: a Job alone
        # is not enough to dedupe — it must have been scored.
        repository.save(Job(
            job_id="ind-old", title="Data Analyst", company="Acme Inc",
            url="u", source="indeed",
        ))
        repository.save(Application(
            job_id="ind-old", status="skipped", source="indeed",
        ))
        # Now search finds the same job again
        new_job = Job(
            job_id="ind-new", title="Data Analyst", company="Acme Inc",
            url="u", source="indeed",
        )
        platform = _fake_platform([new_job])
        result = _run(discover_jobs(platform, "data analyst", "remote", dry_run=False))
        assert result == []  # filtered

    def test_unscored_previously_saved_job_does_not_filter(self, temp_csvs):
        """Phase A: a Job saved but never scored (budget ran out in a
        prior cycle, etc.) must NOT dedupe — continuous-run mode needs
        to come back to it next cycle."""
        repository.save(Job(
            job_id="ind-old", title="Data Analyst", company="Acme Inc",
            url="u", source="indeed",
        ))
        # No Application saved → not processed
        new_job = Job(
            job_id="ind-new", title="Data Analyst", company="Acme Inc",
            url="u", source="indeed",
        )
        platform = _fake_platform([new_job])
        result = _run(discover_jobs(platform, "data analyst", "remote", dry_run=False))
        assert len(result) == 1
        assert result[0].job_id == "ind-new"

    def test_filters_already_applied(self, temp_csvs):
        repository.save(Application(
            job_id="ind-1", status="applied", source="indeed",
        ))
        platform = _fake_platform([
            Job(job_id="ind-1", title="T", company="C", url="u", source="indeed"),
        ])
        result = _run(discover_jobs(platform, "kw", "loc", dry_run=False))
        assert result == []


class TestDryRunBypass:
    """Dry runs must NOT consult persisted state for dedup."""

    def test_reprocesses_previously_saved_jobs(self, temp_csvs):
        """The real-world bug: Indeed found 99 jobs but processed 0
        because every canonical_hash was already in jobs.csv from
        prior runs. Dry runs must not get filtered by this."""
        repository.save(Job(
            job_id="ind-old", title="Data Analyst", company="Acme Inc",
            url="u", source="indeed",
        ))
        new_job = Job(
            job_id="ind-new", title="Data Analyst", company="Acme Inc",
            url="u", source="indeed",
        )
        platform = _fake_platform([new_job])
        result = _run(discover_jobs(platform, "data analyst", "remote", dry_run=True))
        assert len(result) == 1
        assert result[0].job_id == "ind-new"

    def test_still_dedups_within_batch(self, temp_csvs):
        """Even dry runs shouldn't process the same listing twice in
        one run — but they should still reprocess across runs."""
        job_a = Job(
            job_id="ind-a", title="Data Analyst", company="Acme",
            url="u", source="indeed",
        )
        job_b = Job(
            job_id="ind-b", title="Data Analyst", company="Acme",
            url="u", source="indeed",
        )
        # Same canonical hash — they're the same job cross-posted
        assert job_a.canonical_hash == job_b.canonical_hash
        platform = _fake_platform([job_a, job_b])
        result = _run(discover_jobs(platform, "kw", "loc", dry_run=True))
        assert len(result) == 1  # second one filtered as batch dup

    def test_ignores_applied_history(self, temp_csvs):
        """Jobs that were 'applied' in a previous run still get
        reprocessed in a dry run."""
        repository.save(Application(
            job_id="ind-1", status="applied", source="indeed",
        ))
        new_job = Job(
            job_id="ind-1", title="T", company="C", url="u", source="indeed",
        )
        platform = _fake_platform([new_job])
        result = _run(discover_jobs(platform, "kw", "loc", dry_run=True))
        assert len(result) == 1  # not filtered
