"""Regression tests for tightened apply-confirmation paths.

Tier 4 review (2026-05-03) flagged three pre-existing false-success
patterns in the same family as the ZR bug fixed in 8bb546c. Fixed:

  - ZR ``_check_success``: pruned loose phrases ("you've applied",
    "congratulations") that false-matched sidebar / profile chrome;
    added URL-pattern check first.
  - ZR empty-step fallback at MAX_FORM_STEPS: now requires URL
    corroboration (post-apply route OR navigation away from entry)
    before declaring 1-click apply success.
  - Dice apply path: replaced fall-through "assume success on no
    explicit signal" with ``_wait_for_submit_outcome`` poll borrowed
    from Indeed.

These tests pin the new contracts so no future refactor can quietly
revert to the loose behavior.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from auto_applier.browser.platforms.dice import DicePlatform
from auto_applier.browser.platforms.ziprecruiter import ZipRecruiterPlatform
from auto_applier.storage.models import Job


def _run(coro):
    return asyncio.run(coro)


def _make_zr():
    ctx = MagicMock()
    ctx.pages = [MagicMock()]
    return ZipRecruiterPlatform(context=ctx, config={})


def _make_dice():
    ctx = MagicMock()
    ctx.pages = [MagicMock()]
    return DicePlatform(context=ctx, config={})


def _job(jid="J1"):
    return Job(
        job_id=jid,
        source="test",
        title="Engineer",
        company="Acme",
        url="https://example.com",
    )


# ------------------------------------------------------------------
# ZR — URL-pattern signal in _check_success
# ------------------------------------------------------------------

class TestZrCheckSuccessUrlSignal:
    """All six post-apply URL fragments must trigger success without
    needing a body-text match. These are the strongest signal we
    have and the cheapest check."""

    @pytest.mark.parametrize("url", [
        "https://www.ziprecruiter.com/apply/successful?j=1",
        "https://www.ziprecruiter.com/apply/confirmation",
        "https://www.ziprecruiter.com/apply/thank-you",
        "https://www.ziprecruiter.com/post-apply",
        "https://www.ziprecruiter.com/thanks",
        "https://www.ziprecruiter.com/applied",
    ])
    def test_url_match_returns_true(self, url):
        platform = _make_zr()
        page = MagicMock()
        page.url = url
        page.inner_text = AsyncMock(return_value="")

        async def do():
            with patch.object(platform, "safe_query", return_value=None):
                return await platform._check_success(page)

        assert _run(do()) is True


# ------------------------------------------------------------------
# Dice — _wait_for_submit_outcome terminal states
# ------------------------------------------------------------------

class TestDiceSubmitOutcome:
    """The new Dice submit-outcome poller has three terminal states.
    Validate each independently. Replaces the old fall-through that
    silently assumed success when no signal appeared."""

    def test_explicit_success_short_circuits_poll(self):
        """When _check_success fires on the first poll iteration,
        return success immediately without waiting 15s."""
        platform = _make_dice()
        page = MagicMock()
        page.url = "https://www.dice.com/job-detail/123"
        page.query_selector = AsyncMock(return_value=None)
        page.inner_text = AsyncMock(return_value="x")

        async def do():
            with patch.object(platform, "_check_success", AsyncMock(return_value=True)), \
                 patch.object(platform, "_close_modal", AsyncMock()):
                return await platform._wait_for_submit_outcome(
                    page, _job(), pre_url="https://www.dice.com/job-detail/123",
                )

        result = _run(do())
        assert result.success is True
        assert result.failure_reason is None or result.failure_reason == ""

    def test_validation_error_returns_failure(self):
        """A visible validation error within the poll window is a
        terminal failure — not a 'try again' signal."""
        platform = _make_dice()
        page = MagicMock()
        page.url = "https://www.dice.com/job-detail/123"
        page.query_selector = AsyncMock(return_value=None)
        page.inner_text = AsyncMock(return_value="x")

        async def do():
            with patch.object(platform, "_check_success", AsyncMock(return_value=False)), \
                 patch.object(platform, "_scan_validation_errors",
                              AsyncMock(return_value="Phone number is required")):
                return await platform._wait_for_submit_outcome(
                    page, _job(), pre_url="https://www.dice.com/job-detail/123",
                )

        result = _run(do())
        assert result.success is False
        assert "validation" in (result.failure_reason or "").lower()
        assert "phone number" in (result.failure_reason or "").lower()

    def test_navigation_away_treated_as_probable_success(self):
        """When URL moves away from job-detail to a non-auth /
        non-captcha route, treat as probable success — Dice sometimes
        renders a confirmation page without our known selectors."""
        platform = _make_dice()
        page = MagicMock()
        # Pre-url is the modal entry; after submit the URL moved off
        # the job-detail page. Page.url is read by the poller.
        page.url = "https://www.dice.com/jobs/dashboard"
        page.query_selector = AsyncMock(return_value=None)
        page.inner_text = AsyncMock(return_value="x")

        async def do():
            with patch.object(platform, "_check_success", AsyncMock(return_value=False)), \
                 patch.object(platform, "_scan_validation_errors",
                              AsyncMock(return_value="")), \
                 patch.object(platform, "_close_modal", AsyncMock()):
                return await platform._wait_for_submit_outcome(
                    page, _job(),
                    pre_url="https://www.dice.com/job-detail/123",
                )

        result = _run(do())
        assert result.success is True

    def test_navigation_to_login_does_not_trigger_success(self):
        """An auth wall after submit means submission failed — must
        NOT be classified as probable success."""
        platform = _make_dice()
        page = MagicMock()
        page.url = "https://www.dice.com/auth/login?redirect=/apply"
        page.query_selector = AsyncMock(return_value=None)
        page.inner_text = AsyncMock(return_value="x")

        # Patch asyncio.sleep so the 15s poll completes instantly.
        with patch("auto_applier.browser.platforms.dice.asyncio.sleep",
                   AsyncMock(return_value=None)):
            async def do():
                with patch.object(platform, "_check_success", AsyncMock(return_value=False)), \
                     patch.object(platform, "_scan_validation_errors",
                                  AsyncMock(return_value="")):
                    return await platform._wait_for_submit_outcome(
                        page, _job(),
                        pre_url="https://www.dice.com/job-detail/123",
                    )
            result = _run(do())
        assert result.success is False
        assert "no success confirmation" in (result.failure_reason or "")

    def test_exhaustion_returns_failure_not_success(self):
        """The whole point of this fix: when nothing happens within
        15s, do NOT silently claim success. Return a structured
        failure so the user can verify on My Jobs."""
        platform = _make_dice()
        page = MagicMock()
        page.url = "https://www.dice.com/job-detail/123"
        page.query_selector = AsyncMock(return_value=None)
        page.inner_text = AsyncMock(return_value="x")

        # Patch asyncio.sleep to instant so the poll exhausts quickly.
        with patch("auto_applier.browser.platforms.dice.asyncio.sleep",
                   AsyncMock(return_value=None)):
            async def do():
                with patch.object(platform, "_check_success", AsyncMock(return_value=False)), \
                     patch.object(platform, "_scan_validation_errors",
                                  AsyncMock(return_value="")):
                    return await platform._wait_for_submit_outcome(
                        page, _job(),
                        pre_url="https://www.dice.com/job-detail/123",
                    )
            result = _run(do())
        assert result.success is False
        assert "no success confirmation" in (result.failure_reason or "")


# ------------------------------------------------------------------
# Dice — URL signal in _check_success
# ------------------------------------------------------------------

class TestDiceCheckSuccessUrlSignal:
    @pytest.mark.parametrize("url", [
        "https://www.dice.com/jobs/apply/success",
        "https://www.dice.com/jobs/apply/confirmation",
        "https://www.dice.com/post-apply",
        "https://www.dice.com/thanks",
        "https://www.dice.com/applied",
    ])
    def test_url_match_returns_true(self, url):
        platform = _make_dice()
        page = MagicMock()
        page.url = url
        page.inner_text = AsyncMock(return_value="")

        async def do():
            with patch.object(platform, "safe_query", return_value=None):
                return await platform._check_success(page)

        assert _run(do()) is True
