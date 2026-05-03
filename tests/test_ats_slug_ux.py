"""Tests for the ATS slug-discovery UX helpers.

User feedback after the live wizard run: the bare "type slugs here"
text widgets were unintelligible without context. We now expose two
helpers that solve the discovery problem:

  - ``STARTER_PACK_SLUGS``: curated dict of well-known boards per
    ATS that the wizard's "Try popular companies" button loads in
    one click.
  - ``detect_ats_from_url``: pure parser that takes any careers
    URL and returns ``(ats_id, slug)`` so users can paste a real
    URL instead of researching what a slug is.

These tests cover the pure logic; the Tk button handlers are
exercised by the existing module-import canary in
test_wizard_sites_step.py.
"""
from __future__ import annotations

import pytest

from auto_applier.gui.steps.sites import (
    STARTER_PACK_SLUGS,
    detect_ats_from_url,
)


# ----------------------------------------------------------------------
# Starter pack content
# ----------------------------------------------------------------------

class TestStarterPackContent:
    """Curation rules for the starter pack — every entry must be
    a real, well-known company so the user immediately recognizes
    why those slugs are there."""

    def test_all_three_atses_present(self):
        assert set(STARTER_PACK_SLUGS.keys()) == {
            "greenhouse", "lever", "ashby",
        }

    def test_each_ats_has_at_least_one_entry(self):
        """A starter pack with zero entries for any ATS would mean
        clicking 'Try popular companies' produces nothing for that
        ATS. Lever notably has only 2 entries because most companies
        migrated off Lever in 2024-2025; we curate verified-active
        boards only (live-tested 2026-05-03)."""
        for ats_id, slugs in STARTER_PACK_SLUGS.items():
            assert len(slugs) >= 1, (
                f"{ats_id} starter pack is empty"
            )

    def test_starter_pack_total_is_substantive(self):
        """Across all three ATSes, the starter pack should give
        users at least a dozen verified boards so the "Try popular
        companies" button is genuinely useful."""
        total = sum(len(v) for v in STARTER_PACK_SLUGS.values())
        assert total >= 12, (
            f"Total starter slugs = {total}; aim for ≥12 across all 3 "
            "ATSes so users see real variety on click."
        )

    def test_slugs_are_lowercase_and_hyphen_safe(self):
        """Slugs are URL path segments — only ascii alphanum + hyphen
        + underscore is safe. ATS APIs reject mixed-case sometimes."""
        import re
        valid = re.compile(r"^[a-z0-9_-]+$")
        for ats_id, slugs in STARTER_PACK_SLUGS.items():
            for slug in slugs:
                assert valid.match(slug), (
                    f"{ats_id}/{slug!r} doesn't match expected slug "
                    f"shape [a-z0-9_-]+"
                )

    def test_no_duplicate_slugs_within_one_ats(self):
        for ats_id, slugs in STARTER_PACK_SLUGS.items():
            assert len(slugs) == len(set(slugs)), (
                f"{ats_id} has duplicate slugs: {slugs}"
            )

    def test_starter_pack_is_a_dict_of_lists(self):
        """Stable shape — the wizard reads `STARTER_PACK_SLUGS[ats]`
        and iterates. Lists keep the curation order visible (most
        recognizable first)."""
        assert isinstance(STARTER_PACK_SLUGS, dict)
        for v in STARTER_PACK_SLUGS.values():
            assert isinstance(v, list)


# ----------------------------------------------------------------------
# URL detection — Greenhouse
# ----------------------------------------------------------------------

class TestDetectGreenhouseUrls:
    @pytest.mark.parametrize("url,expected_slug", [
        ("https://boards.greenhouse.io/stripe", "stripe"),
        ("https://boards.greenhouse.io/stripe/jobs/1234567", "stripe"),
        ("https://boards.greenhouse.io/stripe/jobs/1234567?gh_src=foo", "stripe"),
        ("http://boards.greenhouse.io/stripe", "stripe"),  # http (not https)
        # API host
        ("https://boards-api.greenhouse.io/v1/boards/airbnb/jobs", "airbnb"),
        # Modern host variant
        ("https://job-boards.greenhouse.io/discord/jobs/8765", "discord"),
    ])
    def test_recognized(self, url, expected_slug):
        result = detect_ats_from_url(url)
        assert result is not None
        ats, slug = result
        assert ats == "greenhouse"
        assert slug == expected_slug


class TestDetectLeverUrls:
    @pytest.mark.parametrize("url,expected_slug", [
        ("https://jobs.lever.co/netflix", "netflix"),
        ("https://jobs.lever.co/netflix/abcd-1234", "netflix"),
        ("https://jobs.lever.co/netflix/abcd-1234/apply", "netflix"),
        ("https://api.lever.co/v0/postings/shopify?mode=json", "shopify"),
    ])
    def test_recognized(self, url, expected_slug):
        result = detect_ats_from_url(url)
        assert result is not None
        ats, slug = result
        assert ats == "lever"
        assert slug == expected_slug


class TestDetectAshbyUrls:
    @pytest.mark.parametrize("url,expected_slug", [
        ("https://jobs.ashbyhq.com/openai", "openai"),
        ("https://jobs.ashbyhq.com/openai/abc-123-def", "openai"),
        ("https://api.ashbyhq.com/posting-api/job-board/ramp", "ramp"),
    ])
    def test_recognized(self, url, expected_slug):
        result = detect_ats_from_url(url)
        assert result is not None
        ats, slug = result
        assert ats == "ashby"
        assert slug == expected_slug


# ----------------------------------------------------------------------
# Negative cases — must NOT misclassify
# ----------------------------------------------------------------------

class TestDetectRejectsUnknownUrls:
    @pytest.mark.parametrize("url", [
        "",
        "   ",
        "https://example.com/jobs",
        "https://www.linkedin.com/jobs/view/123",
        "https://www.indeed.com/viewjob?jk=abc",
        "https://www.dice.com/job-detail/abc",
        "https://careers.boozallen.com/careers/JobDetail",
        # ATS-like but not real:
        "https://greenhouse.example.com/stripe",
        "https://example.com/?continue=https://boards.greenhouse.io/stripe",
        # Garbage
        "not even a url",
        "javascript:alert(1)",
    ])
    def test_unrecognized_returns_none(self, url):
        assert detect_ats_from_url(url) is None


class TestDetectInputCleaning:
    """Users will paste URLs with surrounding garbage — quotes, angle
    brackets from email, trailing punctuation. Tolerate it."""

    @pytest.mark.parametrize("dirty,clean_slug", [
        (' https://boards.greenhouse.io/stripe ', "stripe"),
        ('"https://boards.greenhouse.io/stripe"', "stripe"),
        ("'https://boards.greenhouse.io/stripe'", "stripe"),
        ("<https://boards.greenhouse.io/stripe>", "stripe"),
        ("https://boards.greenhouse.io/stripe\n", "stripe"),
    ])
    def test_strips_surrounding_chars(self, dirty, clean_slug):
        result = detect_ats_from_url(dirty)
        assert result is not None
        assert result[1] == clean_slug

    def test_empty_returns_none(self):
        assert detect_ats_from_url("") is None
        assert detect_ats_from_url(None) is None  # type: ignore[arg-type]


class TestDetectCaseInsensitive:
    """Some users paste URLs after a redirect that uppercased the
    host. The slug itself is case-sensitive (ATS APIs treat
    boards/Stripe differently from boards/stripe), so we preserve
    its case while accepting any-case host."""

    def test_uppercase_host_accepted(self):
        result = detect_ats_from_url("https://BOARDS.GREENHOUSE.IO/Stripe")
        assert result is not None
        assert result[0] == "greenhouse"
        # Slug case preserved as written.
        assert result[1] == "Stripe"
