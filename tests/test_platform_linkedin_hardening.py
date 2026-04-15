"""Tests for LinkedIn Phase 9 hardening.

Ports the proven patterns from Dice + ZR to LinkedIn:
- URL-based login detection (no more fragile selector matches)
- Auth-page bail-out (/login, /signup, /authwall, /checkpoint, /uas)
- Chrome-field filter for LinkedIn's persistent header
- Validation error scanning when forms get stuck
- Spinner-wait helper
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from auto_applier.browser.platforms.linkedin import LinkedInPlatform


def _platform():
    ctx = MagicMock()
    ctx.pages = [MagicMock()]
    return LinkedInPlatform(context=ctx, config={})


def _run(coro):
    return asyncio.run(coro)


# ------------------------------------------------------------------
# URL-based login detection
# ------------------------------------------------------------------

class TestURLIndicatesLoggedIn:
    @pytest.mark.parametrize("url", [
        "https://www.linkedin.com/feed/",
        "https://www.linkedin.com/jobs/",
        "https://www.linkedin.com/jobs/view/12345/",
        "https://www.linkedin.com/in/someone/",
        "https://www.linkedin.com/",
    ])
    def test_logged_in_urls(self, url):
        platform = _platform()
        page = MagicMock()
        page.url = url
        assert _run(platform._url_indicates_logged_in(page)) is True

    @pytest.mark.parametrize("url", [
        "https://www.linkedin.com/login",
        "https://www.linkedin.com/signup",
        "https://www.linkedin.com/checkpoint/challenge",
        "https://www.linkedin.com/checkpoint/lg/login-submit",
        "https://www.linkedin.com/uas/login",
        "https://www.linkedin.com/authwall?sessionId=abc",
        "https://www.linkedin.com/m/login/",
    ])
    def test_auth_urls_not_logged_in(self, url):
        platform = _platform()
        page = MagicMock()
        page.url = url
        assert _run(platform._url_indicates_logged_in(page)) is False

    def test_off_domain_not_logged_in(self):
        platform = _platform()
        page = MagicMock()
        page.url = "https://example.com/something"
        assert _run(platform._url_indicates_logged_in(page)) is False


# ------------------------------------------------------------------
# _looks_like_auth_page
# ------------------------------------------------------------------

class TestLooksLikeAuthPage:
    @pytest.mark.parametrize("url", [
        "https://www.linkedin.com/login",
        "https://www.linkedin.com/signup/new-user",
        "https://www.linkedin.com/checkpoint/challenge",
        "https://www.linkedin.com/uas/login",
        "https://www.linkedin.com/authwall",
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
        page.url = "https://www.linkedin.com/jobs/view/12345/"
        page.query_selector = AsyncMock(return_value=None)
        assert _run(platform._looks_like_auth_page(page)) is False

    def test_password_plus_signin_button(self):
        platform = _platform()
        pw_input = MagicMock()
        btn = MagicMock()
        btn.inner_text = AsyncMock(return_value="Sign in")
        page = MagicMock()
        page.url = "https://www.linkedin.com/jobs"  # not in auth URLs
        page.query_selector = AsyncMock(return_value=pw_input)
        page.query_selector_all = AsyncMock(return_value=[btn])
        assert _run(platform._looks_like_auth_page(page)) is True

    def test_password_plus_join_button(self):
        platform = _platform()
        pw_input = MagicMock()
        btn = MagicMock()
        btn.inner_text = AsyncMock(return_value="Join now")
        page = MagicMock()
        page.url = "https://www.linkedin.com/jobs"
        page.query_selector = AsyncMock(return_value=pw_input)
        page.query_selector_all = AsyncMock(return_value=[btn])
        assert _run(platform._looks_like_auth_page(page)) is True

    def test_no_password_not_detected(self):
        platform = _platform()
        page = MagicMock()
        page.url = "https://www.linkedin.com/jobs"
        page.query_selector = AsyncMock(return_value=None)
        assert _run(platform._looks_like_auth_page(page)) is False


# ------------------------------------------------------------------
# Chrome-field filter
# ------------------------------------------------------------------

class TestChromeFieldFilter:
    @pytest.mark.parametrize("label", [
        "Search",
        "Search by title, skill, or company",
        "Search jobs",
        "Search Jobs",
        "Search for job title or keyword",
        "Search by company",
        "Search messages",
        "Search people",
        "Jobs search",
        "Messaging search",
        "City, State, or Zip Code",
    ])
    def test_chrome_labels_filtered(self, label):
        platform = _platform()
        assert platform._is_chrome_field(label) is True, (
            f"'{label}' should be treated as chrome"
        )

    @pytest.mark.parametrize("label", [
        "First Name",
        "Last Name",
        "Email Address",
        "Phone",
        "Why do you want this job?",
        "Years of experience with Python",
        "Cover letter",
        "Are you authorized to work in the US?",
        "Additional information",
    ])
    def test_real_form_labels_not_filtered(self, label):
        platform = _platform()
        assert platform._is_chrome_field(label) is False, (
            f"'{label}' is a real apply-form field, shouldn't be chrome"
        )

    def test_empty_label(self):
        platform = _platform()
        assert platform._is_chrome_field("") is False
        assert platform._is_chrome_field(None) is False


# ------------------------------------------------------------------
# Validation error scanning
# ------------------------------------------------------------------

class TestScanValidationErrors:
    def test_finds_visible_error(self):
        platform = _platform()
        el = MagicMock()
        el.is_visible = AsyncMock(return_value=True)
        el.inner_text = AsyncMock(return_value="Please enter a valid phone number")
        page = MagicMock()
        page.query_selector_all = AsyncMock(return_value=[el])

        result = _run(platform._scan_validation_errors(page))
        assert "phone" in result.lower()

    def test_ignores_hidden_errors(self):
        platform = _platform()
        el = MagicMock()
        el.is_visible = AsyncMock(return_value=False)
        el.inner_text = AsyncMock(return_value="Required")
        page = MagicMock()
        page.query_selector_all = AsyncMock(return_value=[el])

        result = _run(platform._scan_validation_errors(page))
        assert result == ""

    def test_no_errors(self):
        platform = _platform()
        page = MagicMock()
        page.query_selector_all = AsyncMock(return_value=[])

        result = _run(platform._scan_validation_errors(page))
        assert result == ""


# ------------------------------------------------------------------
# LinkedIn-specific config sanity
# ------------------------------------------------------------------

class TestLinkedInConfig:
    def test_source_id(self):
        assert LinkedInPlatform.source_id == "linkedin"

    def test_has_auth_url_patterns(self):
        assert "/login" in LinkedInPlatform._AUTH_URL_PATTERNS
        assert "/authwall" in LinkedInPlatform._AUTH_URL_PATTERNS
        assert "/checkpoint" in LinkedInPlatform._AUTH_URL_PATTERNS

    def test_has_chrome_patterns(self):
        assert len(LinkedInPlatform._CHROME_LABEL_PATTERNS) >= 5

    def test_captcha_patterns_exclude_login_flow(self):
        """These URLs look like challenges but are normal login flow."""
        patterns = LinkedInPlatform.captcha_url_patterns
        assert "/uas/login-submit" not in patterns
        assert "/authwall" not in patterns  # not a challenge
        # But the real challenge URL should be there
        assert "/checkpoint/challenge" in patterns


# ------------------------------------------------------------------
# Soft-block detector (consecutive empty descriptions)
# ------------------------------------------------------------------

class TestSoftBlockDetector:
    """LinkedIn soft-blocks flagged sessions by serving empty job
    pages. After N consecutive empty descriptions, the adapter must
    raise CaptchaDetectedError to halt the platform run."""

    def _setup_platform(self, description_texts: list[str]):
        """Build a platform with get_page + safe_get_text stubs
        that return the given description texts in sequence."""
        from unittest.mock import patch

        platform = _platform()
        platform._LI_CONSECUTIVE_EMPTY_DESCRIPTIONS = 0
        platform._LI_MAX_CONSECUTIVE_EMPTY = 2

        page = MagicMock()
        page.url = "https://www.linkedin.com/feed/"
        page.goto = AsyncMock()
        page.query_selector = AsyncMock(return_value=None)

        platform.get_page = AsyncMock(return_value=page)
        platform.safe_click = AsyncMock(return_value=False)
        platform.safe_get_text = AsyncMock(side_effect=description_texts)
        platform.check_and_abort_on_captcha = AsyncMock()

        return platform

    def test_empty_description_increments_counter(self):
        from auto_applier.browser.base_platform import CaptchaDetectedError
        from auto_applier.storage.models import Job
        from unittest.mock import patch

        platform = self._setup_platform(["", ""])

        async def run_twice():
            # Patch reading_pause to be a no-op async
            async def _no_op(*a, **kw): pass
            with patch(
                "auto_applier.browser.platforms.linkedin.reading_pause",
                _no_op,
            ), patch(
                "auto_applier.browser.platforms.linkedin.random_delay",
                _no_op,
            ):
                job1 = Job(job_id="a", title="t", company="c",
                           url="https://www.linkedin.com/jobs/view/1")
                await platform.get_job_description(job1)
                assert platform._LI_CONSECUTIVE_EMPTY_DESCRIPTIONS == 1

                job2 = Job(job_id="b", title="t", company="c",
                           url="https://www.linkedin.com/jobs/view/2")
                with pytest.raises(CaptchaDetectedError) as exc_info:
                    await platform.get_job_description(job2)
                assert "soft-blocked" in str(exc_info.value).lower()

        _run(run_twice())

    def test_successful_description_resets_counter(self):
        from auto_applier.storage.models import Job
        from unittest.mock import patch

        platform = self._setup_platform(["Real description content here."])
        platform._LI_CONSECUTIVE_EMPTY_DESCRIPTIONS = 1  # one already failed

        async def run_once():
            async def _no_op(*a, **kw): pass
            with patch(
                "auto_applier.browser.platforms.linkedin.reading_pause",
                _no_op,
            ), patch(
                "auto_applier.browser.platforms.linkedin.random_delay",
                _no_op,
            ):
                job = Job(job_id="a", title="t", company="c",
                          url="https://www.linkedin.com/jobs/view/1")
                result = await platform.get_job_description(job)
                assert "Real description" in result
                assert platform._LI_CONSECUTIVE_EMPTY_DESCRIPTIONS == 0

        _run(run_once())


# ------------------------------------------------------------------
# Apply result summary logging
# ------------------------------------------------------------------

class TestBuildResultLogging:
    def test_success_logs(self, caplog):
        import logging
        platform = _platform()
        filler = MagicMock()
        filler.gaps = []
        filler.resume_label = "eng"
        filler.cover_letter_generated = False
        filler.fields_filled = 4
        filler.fields_total = 5
        filler.used_llm = True
        platform.form_filler = filler

        with caplog.at_level(logging.INFO):
            platform._build_result(success=True)
        assert "LinkedIn: Apply result: success" in caplog.text
        assert "4/5" in caplog.text

    def test_failure_logs(self, caplog):
        import logging
        platform = _platform()
        platform.form_filler = None

        with caplog.at_level(logging.INFO):
            platform._build_result(success=False, failure_reason="Auth page")
        assert "LinkedIn: Apply result: failed" in caplog.text
        assert "Auth page" in caplog.text
