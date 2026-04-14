"""Tests for Dice's _looks_like_auth_page signup/login detector.

Protects against the dead-lock where Dice redirects to a create-account
page after clicking Apply and our login-detection selectors mistakenly
matched the "go to dashboard" link, causing the tool to walk the signup
form for 8 modal steps.
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from auto_applier.browser.platforms.dice import DicePlatform


def _platform():
    ctx = MagicMock()
    ctx.pages = [MagicMock()]
    return DicePlatform(context=ctx, config={})


def _run(coro):
    return asyncio.run(coro)


class TestAuthPageURLDetection:
    @pytest.mark.parametrize("url", [
        "https://www.dice.com/dashboard/login",
        "https://www.dice.com/register",
        "https://www.dice.com/signup",
        "https://www.dice.com/sign-up",
        "https://www.dice.com/auth/signin",
        "https://www.dice.com/account/create",
        "https://www.dice.com/create-account",
    ])
    def test_auth_urls_detected(self, url):
        platform = _platform()
        page = MagicMock()
        page.url = url
        page.query_selector = AsyncMock(return_value=None)
        assert _run(platform._looks_like_auth_page(page)) is True

    def test_apply_url_not_detected(self):
        platform = _platform()
        page = MagicMock()
        page.url = "https://www.dice.com/jobs/detail/abc-123/apply"
        page.query_selector = AsyncMock(return_value=None)
        assert _run(platform._looks_like_auth_page(page)) is False

    def test_job_detail_url_not_detected(self):
        platform = _platform()
        page = MagicMock()
        page.url = "https://www.dice.com/job-detail/abc-123"
        page.query_selector = AsyncMock(return_value=None)
        assert _run(platform._looks_like_auth_page(page)) is False


class TestAuthPageDOMDetection:
    def test_password_field_plus_signup_button(self):
        platform = _platform()
        pw_input = MagicMock()
        signup_btn = MagicMock()
        signup_btn.inner_text = AsyncMock(return_value="Sign Up")
        page = MagicMock()
        page.url = "https://www.dice.com/jobs"  # not in URL patterns
        page.query_selector = AsyncMock(return_value=pw_input)
        page.query_selector_all = AsyncMock(return_value=[signup_btn])
        assert _run(platform._looks_like_auth_page(page)) is True

    def test_password_field_plus_login_button(self):
        platform = _platform()
        pw_input = MagicMock()
        login_btn = MagicMock()
        login_btn.inner_text = AsyncMock(return_value="Log In")
        page = MagicMock()
        page.url = "https://www.dice.com/jobs"
        page.query_selector = AsyncMock(return_value=pw_input)
        page.query_selector_all = AsyncMock(return_value=[login_btn])
        assert _run(platform._looks_like_auth_page(page)) is True

    def test_password_field_no_signup_button_not_detected(self):
        """A lone password field (e.g. on a settings page) shouldn't
        false-positive as an auth page."""
        platform = _platform()
        pw_input = MagicMock()
        random_btn = MagicMock()
        random_btn.inner_text = AsyncMock(return_value="Save Changes")
        page = MagicMock()
        page.url = "https://www.dice.com/dashboard/settings"
        # Actually /dashboard/settings URL wouldn't match but let's
        # isolate — here we test only the DOM logic:
        page.url = "https://www.dice.com/jobs/profile-edit"
        page.query_selector = AsyncMock(return_value=pw_input)
        page.query_selector_all = AsyncMock(return_value=[random_btn])
        assert _run(platform._looks_like_auth_page(page)) is False

    def test_no_password_field_not_detected(self):
        platform = _platform()
        page = MagicMock()
        page.url = "https://www.dice.com/jobs"
        page.query_selector = AsyncMock(return_value=None)
        assert _run(platform._looks_like_auth_page(page)) is False


class TestLoggedInSelectorsNoLongerTooGeneric:
    """Regression test — the old /dashboard and /profile href selectors
    matched on signup pages and caused the tool to mistake signup for
    a logged-in state. Make sure they're no longer in the list."""

    def test_no_bare_dashboard_selector(self):
        from auto_applier.browser.platforms.dice import LOGGED_IN_SELECTORS
        for sel in LOGGED_IN_SELECTORS:
            assert sel != "a[href*='/dashboard']", (
                "a[href*='/dashboard'] is too broad — matches on "
                "signup pages with 'go to dashboard' links"
            )

    def test_no_bare_profile_selector(self):
        from auto_applier.browser.platforms.dice import LOGGED_IN_SELECTORS
        for sel in LOGGED_IN_SELECTORS:
            assert sel != "a[href*='/profile']", (
                "a[href*='/profile'] is too broad"
            )

    def test_has_specific_user_menu_selectors(self):
        from auto_applier.browser.platforms.dice import LOGGED_IN_SELECTORS
        # Should still have some specific selectors that work only
        # when actually logged in
        has_data_cy = any(
            "data-cy" in s and "user-menu" in s.lower()
            for s in LOGGED_IN_SELECTORS
        )
        assert has_data_cy, (
            "Expected at least one data-cy-based user-menu selector"
        )
