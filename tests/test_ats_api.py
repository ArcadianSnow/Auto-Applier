"""Tests for ATS public-API discovery adapters.

ATS adapters are network-driven and need careful mocking. We test
three things per adapter:

  1. **Schema parsing** — given a realistic JSON response, do we
     produce a Job with title / company / url / description set?
  2. **Resilience** — malformed entries don't crash; missing fields
     don't kill sibling jobs.
  3. **Filtering** — keyword/location filters apply.

Plus base-class tests for the configuration shape and HTTP failure
paths (404, network error) — those behaviors are shared across all
ATSes.

We mock httpx via the ``MockTransport`` shipped with httpx itself
rather than monkey-patching, so behaviour matches real httpx
semantics (raise_for_status, follow_redirects, etc.).
"""
from __future__ import annotations

import asyncio
import json
from unittest.mock import MagicMock, patch

import httpx
import pytest

from auto_applier.browser.platforms.ats_api_base import ATSAPIPlatform
from auto_applier.browser.platforms.ats_ashby import ATSAshbyPlatform
from auto_applier.browser.platforms.ats_greenhouse import ATSGreenhousePlatform
from auto_applier.browser.platforms.ats_lever import ATSLeverPlatform
from auto_applier.storage.models import Job


def _run(coro):
    return asyncio.run(coro)


def _make_platform(cls, config: dict | None = None):
    ctx = MagicMock()
    return cls(context=ctx, config=config or {})


def _mock_client(response_map: dict[str, tuple[int, dict | list]]):
    """Build an httpx.AsyncClient backed by MockTransport.

    ``response_map`` maps URL substrings to (status_code, json_body)
    tuples. The first matching substring wins. Lets each test
    declare exactly the URLs it expects to be hit.
    """
    def handler(request: httpx.Request) -> httpx.Response:
        for fragment, (status, body) in response_map.items():
            if fragment in str(request.url):
                return httpx.Response(status, json=body)
        return httpx.Response(404, json={"error": "no fixture for url"})
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


# ------------------------------------------------------------------
# Greenhouse
# ------------------------------------------------------------------

GH_FIXTURE = {
    "jobs": [
        {
            "id": 1234567,
            "title": "Senior Backend Engineer",
            "absolute_url": "https://boards.greenhouse.io/stripe/jobs/1234567",
            "location": {"name": "San Francisco, CA"},
            "departments": [{"name": "Engineering"}],
            "offices": [{"name": "San Francisco"}],
            "content": "<p>Stripe is hiring a backend engineer.</p>"
                       "<ul><li>Python &amp; Go</li><li>Postgres</li></ul>",
            "company_name": "Stripe",
        },
        {
            "id": 1234568,
            "title": "Product Designer",
            "absolute_url": "https://boards.greenhouse.io/stripe/jobs/1234568",
            "location": {"name": "Remote"},
            "content": "<p>Design beautiful payment flows.</p>",
        },
        # Malformed — missing id. Should be skipped, not crash.
        {"title": "no id here"},
    ]
}


class TestGreenhouseAdapter:
    def test_parses_full_payload(self):
        platform = _make_platform(
            ATSGreenhousePlatform,
            {"ats_api_companies": {"greenhouse": ["stripe"]}},
        )

        async def do():
            client = _mock_client({"stripe": (200, GH_FIXTURE)})
            jobs = await platform.fetch_company_jobs(client, "stripe")
            await client.aclose()
            return jobs

        jobs = _run(do())
        assert len(jobs) == 2  # malformed entry dropped
        assert jobs[0].title == "Senior Backend Engineer"
        assert jobs[0].company == "Stripe"
        assert "boards.greenhouse.io/stripe" in jobs[0].url
        assert "Python" in jobs[0].description
        assert "&amp;" not in jobs[0].description  # entity decoded
        assert "<p>" not in jobs[0].description  # tags stripped
        assert "Location: San Francisco" in jobs[0].description
        # Job id namespaced so it can't collide with another ATS slug
        # using the same numeric id.
        assert jobs[0].job_id.startswith("gh_stripe_")

    def test_falls_back_to_humanized_slug_when_company_missing(self):
        platform = _make_platform(ATSGreenhousePlatform)
        fixture = {"jobs": [{
            "id": 99,
            "title": "Engineer",
            "absolute_url": "https://example.com",
            "content": "<p>desc</p>",
            # No company_name
        }]}

        async def do():
            client = _mock_client({"my-cool-co": (200, fixture)})
            jobs = await platform.fetch_company_jobs(client, "my-cool-co")
            await client.aclose()
            return jobs

        jobs = _run(do())
        assert jobs[0].company == "My Cool Co"

    def test_search_jobs_uses_configured_companies(self):
        platform = _make_platform(
            ATSGreenhousePlatform,
            {"ats_api_companies": {"greenhouse": ["stripe", "github"]}},
        )

        async def do():
            with patch.object(platform, "fetch_company_jobs") as m:
                async def fake_fetch(client, slug):
                    return [Job(
                        job_id=f"gh_{slug}_1",
                        title=f"Job at {slug}",
                        company=slug.title(),
                        url=f"https://{slug}.example",
                        description="x",
                    )]
                m.side_effect = fake_fetch
                jobs = await platform.search_jobs("", "")
                return jobs, m

        jobs, m = _run(do())
        assert len(jobs) == 2
        assert m.call_count == 2

    def test_empty_company_list_returns_empty(self):
        platform = _make_platform(ATSGreenhousePlatform)

        async def do():
            return await platform.search_jobs("python", "")

        assert _run(do()) == []

    def test_404_on_one_slug_doesnt_kill_others(self):
        """Wrong slug returns 404; sibling companies should still
        produce jobs. This is critical because users mistype slugs."""
        platform = _make_platform(
            ATSGreenhousePlatform,
            {"ats_api_companies": {"greenhouse": ["good", "broken"]}},
        )

        async def do():
            async def patched_fetch(client, slug):
                if slug == "broken":
                    raise httpx.HTTPStatusError(
                        "404", request=MagicMock(),
                        response=MagicMock(status_code=404),
                    )
                return [Job(
                    job_id=f"gh_{slug}_1", title="ok", company=slug,
                    url="https://x.com", description="d",
                )]
            with patch.object(platform, "fetch_company_jobs",
                              side_effect=patched_fetch):
                return await platform.search_jobs("", "")

        jobs = _run(do())
        assert len(jobs) == 1
        assert jobs[0].company == "good"

    def test_keyword_filter(self):
        platform = _make_platform(
            ATSGreenhousePlatform,
            {"ats_api_companies": {"greenhouse": ["stripe"]}},
        )

        async def do():
            client = _mock_client({"stripe": (200, GH_FIXTURE)})
            with patch.object(platform, "_get_http", return_value=client):
                jobs = await platform.search_jobs("backend", "")
            await client.aclose()
            return jobs

        jobs = _run(do())
        # "Backend" matches the engineer post but not the designer post.
        assert len(jobs) == 1
        assert "Backend" in jobs[0].title


# ------------------------------------------------------------------
# Lever
# ------------------------------------------------------------------

LEVER_FIXTURE = [
    {
        "id": "abcd-1234",
        "text": "Staff Software Engineer",
        "hostedUrl": "https://jobs.lever.co/netflix/abcd-1234",
        "applyUrl": "https://jobs.lever.co/netflix/abcd-1234/apply",
        "categories": {
            "team": "Engineering",
            "location": "Los Gatos",
            "commitment": "Full-time",
        },
        "descriptionPlain": "Build the streaming backend.",
        "lists": [
            {"text": "Requirements", "content": "<ul><li>Java</li></ul>"},
        ],
        "additionalPlain": "We are an EEO employer.",
    },
    {
        # No id — must be skipped
        "text": "broken",
    },
]


class TestLeverAdapter:
    def test_parses_payload(self):
        platform = _make_platform(ATSLeverPlatform)

        async def do():
            client = _mock_client({"netflix": (200, LEVER_FIXTURE)})
            jobs = await platform.fetch_company_jobs(client, "netflix")
            await client.aclose()
            return jobs

        jobs = _run(do())
        assert len(jobs) == 1
        assert jobs[0].title == "Staff Software Engineer"
        assert jobs[0].company == "Netflix"
        assert "Los Gatos" in jobs[0].description
        assert "Build the streaming backend" in jobs[0].description
        assert "Java" in jobs[0].description  # list section flattened in
        assert "EEO" in jobs[0].description
        assert "lever.co/netflix/abcd-1234" in jobs[0].url
        assert jobs[0].job_id == "lever_netflix_abcd-1234"

    def test_prefers_descriptionPlain_over_html(self):
        platform = _make_platform(ATSLeverPlatform)
        fixture = [{
            "id": "x", "text": "X",
            "hostedUrl": "https://x",
            "descriptionPlain": "PLAIN VERSION",
            "description": "<p>HTML version</p>",
        }]

        async def do():
            client = _mock_client({"co": (200, fixture)})
            jobs = await platform.fetch_company_jobs(client, "co")
            await client.aclose()
            return jobs

        jobs = _run(do())
        assert "PLAIN VERSION" in jobs[0].description
        assert "HTML version" not in jobs[0].description


# ------------------------------------------------------------------
# Ashby
# ------------------------------------------------------------------

ASHBY_FIXTURE = {
    "jobs": [
        {
            "id": "uuid-aaa",
            "title": "Research Engineer",
            "department": "Research",
            "team": "Alignment",
            "location": "San Francisco",
            "employmentType": "FullTime",
            "jobUrl": "https://jobs.ashbyhq.com/openai/uuid-aaa",
            "descriptionPlain": "Work on safety research.",
            "compensation": {
                "compensationTierSummary": "$300k - $450k",
            },
        },
    ]
}


class TestAshbyAdapter:
    def test_parses_payload(self):
        platform = _make_platform(ATSAshbyPlatform)

        async def do():
            client = _mock_client({"openai": (200, ASHBY_FIXTURE)})
            jobs = await platform.fetch_company_jobs(client, "openai")
            await client.aclose()
            return jobs

        jobs = _run(do())
        assert len(jobs) == 1
        assert jobs[0].title == "Research Engineer"
        assert jobs[0].company == "Openai"
        assert "Compensation: $300k - $450k" in jobs[0].description
        assert "Department: Research" in jobs[0].description
        assert "Team: Alignment" in jobs[0].description
        assert "Work on safety research" in jobs[0].description
        assert jobs[0].job_id == "ashby_openai_uuid-aaa"


# ------------------------------------------------------------------
# Base-class behavior shared across ATSes
# ------------------------------------------------------------------

class TestATSBaseConfig:
    """The dict-shape vs. list-shape config tolerance is one of the
    sharpest edges in the adapter — users will hand-edit JSON. We
    pin both shapes."""

    def test_dict_shape(self):
        platform = _make_platform(
            ATSGreenhousePlatform,
            {"ats_api_companies": {"greenhouse": ["stripe", "github"]}},
        )
        assert platform._configured_companies() == ["stripe", "github"]

    def test_list_shape_legacy(self):
        platform = _make_platform(
            ATSGreenhousePlatform,
            {"ats_api_companies": [
                {"ats": "greenhouse", "company": "stripe"},
                {"ats": "lever", "company": "netflix"},  # different ATS, ignored
                {"ats": "Greenhouse", "company": "github"},  # case-insensitive
            ]},
        )
        assert platform._configured_companies() == ["stripe", "github"]

    def test_dedup_preserves_order(self):
        platform = _make_platform(
            ATSGreenhousePlatform,
            {"ats_api_companies": {"greenhouse": ["stripe", "Stripe", "github", "stripe"]}},
        )
        assert platform._configured_companies() == ["stripe", "github"]

    def test_unknown_ats_returns_empty(self):
        platform = _make_platform(
            ATSGreenhousePlatform,
            {"ats_api_companies": {"workable": ["x"]}},  # different ATS
        )
        assert platform._configured_companies() == []


class TestATSBaseDiscoveryOnly:
    def test_discovery_only_flag_set(self):
        for cls in (ATSGreenhousePlatform, ATSLeverPlatform, ATSAshbyPlatform):
            platform = _make_platform(cls)
            assert platform.discovery_only is True
            assert platform.discovery_only_reason

    def test_apply_to_job_returns_failure(self):
        """ATS adapters never apply — they're discovery-only. The
        engine short-circuits before this is reached, but the method
        must still return a failure result if called."""
        platform = _make_platform(ATSGreenhousePlatform)
        job = Job(
            job_id="gh_x_1", title="t", company="c",
            url="https://x", description="d",
        )

        async def do():
            return await platform.apply_to_job(job, "/path/resume.pdf")

        result = _run(do())
        assert result.success is False
        assert "manual" in result.failure_reason.lower()

    def test_ensure_logged_in_returns_true(self):
        for cls in (ATSGreenhousePlatform, ATSLeverPlatform, ATSAshbyPlatform):
            platform = _make_platform(cls)
            assert _run(platform.ensure_logged_in()) is True

    def test_get_job_description_returns_cached(self):
        """Description is populated by search_jobs; get_job_description
        is a pass-through. Used by the engine after search."""
        platform = _make_platform(ATSGreenhousePlatform)
        job = Job(
            job_id="gh_x_1", title="t", company="c",
            url="https://x",
            description="cached description",
        )

        async def do():
            return await platform.get_job_description(job)

        assert _run(do()) == "cached description"


# ------------------------------------------------------------------
# Registry
# ------------------------------------------------------------------

class TestRegistry:
    """The platform registry must contain all three ATS adapters
    under their namespaced keys so users can enable them in
    user_config.json's ``enabled_platforms`` list.
    """

    def test_all_three_registered(self):
        from auto_applier.browser.platforms import PLATFORM_REGISTRY
        assert "ats_greenhouse" in PLATFORM_REGISTRY
        assert "ats_lever" in PLATFORM_REGISTRY
        assert "ats_ashby" in PLATFORM_REGISTRY

    def test_browser_platforms_still_registered(self):
        """Adding ATS adapters must not have displaced browser ones."""
        from auto_applier.browser.platforms import PLATFORM_REGISTRY
        assert "linkedin" in PLATFORM_REGISTRY
        assert "indeed" in PLATFORM_REGISTRY
        assert "dice" in PLATFORM_REGISTRY
        assert "ziprecruiter" in PLATFORM_REGISTRY
