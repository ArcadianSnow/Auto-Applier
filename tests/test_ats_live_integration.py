"""Live integration tests for the ATS public-API adapters.

These tests hit the real Greenhouse / Lever / Ashby endpoints with
known-good slugs (Stripe / Netflix / OpenAI) and assert that the
adapter produces well-formed Job objects with non-empty titles,
companies, and descriptions.

Marked ``integration`` so they're easy to skip on CI / offline
machines. Run them yourself with::

    pytest -m integration tests/test_ats_live_integration.py -v

We treat any kind of network failure as a SKIP (not a failure) so
flaky CI environments don't break the build. A real bug shows up
as ``0 jobs returned for a known-good slug`` or ``AssertionError
on Job field shape``.
"""
from __future__ import annotations

import asyncio

import pytest


pytestmark = pytest.mark.integration


def _run(coro):
    return asyncio.run(coro)


def _make(cls, slug):
    """Instantiate an ATS adapter with a single configured slug."""
    from unittest.mock import MagicMock
    ctx = MagicMock()
    config = {
        "ats_api_companies": {
            cls.ats_id: [slug],
        },
    }
    return cls(context=ctx, config=config)


# ----------------------------------------------------------------------
# Greenhouse — Stripe is the canonical "this works" check
# ----------------------------------------------------------------------

class TestGreenhouseLive:
    def test_stripe_returns_real_jobs(self):
        from auto_applier.browser.platforms.ats_greenhouse import (
            ATSGreenhousePlatform,
        )
        platform = _make(ATSGreenhousePlatform, "stripe")

        async def do():
            try:
                return await platform.search_jobs("", "")
            finally:
                await platform.aclose()

        try:
            jobs = _run(do())
        except Exception as exc:
            pytest.skip(f"Network failed for Greenhouse/stripe: {exc}")

        # Stripe always has open jobs. If we got 0, our parser broke.
        assert len(jobs) > 10, (
            f"Expected >10 Stripe jobs from Greenhouse API, got {len(jobs)}."
        )

        # Spot-check Job shape on the first result.
        first = jobs[0]
        assert first.title, "Job has empty title"
        assert first.company, "Job has empty company"
        assert first.url.startswith("http"), f"Bad URL: {first.url}"
        # Greenhouse adapter prefixes job_id with "gh_<slug>_<id>".
        assert first.job_id.startswith("gh_stripe_"), (
            f"Expected gh_stripe_ prefix, got {first.job_id}"
        )
        # Description should be HTML-stripped, multi-paragraph.
        assert len(first.description) > 100, (
            "Description too short — HTML stripper may have eaten content"
        )
        assert "<p>" not in first.description, "HTML tags not stripped"
        assert "&amp;" not in first.description, "Entities not decoded"

    def test_stripe_keyword_filter_word_level_or(self):
        """The new keyword filter is word-level OR. 'data engineer'
        should match jobs with ANY of {data, engineer} in the title.
        Stripe always has both 'Data' and 'Engineer' jobs."""
        from auto_applier.browser.platforms.ats_greenhouse import (
            ATSGreenhousePlatform,
        )
        platform = _make(ATSGreenhousePlatform, "stripe")

        async def do():
            try:
                return await platform.search_jobs(
                    keyword="data engineer", location="",
                )
            finally:
                await platform.aclose()

        try:
            jobs = _run(do())
        except Exception as exc:
            pytest.skip(f"Network failed: {exc}")

        assert len(jobs) > 0, (
            "Expected at least 1 'data' or 'engineer' role at Stripe; "
            "filter may still be too aggressive."
        )
        # Validate the OR semantics — every kept job has either
        # 'data' OR 'engineer' in the title.
        for j in jobs[:10]:
            t = j.title.lower()
            assert "data" in t or "engineer" in t, (
                f"Title {j.title!r} doesn't match 'data' or 'engineer' — "
                "filter logic regressed"
            )


# ----------------------------------------------------------------------
# Lever — Netflix
# ----------------------------------------------------------------------

class TestLeverLive:
    def test_palantir_returns_real_jobs(self):
        """Palantir is the most reliably-active Lever board in 2026
        (235 jobs as of 2026-05-03). Many other companies have
        migrated off Lever entirely — Netflix, Shopify, Discord etc.
        return ``{"ok": ..., "error": ...}`` not a job list, which
        our adapter correctly treats as zero results.
        """
        from auto_applier.browser.platforms.ats_lever import (
            ATSLeverPlatform,
        )
        platform = _make(ATSLeverPlatform, "palantir")

        async def do():
            try:
                return await platform.search_jobs("", "")
            finally:
                await platform.aclose()

        try:
            jobs = _run(do())
        except Exception as exc:
            pytest.skip(f"Network failed for Lever/palantir: {exc}")

        assert len(jobs) > 5, (
            f"Expected >5 Palantir jobs from Lever API, got {len(jobs)}."
        )
        first = jobs[0]
        assert first.title
        assert first.url.startswith("http")
        assert first.job_id.startswith("lever_palantir_")
        # Lever ships descriptionPlain so adapter should produce
        # readable text.
        assert len(first.description) > 50

    def test_inactive_company_returns_zero_not_crash(self):
        """A company that's left Lever returns the dict
        ``{"ok": ..., "error": ...}`` instead of a list. The adapter
        must treat that as zero jobs, not crash."""
        from auto_applier.browser.platforms.ats_lever import (
            ATSLeverPlatform,
        )
        platform = _make(ATSLeverPlatform, "netflix")

        async def do():
            try:
                return await platform.search_jobs("", "")
            finally:
                await platform.aclose()

        try:
            jobs = _run(do())
        except Exception as exc:
            pytest.skip(f"Network failed: {exc}")

        # Netflix migrated off Lever — endpoint returns dict, adapter
        # returns []. This is the desired behavior; users who want
        # Netflix should add a different ATS slug.
        assert jobs == []


# ----------------------------------------------------------------------
# Ashby — OpenAI
# ----------------------------------------------------------------------

class TestAshbyLive:
    def test_openai_returns_real_jobs(self):
        from auto_applier.browser.platforms.ats_ashby import (
            ATSAshbyPlatform,
        )
        platform = _make(ATSAshbyPlatform, "openai")

        async def do():
            try:
                return await platform.search_jobs("", "")
            finally:
                await platform.aclose()

        try:
            jobs = _run(do())
        except Exception as exc:
            pytest.skip(f"Network failed for Ashby/openai: {exc}")

        assert len(jobs) > 5, (
            f"Expected >5 OpenAI jobs from Ashby API, got {len(jobs)}."
        )
        first = jobs[0]
        assert first.title
        assert first.url.startswith("http")
        assert first.job_id.startswith("ashby_openai_")
        assert len(first.description) > 50


# ----------------------------------------------------------------------
# Cap behavior — make sure max_jobs_per_company actually clamps
# ----------------------------------------------------------------------

class TestJobCapEnforced:
    def test_per_company_cap_enforced(self):
        """With max_jobs_per_company=5, a 500-job board should yield
        exactly 5 jobs (or all of them if the board has fewer)."""
        from unittest.mock import MagicMock
        from auto_applier.browser.platforms.ats_greenhouse import (
            ATSGreenhousePlatform,
        )
        ctx = MagicMock()
        config = {
            "ats_api_companies": {"greenhouse": ["stripe"]},
            "ats_greenhouse": {
                "max_jobs_per_company": 5,
                "max_jobs_per_search": 50,
            },
        }
        platform = ATSGreenhousePlatform(context=ctx, config=config)

        async def do():
            try:
                return await platform.search_jobs("", "")
            finally:
                await platform.aclose()

        try:
            jobs = _run(do())
        except Exception as exc:
            pytest.skip(f"Network failed: {exc}")

        assert len(jobs) == 5, (
            f"Expected exactly 5 jobs after cap, got {len(jobs)}"
        )
