"""Regression tests for the Dice external-ATS-redirect false-applied bug.

Live run 2026-05-03 11:28:54 hit a false-applied scenario:

  1. User clicks Dice apply button on a job hosted by TEKsystems.
  2. Dice opens a new tab at ``dice.com/job-applications/<id>/start-apply``
     (a bridge URL on the dice.com domain).
  3. The bridge 302-redirects the new tab to ``apply.teksystems.com``.
  4. The old ``_check_ats_redirect`` only waited for ``domcontentloaded``,
     which fired on the bridge's first DOM (still on dice.com), so it
     classified the tab as "internal" and returned its Page.
  5. The form-filler then walked TEKsystems' application form with 27
     fields, none of which matched our heuristics, and the run logged
     "Apply: Telecom GIS ... → dry_run [0/27 fields]" — false success
     on a foreign site.

The fix is three layers:
  - ``_check_ats_redirect`` waits for ``load`` (full redirect chain),
    parses the host strictly, requires both dice.com host AND a known
    apply path before classifying as internal.
  - ``_walk_easy_apply_modal`` re-checks page host at entry; if off
    dice.com, returns ``requires_manual_apply=True`` immediately.
  - ``_wait_for_submit_outcome`` requires ``stayed_on_dice`` for its
    "navigation away = probable success" heuristic to fire.

These tests pin all three layers.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from auto_applier.browser.platforms.dice import DicePlatform
from auto_applier.storage.models import Job


def _run(coro):
    return asyncio.run(coro)


def _make_platform():
    ctx = MagicMock()
    ctx.pages = []
    p = DicePlatform(context=ctx, config={})
    return p, ctx


def _job(jid="dice-test-1"):
    return Job(
        job_id=jid, source="dice", title="Engineer",
        company="Acme", url="https://www.dice.com/job-detail/abc",
    )


# ----------------------------------------------------------------------
# Layer 1: _check_ats_redirect host parsing
# ----------------------------------------------------------------------

class TestCheckAtsRedirectHostParsing:
    """The substring check used to fire on any URL containing
    'dice.com/job-applications' or '/start-apply'. The bridge tab
    URL satisfies both (it's literally on dice.com), but redirects
    to an external ATS by the time the walker scans for fields.
    Strict host parsing + load-state wait catches this."""

    def _make_tab(self, url):
        tab = MagicMock()
        tab.wait_for_load_state = AsyncMock()
        tab.url = url
        tab.bring_to_front = AsyncMock()
        tab.close = AsyncMock()
        return tab

    def test_dice_internal_apply_url_classified_internal(self):
        """A tab that resolves to a true dice.com apply URL after
        redirects → returns the Page object."""
        platform, ctx = _make_platform()
        tab = self._make_tab(
            "https://www.dice.com/job-applications/abc/start-apply"
        )
        ctx.pages = [tab]

        async def do():
            return await platform._check_ats_redirect(pages_before=0)
        result = _run(do())
        assert result is tab

    def test_external_redirect_classified_external_even_if_bridge_url_was_dice(self):
        """The bridge URL is on dice.com, but ``wait_for_load_state('load')``
        ensures we read the URL AFTER the 302 chain. If that final URL
        is off-host, classify as external — close the tab, return
        sentinel string."""
        platform, ctx = _make_platform()
        tab = self._make_tab(
            "https://apply.teksystems.com/v1/s/?opco=TEK&params=..."
        )
        ctx.pages = [tab]

        async def do():
            return await platform._check_ats_redirect(pages_before=0)
        assert _run(do()) == "external"
        tab.close.assert_awaited()

    def test_external_url_with_dice_substring_in_path_not_misclassified(self):
        """Defense-in-depth: an external site that happens to have
        '/start-apply' in its URL must NOT be classified as internal.
        The old substring check would have matched."""
        platform, ctx = _make_platform()
        tab = self._make_tab(
            "https://careers.boozallen.com/start-apply/123"
        )
        ctx.pages = [tab]

        async def do():
            return await platform._check_ats_redirect(pages_before=0)
        assert _run(do()) == "external"

    def test_external_url_with_dice_com_in_query_string_not_misclassified(self):
        """Some external trackers include 'rx_source=Dice' or even
        'redirect=https://www.dice.com/...' as query params. Substring
        matching on raw URL would classify as internal; host-anchored
        check correctly classifies as external."""
        platform, ctx = _make_platform()
        tab = self._make_tab(
            "https://apply.teksystems.com/v1/s/?rx_source=Dice"
            "&continue=https://www.dice.com/dashboard/apply"
        )
        ctx.pages = [tab]

        async def do():
            return await platform._check_ats_redirect(pages_before=0)
        assert _run(do()) == "external"

    def test_no_new_tab_returns_none(self):
        platform, ctx = _make_platform()
        ctx.pages = []  # no new pages

        async def do():
            return await platform._check_ats_redirect(pages_before=0)
        assert _run(do()) is None

    def test_dashboard_apply_url_classified_internal(self):
        platform, ctx = _make_platform()
        tab = self._make_tab(
            "https://www.dice.com/dashboard/apply/abc"
        )
        ctx.pages = [tab]

        async def do():
            return await platform._check_ats_redirect(pages_before=0)
        assert _run(do()) is tab


# ----------------------------------------------------------------------
# Layer 2: _walk_easy_apply_modal host gate
# ----------------------------------------------------------------------

class TestWalkEasyApplyModalHostGate:
    """The walk-modal entrypoint must verify the page is still on
    dice.com BEFORE invoking any selector logic. Even if
    _check_ats_redirect's stricter check missed an external host
    (network instability, late redirect, etc.), this catches it."""

    def test_off_host_page_returns_manual_apply(self):
        platform, _ = _make_platform()
        page = MagicMock()
        page.url = "https://apply.teksystems.com/v1/s/?opco=TEK"
        page.close = AsyncMock()

        async def do():
            return await platform._walk_easy_apply_modal(
                page, _job(), resume_path="/x.pdf", dry_run=True,
            )
        result = _run(do())
        assert result.success is False
        assert result.requires_manual_apply is True
        assert "external" in (result.failure_reason or "").lower()
        # We close the off-host tab as part of the bail.
        page.close.assert_awaited()

    def test_off_host_failure_reason_includes_host(self):
        """The failure reason should name the host so the user can
        see which ATS the apply got routed to (helps debug + builds
        confidence the bot didn't fill anything on the wrong site)."""
        platform, _ = _make_platform()
        page = MagicMock()
        page.url = "https://careers.boozallen.com/careers/JobDetail?jobId=123"
        page.close = AsyncMock()

        async def do():
            return await platform._walk_easy_apply_modal(
                page, _job(), resume_path="/x.pdf", dry_run=True,
            )
        result = _run(do())
        assert result.success is False
        assert "careers.boozallen.com" in (result.failure_reason or "")

    def test_subdomain_of_dice_passes_host_check(self):
        """Dice runs apply.dice.com / applications.dice.com etc as
        internal subdomains. The host gate's accept condition is
        ``cur_host == "dice.com" or cur_host.endswith(".dice.com")``
        — verify directly rather than patching every walker path."""
        # Mirror the inline host-parse logic from _walk_easy_apply_modal.
        for url in (
            "https://www.dice.com/job-detail/abc",
            "https://dice.com/job-detail/abc",
            "https://apply.dice.com/v1/123",
            "https://applications.dice.com/abc",
        ):
            host = url.split("/")[2].lower()
            assert host == "dice.com" or host.endswith(".dice.com"), \
                f"Expected {host} to be accepted as Dice host"

    def test_external_hosts_rejected_by_host_check(self):
        """Mirror set: hosts that should NOT be accepted as Dice."""
        for url in (
            "https://apply.teksystems.com/v1/abc",
            "https://careers.boozallen.com/abc",
            "https://my.dice.com.evil.example/abc",
            "https://example.com/dice.com/abc",
        ):
            host = url.split("/")[2].lower()
            is_internal = host == "dice.com" or host.endswith(".dice.com")
            assert not is_internal, \
                f"{host} must NOT be accepted as Dice"


# ----------------------------------------------------------------------
# Layer 3: _wait_for_submit_outcome stayed_on_dice gate
# ----------------------------------------------------------------------

class TestWaitForSubmitOutcomeStaysOnDice:
    """The 'navigation away = probable success' branch must only
    fire when we're still on dice.com. An external ATS-driven URL
    change would otherwise be misread as a submission confirmation."""

    def test_navigation_to_external_host_does_not_trigger_success(self):
        platform, _ = _make_platform()
        page = MagicMock()
        # After submit, the page redirected to an external host — must
        # NOT fire the navigation-away success heuristic.
        page.url = "https://apply.teksystems.com/v1/confirmation/123"
        page.query_selector = AsyncMock(return_value=None)

        # Patch asyncio.sleep so the 15s poll completes instantly.
        with patch(
            "auto_applier.browser.platforms.dice.asyncio.sleep",
            AsyncMock(return_value=None),
        ):
            async def do():
                with patch.object(platform, "_check_success",
                                  AsyncMock(return_value=False)), \
                     patch.object(platform, "_scan_validation_errors",
                                  AsyncMock(return_value="")):
                    return await platform._wait_for_submit_outcome(
                        page, _job(),
                        pre_url="https://www.dice.com/job-detail/abc",
                    )
            result = _run(do())
        assert result.success is False
        assert "no success confirmation" in (result.failure_reason or "")

    def test_navigation_to_dice_subdomain_still_triggers_probable_success(self):
        """A real submit can land on a Dice subdomain (e.g. apply.dice.com).
        Shouldn't be rejected — *.dice.com is internal."""
        platform, _ = _make_platform()
        page = MagicMock()
        page.url = "https://apply.dice.com/v1/confirmation/abc"
        page.query_selector = AsyncMock(return_value=None)

        async def do():
            with patch.object(platform, "_check_success",
                              AsyncMock(return_value=False)), \
                 patch.object(platform, "_scan_validation_errors",
                              AsyncMock(return_value="")), \
                 patch.object(platform, "_close_modal", AsyncMock()):
                return await platform._wait_for_submit_outcome(
                    page, _job(),
                    pre_url="https://www.dice.com/job-detail/abc",
                )
        result = _run(do())
        assert result.success is True
