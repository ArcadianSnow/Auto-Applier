"""Tests for the tightened CAPTCHA detection.

The old detector flagged any page that contained the phrase 'security
check' in its body text, which matched LinkedIn's cookie banner
and login help page. The new detector uses a layered evidence model:

- A real CAPTCHA iframe or container element is a strong signal.
- A URL matching a known challenge path is a strong signal.
- Phrase text is only accepted alongside a challenge container
  element (class or id containing 'captcha' or 'challenge').

These tests lock in the strong/weak split so a regression on
LinkedIn's normal login page can't reintroduce the false positive.
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from auto_applier.browser.base_platform import JobPlatform


class _FakePlatform(JobPlatform):
    """Minimal concrete subclass so we can instantiate the ABC."""
    source_id = "test"
    display_name = "Test"
    captcha_url_patterns = ["/captcha", "/checkpoint/challenge"]

    async def ensure_logged_in(self) -> bool: return True
    async def search_jobs(self, keyword, location): return []
    async def get_job_description(self, job): return ""
    async def apply_to_job(self, job, resume_path, dry_run=False): return None


def _fake_page(
    url: str = "https://example.com/normal",
    body_text: str = "",
    selector_hits: dict | None = None,
):
    """Build a MagicMock page that mimics Playwright's async API."""
    page = MagicMock()
    page.url = url
    page.inner_text = AsyncMock(return_value=body_text)

    hits = selector_hits or {}

    async def _query_selector(sel):
        return hits.get(sel)

    page.query_selector = _query_selector
    return page


def _run(coro):
    return asyncio.run(coro)


def _detect(platform, page) -> bool:
    return _run(platform.detect_captcha(page))


class TestStrongIframeSignal:
    def test_recaptcha_iframe_triggers(self):
        p = _FakePlatform(context=None, config={})
        page = _fake_page(
            selector_hits={"iframe[src*='recaptcha']": MagicMock()},
        )
        assert _detect(p, page) is True

    def test_hcaptcha_iframe_triggers(self):
        p = _FakePlatform(context=None, config={})
        page = _fake_page(
            selector_hits={"iframe[src*='hcaptcha']": MagicMock()},
        )
        assert _detect(p, page) is True

    def test_arkose_iframe_triggers(self):
        p = _FakePlatform(context=None, config={})
        page = _fake_page(
            selector_hits={"iframe[src*='arkoselabs']": MagicMock()},
        )
        assert _detect(p, page) is True


class TestStrongUrlSignal:
    def test_captcha_url_triggers(self):
        p = _FakePlatform(context=None, config={})
        page = _fake_page(url="https://linkedin.com/captcha?id=123")
        assert _detect(p, page) is True

    def test_checkpoint_challenge_triggers(self):
        p = _FakePlatform(context=None, config={})
        page = _fake_page(
            url="https://www.linkedin.com/checkpoint/challenge/abc",
        )
        assert _detect(p, page) is True

    def test_clean_url_no_match(self):
        p = _FakePlatform(context=None, config={})
        page = _fake_page(url="https://linkedin.com/feed/")
        assert _detect(p, page) is False


class TestStrongOnlyNoFalsePositives:
    """Regression tests: the detector must be strong-signal-only now."""

    def test_phrase_alone_does_not_trigger(self):
        p = _FakePlatform(context=None, config={})
        page = _fake_page(
            body_text=(
                "Welcome to LinkedIn. We run an ongoing security check "
                "on all accounts. Please verify you are a human — no "
                "unusual activity from your account has been detected."
            ),
        )
        # Even with THREE strong phrases in the body text, no
        # vendor iframe and no challenge URL means no stop.
        assert _detect(p, page) is False

    def test_challenge_class_on_random_element_does_not_trigger(self):
        """The old weak-container path matched React components.

        LinkedIn has plenty of normal DOM nodes with 'challenge' in
        their class name ('daily-challenge-widget', etc.) — those
        must not fire the detector.
        """
        p = _FakePlatform(context=None, config={})
        page = _fake_page(
            body_text="verify you are a human",
            selector_hits={"[class*='challenge']": MagicMock()},
        )
        assert _detect(p, page) is False


class TestLinkedInFalsePositives:
    """Specific regression tests for LinkedIn pages that used to fire."""

    def test_login_page_with_security_footer_is_clean(self):
        p = _FakePlatform(context=None, config={})
        page = _fake_page(
            url="https://www.linkedin.com/login",
            body_text=(
                "Sign in to LinkedIn. Forgot password? We take your "
                "security check seriously. Unusual activity in your "
                "account will be flagged. Are you a robot? No thank you."
            ),
        )
        assert _detect(p, page) is False

    def test_feed_page_clean(self):
        p = _FakePlatform(context=None, config={})
        page = _fake_page(
            url="https://www.linkedin.com/feed/",
            body_text="Your feed is empty. Connect with more people.",
        )
        assert _detect(p, page) is False


class TestLinkedInRealLoginFlow:
    """Guard against re-adding URL patterns that match normal login endpoints."""

    def _linkedin_platform(self):
        """Build a real LinkedIn platform instance for its actual URL list."""
        from auto_applier.browser.platforms.linkedin import LinkedInPlatform
        p = LinkedInPlatform.__new__(LinkedInPlatform)
        # Skip __init__ to avoid needing a context — we only touch
        # the class attribute captcha_url_patterns.
        return p

    def test_authwall_is_not_a_challenge(self):
        """
        LinkedIn redirects logged-out users to /authwall when they
        click a job/profile link. It's where users go to log in
        MANUALLY — not a challenge.
        """
        p = self._linkedin_platform()
        page = _fake_page(
            url="https://www.linkedin.com/authwall?session_redirect=...",
        )
        assert _detect(p, page) is False

    def test_login_submit_is_not_a_challenge(self):
        """
        /checkpoint/lg/login-submit is the POST target for LinkedIn's
        login form. The browser briefly navigates through it during
        a normal manual login — must not fire the detector.
        """
        p = self._linkedin_platform()
        page = _fake_page(
            url="https://www.linkedin.com/checkpoint/lg/login-submit",
        )
        assert _detect(p, page) is False

    def test_uas_login_submit_is_not_a_challenge(self):
        p = self._linkedin_platform()
        page = _fake_page(
            url="https://www.linkedin.com/uas/login-submit",
        )
        assert _detect(p, page) is False

    def test_real_checkpoint_challenge_still_fires(self):
        """The genuine challenge path must still trigger."""
        p = self._linkedin_platform()
        page = _fake_page(
            url="https://www.linkedin.com/checkpoint/challenge/verify",
        )
        assert _detect(p, page) is True


class TestErrorHandling:
    def test_query_selector_exception_does_not_raise(self):
        p = _FakePlatform(context=None, config={})
        page = MagicMock()
        page.url = "https://example.com/clean"

        async def _qs(sel):
            raise RuntimeError("selector broke")

        page.query_selector = _qs
        page.inner_text = AsyncMock(return_value="clean page")
        # Should swallow exceptions and return False
        assert _detect(p, page) is False

    def test_inner_text_exception_does_not_raise(self):
        p = _FakePlatform(context=None, config={})
        page = _fake_page()
        page.inner_text = AsyncMock(side_effect=RuntimeError("inner_text broke"))
        assert _detect(p, page) is False
