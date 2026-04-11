"""Regression tests for fast-skip of external jobs.

Previously, an external-only job burned 20-70s of LLM cycles on
ghost check + archetype classify + multi-dim scoring BEFORE the
apply step discovered it was external and bailed. fetch_description
now runs the external check on the already-loaded page so the
engine can skip these before any LLM work.
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from auto_applier.orchestrator.pipeline import fetch_description
from auto_applier.storage.models import Job


def _mock_platform(
    description: str = "job description text",
    liveness_value: str = "live",
    is_external: bool = False,
):
    p = MagicMock()
    p.get_job_description = AsyncMock(return_value=description)

    async def _liveness(job, navigate=False):
        job.liveness = liveness_value
        return liveness_value

    async def _external(job):
        return is_external

    p.check_liveness = _liveness
    p.check_is_external = _external
    return p


def _run(coro):
    return asyncio.run(coro)


class TestFetchDescriptionFastSkip:
    def test_live_non_external_stays_live(self):
        job = Job(job_id="j1", title="T", company="C", url="u", source="indeed")
        platform = _mock_platform(
            description="long enough",
            liveness_value="live",
            is_external=False,
        )
        result = _run(fetch_description(platform, job))
        assert result.liveness == "live"
        assert result.description == "long enough"

    def test_external_is_flagged(self):
        job = Job(job_id="j1", title="T", company="C", url="u", source="indeed")
        platform = _mock_platform(
            description="apply on company site",
            liveness_value="live",
            is_external=True,
        )
        result = _run(fetch_description(platform, job))
        assert result.liveness == "external"

    def test_dead_skips_external_check(self):
        """Dead jobs shouldn't even run the external check — the
        short-circuit keeps pages we already know are gone from
        running extra queries."""
        external_called = {"count": 0}
        async def _external(job):
            external_called["count"] += 1
            return True

        job = Job(job_id="j1", title="T", company="C", url="u", source="indeed")
        platform = _mock_platform(
            description="this listing has expired",
            liveness_value="dead",
            is_external=False,
        )
        platform.check_is_external = _external
        result = _run(fetch_description(platform, job))
        assert result.liveness == "dead"
        assert external_called["count"] == 0

    def test_external_check_failure_is_swallowed(self):
        """If the external check itself raises, the job stays at
        whatever liveness was set — we don't want an exception in
        the diagnostic to abort the whole pipeline."""
        job = Job(job_id="j1", title="T", company="C", url="u", source="indeed")
        platform = _mock_platform(liveness_value="live")

        async def _boom(job):
            raise RuntimeError("selector crashed")

        platform.check_is_external = _boom
        result = _run(fetch_description(platform, job))
        assert result.liveness == "live"

    def test_description_persisted_before_external_check(self):
        """Even on an external job, we still want the description
        saved so the GUI can show it in the review panel."""
        job = Job(job_id="j1", title="T", company="C", url="u", source="indeed")
        platform = _mock_platform(
            description="real description text",
            is_external=True,
        )
        result = _run(fetch_description(platform, job))
        assert result.description == "real description text"
        assert result.liveness == "external"


class TestBasePlatformDefault:
    """The default check_is_external returns False so platforms
    that don't override it behave like pre-fix code."""

    def test_default_returns_false(self):
        from auto_applier.browser.base_platform import JobPlatform

        class _Stub(JobPlatform):
            source_id = "stub"
            display_name = "Stub"

            async def ensure_logged_in(self): return True
            async def search_jobs(self, k, l): return []
            async def get_job_description(self, job): return ""
            async def apply_to_job(self, job, path, dry_run=False): return None

        p = _Stub(context=None, config={})
        result = _run(p.check_is_external(Job(
            job_id="j", title="t", company="c", url="u", source="stub",
        )))
        assert result is False
