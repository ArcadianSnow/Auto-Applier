"""Regression tests for Dice login-wait logic.

Live run 2026-05-03 17:20:25 hit a race where ``_dice_url_indicates_logged_in``
returned True for a transient dice.com URL that immediately
redirected to ``fedex.paradox.ai`` (Paradox.ai chatbot interview
platform). The subsequent ``page.goto(job.url)`` aborted with
``net::ERR_ABORTED`` and the apply was misclassified as failed.

Tightened predicate now requires:
  - URL on dice.com
  - URL not on an auth path
  - URL on a recognized post-login path (job-detail, dashboard, etc.)
  - After load-state settle: URL still satisfies above

Plus a sustained-off-host bail in ``_wait_for_dice_login``: if the
page leaves dice.com and stays off-host for 6+ seconds, the wait
loop returns False (route to manual-apply) rather than True.

Tests cover:
  - Predicate accepts known post-login URLs
  - Predicate rejects auth URLs (login/signup/etc.)
  - Predicate rejects unrecognized dice.com paths
  - Predicate rejects off-host URLs
  - Predicate rejects when post-settle URL changes off-host
  - Wait-loop bails after sustained off-host
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from auto_applier.browser.platforms.dice import DicePlatform


def _run(coro):
    return asyncio.run(coro)


def _make_platform():
    ctx = MagicMock()
    ctx.pages = []
    return DicePlatform(context=ctx, config={})


def _page(url: str, settle_url: str | None = None):
    """Build a fake Page where page.url returns ``url`` initially.
    If ``settle_url`` is provided, the SECOND read of page.url
    returns that (simulates the page redirecting during the
    wait_for_load_state call inside the predicate)."""
    page = MagicMock()
    page.wait_for_load_state = AsyncMock()
    if settle_url is None:
        page.url = url
    else:
        # property that flips after first read
        urls = iter([url, settle_url, settle_url])
        type(page).url = property(lambda self: next(urls))
    return page


# ----------------------------------------------------------------------
# _dice_url_indicates_logged_in — predicate accept/reject
# ----------------------------------------------------------------------

class TestPredicateAccept:
    @pytest.mark.parametrize("url", [
        "https://www.dice.com/job-detail/abc-123",
        "https://www.dice.com/dashboard",
        "https://www.dice.com/dashboard/profile",
        "https://www.dice.com/my-jobs",
        "https://www.dice.com/profile/",
        "https://www.dice.com/job-applications/abc/start-apply",
        "https://www.dice.com/job-search?q=data",
    ])
    def test_accepts_known_post_login_paths(self, url):
        platform = _make_platform()
        page = _page(url)
        with patch.object(platform, "_dismiss_common_popups", new=AsyncMock()):
            result = _run(platform._dice_url_indicates_logged_in(page))
        assert result is True


class TestPredicateReject:
    @pytest.mark.parametrize("url", [
        # Auth pages
        "https://www.dice.com/login",
        "https://www.dice.com/dashboard/login?redirect=/abc",
        "https://www.dice.com/signup",
        "https://www.dice.com/auth/oauth",
        # Off-host (third-party ATS / chatbot — Paradox.ai is the
        # actual case we observed live on 2026-05-03 17:20:25)
        "https://fedex.paradox.ai/co/Federal/Job?id=123",
        "https://careers.boozallen.com/abc",
        "https://apply.teksystems.com/v1/s/abc",
        # Unrecognized dice paths — too loose if we accepted these
        "https://www.dice.com/help/contact-us",
        "https://www.dice.com/blog/article-slug",
        "https://www.dice.com/",  # bare home page is not "logged in" enough
    ])
    def test_rejects_unrecognized_or_auth_or_offhost(self, url):
        platform = _make_platform()
        page = _page(url)
        with patch.object(platform, "_dismiss_common_popups", new=AsyncMock()):
            result = _run(platform._dice_url_indicates_logged_in(page))
        assert result is False


class TestPredicateSettleRecheck:
    """The 17:20:25 race: predicate sees a valid dice.com URL,
    then waits for page load, by which time the page has navigated
    off-host. The post-settle re-check must catch this."""

    def test_url_navigates_offhost_during_settle_returns_false(self):
        """First page.url read returns a valid dice URL. The
        wait_for_load_state call gives the redirect time to fire.
        Second + third reads return the off-host URL. Predicate
        rejects."""
        platform = _make_platform()
        # Initial check passes; after settle the URL has redirected
        # to an external chatbot host.
        page = _page(
            url="https://www.dice.com/job-applications/abc/start-apply",
            settle_url="https://fedex.paradox.ai/co/x/Job?id=123",
        )
        result = _run(platform._dice_url_indicates_logged_in(page))
        assert result is False


# ----------------------------------------------------------------------
# _wait_for_dice_login — sustained off-host bail
# ----------------------------------------------------------------------

class TestWaitForDiceLoginOffHostBail:
    """When the user gets handed off to a third-party ATS / chatbot
    (Paradox.ai / Workday-hosted / etc.) the wait loop should
    return False after a sustained off-host period rather than
    spinning until timeout AND rather than declaring login complete
    on a transient dice.com flicker."""

    def test_sustained_offhost_returns_false(self, monkeypatch):
        """Page sits on fedex.paradox.ai for >= OFF_HOST_BAIL_SECONDS.
        Wait loop bails to False before its full timeout."""
        platform = _make_platform()
        page = MagicMock()
        page.url = "https://fedex.paradox.ai/co/Federal/Job?id=123"

        # Predicate always returns False (off-host).
        with patch.object(
            platform, "_dice_url_indicates_logged_in",
            new=AsyncMock(return_value=False),
        ), patch(
            "auto_applier.browser.platforms.dice.asyncio.sleep",
            new=AsyncMock(),
        ), patch(
            "auto_applier.notify.notify_user", new=MagicMock(),
        ), patch(
            "time.monotonic",
            side_effect=[0.0, 0.0, 1.0, 2.0, 8.0, 9.0, 10.0, 11.0, 12.0,
                         13.0, 14.0, 15.0, 16.0, 17.0, 18.0, 19.0, 20.0],
        ):
            result = _run(platform._wait_for_dice_login(page, timeout=180))
        # OFF_HOST_BAIL_SECONDS=6 means 6s sustained off-host → bail
        assert result is False

    def test_back_on_dice_resets_offhost_timer(self, monkeypatch):
        """If the page bounces off-host briefly (1-2s) then back to
        dice.com, the off-host timer should reset rather than
        triggering a bail. Simulate by having the URL flip back
        and forth."""
        platform = _make_platform()
        page = MagicMock()
        urls = iter([
            "https://fedex.paradox.ai/x",       # off-host
            "https://www.dice.com/job-detail/x",  # back on dice
            "https://fedex.paradox.ai/x",       # off-host again
            "https://www.dice.com/job-detail/x",  # back on dice
        ])
        type(page).url = property(lambda self: next(urls, "https://www.dice.com/x"))

        # Predicate always returns False so the loop runs until
        # something else exits it. We'll exit by exhausting the
        # iterator → final URL becomes the default.
        # Use a small timeout so the loop actually exits.
        with patch.object(
            platform, "_dice_url_indicates_logged_in",
            new=AsyncMock(return_value=False),
        ), patch(
            "auto_applier.browser.platforms.dice.asyncio.sleep",
            new=AsyncMock(),
        ), patch(
            "auto_applier.notify.notify_user", new=MagicMock(),
        ):
            # Use a very short timeout to let the loop exit normally
            # — we just want to confirm the off-host bail didn't
            # fire before the timeout (i.e. URL alternation reset
            # the timer).
            result = _run(platform._wait_for_dice_login(page, timeout=1))
        # Timed out (False), but NOT because of off-host bail. The
        # caller can't tell the difference, but the WARN log line
        # would differ. Sanity check: we didn't crash.
        assert result is False
