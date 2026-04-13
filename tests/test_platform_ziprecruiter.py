"""Tests for ZipRecruiter platform adapter — external detection, success, submit detection."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from auto_applier.browser.platforms.ziprecruiter import ZipRecruiterPlatform
from auto_applier.storage.models import Job


def _make_platform():
    ctx = MagicMock()
    ctx.pages = [MagicMock()]
    return ZipRecruiterPlatform(context=ctx, config={})


def _run(coro):
    return asyncio.run(coro)


# ------------------------------------------------------------------
# Success Detection
# ------------------------------------------------------------------

class TestCheckSuccess:
    def test_success_by_selector(self):
        platform = _make_platform()
        el = MagicMock()
        page = MagicMock()
        page.inner_text = AsyncMock(return_value="stuff")

        async def do():
            with patch.object(platform, "safe_query", return_value=el):
                return await platform._check_success(page)

        assert _run(do()) is True

    def test_success_by_phrase(self):
        platform = _make_platform()
        page = MagicMock()
        page.inner_text = AsyncMock(return_value="Congratulations! You've applied.")

        async def do():
            with patch.object(platform, "safe_query", return_value=None):
                return await platform._check_success(page)

        assert _run(do()) is True

    def test_application_received(self):
        platform = _make_platform()
        page = MagicMock()
        page.inner_text = AsyncMock(return_value="Your application received!")

        async def do():
            with patch.object(platform, "safe_query", return_value=None):
                return await platform._check_success(page)

        assert _run(do()) is True

    def test_no_success(self):
        platform = _make_platform()
        page = MagicMock()
        page.inner_text = AsyncMock(return_value="Fill out required fields.")

        async def do():
            with patch.object(platform, "safe_query", return_value=None):
                return await platform._check_success(page)

        assert _run(do()) is False


# ------------------------------------------------------------------
# External Apply Detection
# ------------------------------------------------------------------

class TestIsExternalApply:
    def test_external_by_selector(self):
        platform = _make_platform()
        el = MagicMock()
        page = MagicMock()
        page.query_selector = AsyncMock(return_value=el)

        assert _run(platform._is_external_apply(page)) is True

    def test_external_by_button_text(self):
        platform = _make_platform()
        link = MagicMock()
        link.inner_text = AsyncMock(return_value="Apply on company site")
        page = MagicMock()
        page.query_selector = AsyncMock(return_value=None)
        page.query_selector_all = AsyncMock(return_value=[link])

        assert _run(platform._is_external_apply(page)) is True

    def test_not_external(self):
        platform = _make_platform()
        page = MagicMock()
        page.query_selector = AsyncMock(return_value=None)
        page.query_selector_all = AsyncMock(return_value=[])

        assert _run(platform._is_external_apply(page)) is False


# ------------------------------------------------------------------
# External Redirect Detection
# ------------------------------------------------------------------

class TestCheckExternalRedirect:
    def test_redirect_away_from_zr(self):
        platform = _make_platform()
        page = MagicMock()
        page.url = "https://careers.somecompany.com/apply"

        assert _run(platform._check_external_redirect(page)) is True

    def test_still_on_zr(self):
        platform = _make_platform()
        page = MagicMock()
        page.url = "https://www.ziprecruiter.com/jobs/apply/123"
        platform.context.pages = [page]

        assert _run(platform._check_external_redirect(page)) is False

    def test_new_tab_detected(self):
        platform = _make_platform()
        page = MagicMock()
        page.url = "https://www.ziprecruiter.com/jobs/123"
        extra = MagicMock()
        extra.close = AsyncMock()
        platform.context.pages = [page, extra]

        assert _run(platform._check_external_redirect(page)) is True


# ------------------------------------------------------------------
# Submit Step Detection
# ------------------------------------------------------------------

class TestIsSubmitStep:
    def test_submit_button_found(self):
        platform = _make_platform()
        el = MagicMock()
        el.inner_text = AsyncMock(return_value="Submit Application")
        page = MagicMock()
        page.query_selector = AsyncMock(return_value=el)

        assert _run(platform._is_submit_step(page)) is True

    def test_one_click_apply(self):
        platform = _make_platform()
        btn = MagicMock()
        btn.inner_text = AsyncMock(return_value="1-Click Apply")
        page = MagicMock()
        page.query_selector = AsyncMock(return_value=None)
        page.query_selector_all = AsyncMock(return_value=[btn])

        assert _run(platform._is_submit_step(page)) is True

    def test_continue_is_not_submit(self):
        platform = _make_platform()
        btn = MagicMock()
        btn.inner_text = AsyncMock(return_value="Continue")
        page = MagicMock()
        page.query_selector = AsyncMock(return_value=None)
        page.query_selector_all = AsyncMock(return_value=[btn])

        assert _run(platform._is_submit_step(page)) is False


# ------------------------------------------------------------------
# check_is_external (override)
# ------------------------------------------------------------------

class TestCheckIsExternal:
    def test_apply_button_visible_means_not_external(self):
        platform = _make_platform()
        el = MagicMock()
        el.is_visible = AsyncMock(return_value=True)
        page = MagicMock()
        page.query_selector = AsyncMock(return_value=el)
        platform._page = page

        job = Job(job_id="zr-1", title="SWE", company="Acme", url="https://zr.com/jobs/1")
        assert _run(platform.check_is_external(job)) is False

    def test_no_apply_button_means_external(self):
        platform = _make_platform()
        page = MagicMock()
        page.is_closed.return_value = False
        page.query_selector = AsyncMock(return_value=None)
        platform._page = page

        job = Job(job_id="zr-1", title="SWE", company="Acme", url="https://zr.com/jobs/1")
        assert _run(platform.check_is_external(job)) is True


# ------------------------------------------------------------------
# Build Result
# ------------------------------------------------------------------

class TestBuildResult:
    def test_with_form_filler(self):
        platform = _make_platform()
        filler = MagicMock()
        filler.gaps = []
        filler.resume_label = "engineer"
        filler.cover_letter_generated = True
        filler.fields_filled = 3
        filler.fields_total = 4
        filler.used_llm = True
        platform.form_filler = filler

        result = platform._build_result(success=True)
        assert result.success is True
        assert result.cover_letter_generated is True

    def test_without_form_filler(self):
        platform = _make_platform()
        platform.form_filler = None
        result = platform._build_result(success=False, failure_reason="External")
        assert result.failure_reason == "External"


# ------------------------------------------------------------------
# Constants
# ------------------------------------------------------------------

# ------------------------------------------------------------------
# Validation Error Scanning
# ------------------------------------------------------------------

class TestScanValidationErrors:
    def test_finds_visible_error(self):
        platform = _make_platform()
        el = MagicMock()
        el.is_visible = AsyncMock(return_value=True)
        el.inner_text = AsyncMock(return_value="Required field")
        page = MagicMock()
        page.query_selector_all = AsyncMock(return_value=[el])

        result = _run(platform._scan_validation_errors(page))
        assert result == "Required field"

    def test_no_errors(self):
        platform = _make_platform()
        page = MagicMock()
        page.query_selector_all = AsyncMock(return_value=[])

        result = _run(platform._scan_validation_errors(page))
        assert result == ""


# ------------------------------------------------------------------
# Apply Result Logging
# ------------------------------------------------------------------

class TestBuildResultLogging:
    def test_success_logs(self, caplog):
        import logging
        platform = _make_platform()
        filler = MagicMock()
        filler.gaps = []
        filler.resume_label = "eng"
        filler.cover_letter_generated = False
        filler.fields_filled = 2
        filler.fields_total = 3
        filler.used_llm = False
        platform.form_filler = filler

        with caplog.at_level(logging.INFO):
            platform._build_result(success=True)
        assert "Apply result: success" in caplog.text

    def test_failure_logs(self, caplog):
        import logging
        platform = _make_platform()
        platform.form_filler = None

        with caplog.at_level(logging.INFO):
            platform._build_result(success=False, failure_reason="External")
        assert "Apply result: failed" in caplog.text


class TestZipRecruiterConfig:
    def test_source_id(self):
        assert ZipRecruiterPlatform.source_id == "ziprecruiter"

    def test_display_name(self):
        assert ZipRecruiterPlatform.display_name == "ZipRecruiter"

    def test_has_dead_listing_selectors(self):
        assert len(ZipRecruiterPlatform.dead_listing_selectors) > 0
