"""Tests for Dice platform adapter — ATS redirect, success, submit detection."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from auto_applier.browser.platforms.dice import DicePlatform


def _make_platform():
    ctx = MagicMock()
    ctx.pages = [MagicMock()]
    return DicePlatform(context=ctx, config={})


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
        page.inner_text = AsyncMock(return_value="Thank you for applying! Application submitted.")

        async def do():
            with patch.object(platform, "safe_query", return_value=None):
                return await platform._check_success(page)

        assert _run(do()) is True

    def test_no_success(self):
        platform = _make_platform()
        page = MagicMock()
        page.inner_text = AsyncMock(return_value="Complete all required fields.")

        async def do():
            with patch.object(platform, "safe_query", return_value=None):
                return await platform._check_success(page)

        assert _run(do()) is False


# ------------------------------------------------------------------
# ATS Redirect Detection
# ------------------------------------------------------------------

class TestCheckAtsRedirect:
    """`_check_ats_redirect` returns one of:
      - None: no new tab opened
      - Page object: new tab is on dice.com/job-applications (internal)
      - "external": new tab is off-site ATS
    """

    def test_external_tab_detected(self):
        platform = _make_platform()
        extra = MagicMock()
        extra.close = AsyncMock()
        extra.wait_for_load_state = AsyncMock()
        # Off-site URL — should be classified as external.
        extra.url = "https://www.randstadusa.com/jobs/apply/123"
        platform.context.pages = [MagicMock(), extra]

        assert _run(platform._check_ats_redirect(pages_before=1)) == "external"
        extra.close.assert_called_once()

    def test_internal_dice_tab_returns_page(self):
        platform = _make_platform()
        extra = MagicMock()
        extra.close = AsyncMock()
        extra.bring_to_front = AsyncMock()
        extra.wait_for_load_state = AsyncMock()
        extra.url = (
            "https://www.dice.com/job-applications/abc-123/start-apply"
        )
        platform.context.pages = [MagicMock(), extra]

        result = _run(platform._check_ats_redirect(pages_before=1))
        assert result is extra
        # Internal tab should NOT be closed — caller will use it.
        extra.close.assert_not_called()

    def test_no_new_tab(self):
        platform = _make_platform()
        platform.context.pages = [MagicMock()]

        assert _run(platform._check_ats_redirect(pages_before=1)) is None


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

    def test_submit_by_text_scan(self):
        platform = _make_platform()
        btn = MagicMock()
        btn.inner_text = AsyncMock(return_value="Submit")
        page = MagicMock()
        page.query_selector = AsyncMock(return_value=None)
        page.query_selector_all = AsyncMock(return_value=[btn])

        assert _run(platform._is_submit_step(page)) is True

    def test_next_is_not_submit(self):
        platform = _make_platform()
        btn = MagicMock()
        btn.inner_text = AsyncMock(return_value="Next")
        page = MagicMock()
        page.query_selector = AsyncMock(return_value=None)
        page.query_selector_all = AsyncMock(return_value=[btn])

        assert _run(platform._is_submit_step(page)) is False


# ------------------------------------------------------------------
# Build Result
# ------------------------------------------------------------------

class TestBuildResult:
    def test_with_form_filler(self):
        platform = _make_platform()
        filler = MagicMock()
        filler.gaps = []
        filler.resume_label = "data_analyst"
        filler.cover_letter_generated = False
        filler.fields_filled = 5
        filler.fields_total = 7
        filler.used_llm = True
        platform.form_filler = filler

        result = platform._build_result(success=True)
        assert result.success is True
        assert result.resume_used == "data_analyst"
        assert result.fields_filled == 5

    def test_without_form_filler(self):
        platform = _make_platform()
        platform.form_filler = None

        result = platform._build_result(success=False, failure_reason="No button")
        assert result.failure_reason == "No button"


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
        el.inner_text = AsyncMock(return_value="This field is required")
        page = MagicMock()
        page.query_selector_all = AsyncMock(return_value=[el])

        result = _run(platform._scan_validation_errors(page))
        assert result == "This field is required"

    def test_ignores_hidden_errors(self):
        platform = _make_platform()
        el = MagicMock()
        el.is_visible = AsyncMock(return_value=False)
        el.inner_text = AsyncMock(return_value="Error")
        page = MagicMock()
        page.query_selector_all = AsyncMock(return_value=[el])

        result = _run(platform._scan_validation_errors(page))
        assert result == ""

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
    def test_success_result_logs(self, caplog):
        import logging
        platform = _make_platform()
        filler = MagicMock()
        filler.gaps = []
        filler.resume_label = "test"
        filler.cover_letter_generated = False
        filler.fields_filled = 3
        filler.fields_total = 5
        filler.used_llm = True
        platform.form_filler = filler

        with caplog.at_level(logging.INFO):
            platform._build_result(success=True)
        assert "Apply result: success" in caplog.text

    def test_failure_result_logs(self, caplog):
        import logging
        platform = _make_platform()
        platform.form_filler = None

        with caplog.at_level(logging.INFO):
            platform._build_result(success=False, failure_reason="No button")
        assert "Apply result: failed" in caplog.text


class TestDiceConfig:
    def test_source_id(self):
        assert DicePlatform.source_id == "dice"

    def test_display_name(self):
        assert DicePlatform.display_name == "Dice"

    def test_has_dead_listing_selectors(self):
        assert len(DicePlatform.dead_listing_selectors) > 0
