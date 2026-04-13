"""Tests for Indeed platform adapter — module dispatch, success, external detection."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from auto_applier.browser.platforms.indeed import IndeedPlatform


def _make_platform():
    ctx = MagicMock()
    ctx.pages = []
    return IndeedPlatform(context=ctx, config={})


def _run(coro):
    return asyncio.run(coro)


# ------------------------------------------------------------------
# Module Dispatch (URL-based routing)
# ------------------------------------------------------------------

class TestDispatchModule:
    """Verify URL patterns route to the correct module name."""

    def _dispatch(self, url):
        platform = _make_platform()
        page = MagicMock()
        page.url = url
        job = MagicMock()
        resume_path = ""

        # Patch all handlers to avoid side effects
        for handler in ("_handle_contact_info", "_handle_resume_selection",
                        "_handle_review", "_handle_questions", "_handle_generic"):
            setattr(platform, handler, AsyncMock(return_value=[]))

        async def do():
            module, _ = await platform._dispatch_module(page, job, resume_path, url)
            return module

        return _run(do())

    def test_contact_info(self):
        assert self._dispatch("https://m5.apply.indeed.com/indeedapply/contact-info-module") == "contact-info"

    def test_questions(self):
        assert self._dispatch("https://m5.apply.indeed.com/indeedapply/questions-module") == "questions"

    def test_questions_alt_url(self):
        assert self._dispatch("https://m5.apply.indeed.com/indeedapply/questions/q1") == "questions"

    def test_resume_selection(self):
        assert self._dispatch("https://m5.apply.indeed.com/indeedapply/resume-selection") == "resume-selection"

    def test_review(self):
        assert self._dispatch("https://m5.apply.indeed.com/indeedapply/review-module") == "review"

    def test_intervention(self):
        assert self._dispatch("https://m5.apply.indeed.com/indeedapply/intervention") == "intervention"

    def test_generic_fallback(self):
        assert self._dispatch("https://m5.apply.indeed.com/indeedapply/something-new") == "generic"


# ------------------------------------------------------------------
# Submit Step Detection
# ------------------------------------------------------------------

class TestIsSubmitStep:
    def test_submit_detected_by_selector(self):
        platform = _make_platform()
        el = MagicMock()
        el.inner_text = AsyncMock(return_value="Submit Application")
        page = MagicMock()
        page.query_selector = AsyncMock(return_value=el)

        assert _run(platform._is_submit_step(page)) is True

    def test_submit_by_text_scan(self):
        platform = _make_platform()
        btn = MagicMock()
        btn.inner_text = AsyncMock(return_value="Submit Application")
        page = MagicMock()
        page.query_selector = AsyncMock(return_value=None)
        page.query_selector_all = AsyncMock(return_value=[btn])

        assert _run(platform._is_submit_step(page)) is True

    def test_no_submit(self):
        platform = _make_platform()
        btn = MagicMock()
        btn.inner_text = AsyncMock(return_value="Continue")
        page = MagicMock()
        page.query_selector = AsyncMock(return_value=None)
        page.query_selector_all = AsyncMock(return_value=[btn])

        assert _run(platform._is_submit_step(page)) is False


# ------------------------------------------------------------------
# Success Detection
# ------------------------------------------------------------------

class TestCheckSuccess:
    def test_success_by_url(self):
        platform = _make_platform()
        page = MagicMock()
        page.url = "https://m5.apply.indeed.com/indeedapply/confirmation?tk=xyz"
        page.query_selector = AsyncMock(return_value=None)
        page.inner_text = AsyncMock(return_value="")

        assert _run(platform._check_success(page)) is True

    def test_success_by_visible_selector(self):
        platform = _make_platform()
        el = MagicMock()
        el.is_visible = AsyncMock(return_value=True)
        page = MagicMock()
        page.url = "https://m5.apply.indeed.com/indeedapply/step"
        page.query_selector = AsyncMock(return_value=el)
        page.inner_text = AsyncMock(return_value="")

        assert _run(platform._check_success(page)) is True

    def test_success_by_phrase(self):
        platform = _make_platform()
        page = MagicMock()
        page.url = "https://m5.apply.indeed.com/indeedapply/step"
        page.query_selector = AsyncMock(return_value=None)
        page.inner_text = AsyncMock(return_value="Your application has been submitted to Acme Corp.")

        assert _run(platform._check_success(page)) is True

    def test_no_success(self):
        platform = _make_platform()
        page = MagicMock()
        page.url = "https://m5.apply.indeed.com/indeedapply/step"
        page.query_selector = AsyncMock(return_value=None)
        page.inner_text = AsyncMock(return_value="Please complete all fields.")

        assert _run(platform._check_success(page)) is False

    def test_invisible_selector_ignored(self):
        platform = _make_platform()
        el = MagicMock()
        el.is_visible = AsyncMock(return_value=False)
        page = MagicMock()
        page.url = "https://m5.apply.indeed.com/indeedapply/step"
        page.query_selector = AsyncMock(return_value=el)
        page.inner_text = AsyncMock(return_value="Nothing")

        assert _run(platform._check_success(page)) is False


# ------------------------------------------------------------------
# External Apply Detection
# ------------------------------------------------------------------

class TestIsExternalApply:
    def test_external_by_button_text(self):
        platform = _make_platform()
        btn = MagicMock()
        btn.inner_text = AsyncMock(return_value="Apply on company site")
        page = MagicMock()
        page.query_selector_all = AsyncMock(return_value=[btn])

        async def do():
            with patch.object(platform, "safe_query", return_value=None):
                return await platform._is_external_apply(page)

        assert _run(do()) is True

    def test_not_external(self):
        platform = _make_platform()
        page = MagicMock()
        page.query_selector_all = AsyncMock(return_value=[])

        async def do():
            with patch.object(platform, "safe_query", return_value=None):
                return await platform._is_external_apply(page)

        assert _run(do()) is False


# ------------------------------------------------------------------
# Constants & Config
# ------------------------------------------------------------------

class TestIndeedConfig:
    def test_source_id(self):
        assert IndeedPlatform.source_id == "indeed"

    def test_display_name(self):
        assert IndeedPlatform.display_name == "Indeed"

    def test_has_dead_listing_selectors(self):
        assert len(IndeedPlatform.dead_listing_selectors) > 0

    def test_has_captcha_patterns(self):
        assert any("/captcha" in p for p in IndeedPlatform.captcha_url_patterns)
