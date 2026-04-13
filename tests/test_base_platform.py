"""Tests for browser/base_platform.py — safe_query, safe_click, find_jobs_by_anchors."""

import asyncio
from unittest.mock import MagicMock, AsyncMock, patch, PropertyMock

import pytest

from auto_applier.browser.base_platform import JobPlatform, CaptchaDetectedError


# ------------------------------------------------------------------
# Concrete stub for the ABC
# ------------------------------------------------------------------

class StubPlatform(JobPlatform):
    source_id = "test"
    display_name = "Test"

    async def ensure_logged_in(self):
        return True

    async def search_jobs(self, kw, loc):
        return []

    async def get_job_description(self, job):
        return ""

    async def apply_to_job(self, job, resume_path, dry_run=False):
        return None


def _make_platform():
    ctx = MagicMock()
    return StubPlatform(context=ctx, config={})


# ------------------------------------------------------------------
# Helpers to build mock Playwright elements
# ------------------------------------------------------------------

def _mock_element(visible=True, text="", bbox=None):
    el = AsyncMock()
    el.is_visible = AsyncMock(return_value=visible)
    el.inner_text = AsyncMock(return_value=text)
    el.bounding_box = AsyncMock(return_value=bbox)
    el.click = AsyncMock()
    return el


def _mock_page(wait_for_selector_returns=None, url="https://example.com"):
    page = AsyncMock()
    page.url = url
    page.mouse = AsyncMock()
    page.mouse.click = AsyncMock()
    page.mouse.move = AsyncMock()
    if wait_for_selector_returns is not None:
        page.wait_for_selector = AsyncMock(return_value=wait_for_selector_returns)
    else:
        page.wait_for_selector = AsyncMock(side_effect=Exception("not found"))
    return page


# ------------------------------------------------------------------
# safe_query
# ------------------------------------------------------------------

class TestSafeQuery:
    def test_returns_first_match(self):
        platform = _make_platform()
        el = _mock_element()
        page = _mock_page(wait_for_selector_returns=el)

        result = asyncio.run(platform.safe_query(page, ["#a", "#b"], timeout=100))
        assert result is el

    def test_returns_none_when_nothing_matches(self):
        platform = _make_platform()
        page = _mock_page()  # raises on all selectors

        result = asyncio.run(platform.safe_query(page, ["#a", "#b"], timeout=100))
        assert result is None

    def test_tries_all_selectors(self):
        platform = _make_platform()
        el = _mock_element()
        page = AsyncMock()
        # First selector fails, second succeeds
        page.wait_for_selector = AsyncMock(
            side_effect=[Exception("miss"), el]
        )

        result = asyncio.run(platform.safe_query(page, ["#miss", "#hit"], timeout=100))
        assert result is el
        assert page.wait_for_selector.call_count == 2

    def test_empty_selector_list(self):
        platform = _make_platform()
        page = _mock_page()
        result = asyncio.run(platform.safe_query(page, [], timeout=100))
        assert result is None


# ------------------------------------------------------------------
# safe_click
# ------------------------------------------------------------------

class TestSafeClick:
    def test_click_with_bounding_box(self):
        platform = _make_platform()
        el = _mock_element(bbox={"x": 100, "y": 200, "width": 50, "height": 30})
        page = _mock_page(wait_for_selector_returns=el)

        with patch("auto_applier.browser.base_platform.human_move", new_callable=AsyncMock):
            result = asyncio.run(platform.safe_click(page, ["#btn"], timeout=100))
        assert result is True

    def test_click_without_bounding_box(self):
        platform = _make_platform()
        el = _mock_element(bbox=None)
        page = _mock_page(wait_for_selector_returns=el)

        result = asyncio.run(platform.safe_click(page, ["#btn"], timeout=100))
        assert result is True
        el.click.assert_called_once()

    def test_returns_false_when_not_found(self):
        platform = _make_platform()
        page = _mock_page()  # no element found

        result = asyncio.run(platform.safe_click(page, ["#btn"], timeout=100))
        assert result is False


# ------------------------------------------------------------------
# safe_get_text
# ------------------------------------------------------------------

class TestSafeGetText:
    def test_returns_text(self):
        platform = _make_platform()
        el = _mock_element(text="  Hello World  ")
        page = _mock_page(wait_for_selector_returns=el)

        result = asyncio.run(platform.safe_get_text(page, ["#el"], timeout=100))
        assert result == "Hello World"

    def test_returns_empty_when_not_found(self):
        platform = _make_platform()
        page = _mock_page()

        result = asyncio.run(platform.safe_get_text(page, ["#el"], timeout=100))
        assert result == ""


# ------------------------------------------------------------------
# find_jobs_by_anchors
# ------------------------------------------------------------------

class TestFindJobsByAnchors:
    def _make_anchor(self, href, text):
        a = AsyncMock()
        a.get_attribute = AsyncMock(return_value=href)
        a.inner_text = AsyncMock(return_value=text)
        return a

    def test_finds_matching_anchors(self):
        platform = _make_platform()
        page = AsyncMock()
        page.url = "https://www.example.com/search"
        page.query_selector_all = AsyncMock(return_value=[
            self._make_anchor("/jobs/123", "Software Engineer"),
            self._make_anchor("/jobs/456", "Data Analyst"),
        ])

        result = asyncio.run(platform.find_jobs_by_anchors(page, "/jobs/"))
        assert len(result) == 2
        assert result[0] == ("Software Engineer", "https://www.example.com/jobs/123")

    def test_deduplicates_by_href(self):
        platform = _make_platform()
        page = AsyncMock()
        page.url = "https://www.example.com/search"
        page.query_selector_all = AsyncMock(return_value=[
            self._make_anchor("/jobs/123", "Software Engineer"),
            self._make_anchor("/jobs/123?ref=1", "Software Engineer"),  # same base
        ])

        result = asyncio.run(platform.find_jobs_by_anchors(page, "/jobs/"))
        assert len(result) == 1

    def test_filters_short_titles(self):
        platform = _make_platform()
        page = AsyncMock()
        page.url = "https://www.example.com/search"
        page.query_selector_all = AsyncMock(return_value=[
            self._make_anchor("/jobs/1", "OK"),  # too short (2 chars < 5)
            self._make_anchor("/jobs/2", "Good Title Here"),
        ])

        result = asyncio.run(platform.find_jobs_by_anchors(page, "/jobs/"))
        assert len(result) == 1
        assert result[0][0] == "Good Title Here"

    def test_filters_navigation_words(self):
        platform = _make_platform()
        page = AsyncMock()
        page.url = "https://www.example.com/search"
        page.query_selector_all = AsyncMock(return_value=[
            self._make_anchor("/jobs/1", "Apply Now"),
            self._make_anchor("/jobs/2", "Sign In"),
            self._make_anchor("/jobs/3", "Real Job Title"),
        ])

        result = asyncio.run(platform.find_jobs_by_anchors(page, "/jobs/"))
        assert len(result) == 1
        assert result[0][0] == "Real Job Title"

    def test_empty_page(self):
        platform = _make_platform()
        page = AsyncMock()
        page.query_selector_all = AsyncMock(return_value=[])

        result = asyncio.run(platform.find_jobs_by_anchors(page, "/jobs/"))
        assert result == []


# ------------------------------------------------------------------
# detect_captcha
# ------------------------------------------------------------------

class TestDetectCaptcha:
    def test_no_captcha(self):
        platform = _make_platform()
        page = AsyncMock()
        page.url = "https://www.indeed.com/jobs"
        page.query_selector = AsyncMock(return_value=None)

        result = asyncio.run(platform.detect_captcha(page))
        assert result is False

    def test_visible_captcha_detected(self):
        platform = _make_platform()
        el = AsyncMock()
        el.is_visible = AsyncMock(return_value=True)
        page = AsyncMock()
        page.url = "https://www.indeed.com/jobs"
        page.query_selector = AsyncMock(return_value=el)

        result = asyncio.run(platform.detect_captcha(page))
        assert result is True

    def test_invisible_captcha_ignored(self):
        platform = _make_platform()
        el = AsyncMock()
        el.is_visible = AsyncMock(return_value=False)
        page = AsyncMock()
        page.url = "https://www.indeed.com/jobs"
        page.query_selector = AsyncMock(return_value=el)

        result = asyncio.run(platform.detect_captcha(page))
        assert result is False

    def test_captcha_url_pattern(self):
        platform = _make_platform()
        page = AsyncMock()
        page.url = "https://www.indeed.com/captcha/verify"
        page.query_selector = AsyncMock(return_value=None)

        result = asyncio.run(platform.detect_captcha(page))
        assert result is True


# ------------------------------------------------------------------
# check_and_abort_on_captcha
# ------------------------------------------------------------------

class TestCheckAndAbortOnCaptcha:
    def test_no_captcha_passes(self):
        platform = _make_platform()
        page = AsyncMock()
        page.url = "https://www.indeed.com/jobs"
        page.query_selector = AsyncMock(return_value=None)

        asyncio.run(platform.check_and_abort_on_captcha(page, retry_seconds=0))
        # Should not raise

    def test_captcha_raises(self):
        platform = _make_platform()
        el = AsyncMock()
        el.is_visible = AsyncMock(return_value=True)
        page = AsyncMock()
        page.url = "https://www.indeed.com/jobs"
        page.query_selector = AsyncMock(return_value=el)

        with pytest.raises(CaptchaDetectedError):
            asyncio.run(platform.check_and_abort_on_captcha(page, retry_seconds=0))
