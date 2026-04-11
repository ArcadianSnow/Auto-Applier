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


class TestWeakPhraseRequiresContainer:
    def test_phrase_alone_does_not_trigger(self):
        """The old detector would flag this. The new one must not."""
        p = _FakePlatform(context=None, config={})
        # A normal LinkedIn footer-ish page mentioning 'security check'
        page = _fake_page(
            body_text=(
                "Welcome to LinkedIn. We run an ongoing security check "
                "on all accounts to keep your data safe. By clicking "
                "continue you agree to our privacy policy."
            ),
        )
        assert _detect(p, page) is False

    def test_phrase_plus_challenge_container_triggers(self):
        p = _FakePlatform(context=None, config={})
        page = _fake_page(
            body_text="Please verify you are a human to continue",
            selector_hits={
                "[class*='challenge']": MagicMock(),
            },
        )
        assert _detect(p, page) is True

    def test_challenge_container_without_phrase_does_not_trigger(self):
        p = _FakePlatform(context=None, config={})
        page = _fake_page(
            body_text="Normal page content with nothing unusual",
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
        # None of those phrases appear as strong phrases AND there's
        # no challenge container — must not trigger.
        assert _detect(p, page) is False

    def test_feed_page_clean(self):
        p = _FakePlatform(context=None, config={})
        page = _fake_page(
            url="https://www.linkedin.com/feed/",
            body_text="Your feed is empty. Connect with more people.",
        )
        assert _detect(p, page) is False


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
