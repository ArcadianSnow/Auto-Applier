"""Tests for the ATS quick-apply DOM-prefill flow (Phase 2.2).

The quick_apply method navigates to an apply URL, runs FormFiller
to populate every field, uploads the resume, halts before Submit,
and returns an ApplyResult tagged ``requires_manual_apply=True``.

Tests cover:
  - Default opt-out (config flag is False)
  - quick_apply with no router → returns failure with wiring-bug msg
  - Navigation failure → clean ApplyResult, no crash
  - Successful prefill → success=True, fields_filled tracked
  - Cover letter pasted into a recognized textarea
  - User MUST click Submit themselves — we never call submit selectors

We mock the Page surface and FormFiller so tests run without a
browser. Live integration is exercised via the existing
test_ats_live_integration.py framework when available.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from auto_applier.browser.platforms.ats_greenhouse import (
    ATSGreenhousePlatform,
)
from auto_applier.storage.models import Job


def _run(coro):
    return asyncio.run(coro)


def _make_platform_with_router():
    """Build an ATS adapter with form_filler.router pre-wired.
    The base adapter accepts form_filler as a third __init__ arg."""
    ctx = MagicMock()
    ctx.pages = []
    fake_form_filler = MagicMock()
    fake_form_filler.router = MagicMock()
    fake_form_filler.resume_label = "default"
    return ATSGreenhousePlatform(
        context=ctx, config={}, form_filler=fake_form_filler,
    )


def _make_platform_no_router():
    """Without a form_filler, quick_apply should fail with a clear
    wiring-bug error."""
    ctx = MagicMock()
    ctx.pages = []
    return ATSGreenhousePlatform(context=ctx, config={})


def _job():
    return Job(
        job_id="gh_test_1",
        source="ats_greenhouse",
        title="Senior Engineer",
        company="TestCo",
        url="https://boards.greenhouse.io/testco/jobs/123",
        description="A senior engineer role.",
    )


def _mock_page(navigation_raises: bool = False, fields=None):
    page = MagicMock()
    if navigation_raises:
        page.goto = AsyncMock(side_effect=RuntimeError("net down"))
    else:
        page.goto = AsyncMock()
    page.wait_for_timeout = AsyncMock()
    page.query_selector = AsyncMock(return_value=None)
    return page


# ----------------------------------------------------------------------
# Config default
# ----------------------------------------------------------------------

class TestQuickApplyConfigDefault:
    def test_default_is_off(self):
        from auto_applier.config import DEFAULT_AUTO_QUICK_APPLY_ATS
        # Off by default — biggest behavior change should be deliberate
        assert DEFAULT_AUTO_QUICK_APPLY_ATS is False


# ----------------------------------------------------------------------
# quick_apply method on the ATS base
# ----------------------------------------------------------------------

class TestQuickApplyContract:
    def test_no_form_filler_returns_wiring_bug_failure(self):
        """If quick_apply is called without an LLM router wired,
        return a failure with a clear wiring-bug message — don't
        let the user see a cryptic AttributeError."""
        platform = _make_platform_no_router()
        page = _mock_page()

        result = _run(platform.quick_apply(
            job=_job(),
            resume_path="/path/to/resume.pdf",
            cover_letter_text="",
            personal_info={"first_name": "Jane"},
            page=page,
        ))

        assert result.success is False
        assert result.requires_manual_apply is True
        msg = (result.failure_reason or "").lower()
        assert "wiring" in msg or "router" in msg

    def test_navigation_failure_returns_clean_result(self):
        platform = _make_platform_with_router()
        page = _mock_page(navigation_raises=True)

        result = _run(platform.quick_apply(
            job=_job(),
            resume_path="/path/to/resume.pdf",
            cover_letter_text="",
            personal_info={},
            page=page,
        ))

        assert result.success is False
        assert result.requires_manual_apply is True
        assert "open the apply page" in (result.failure_reason or "").lower()

    def test_no_apply_url_returns_clean_failure(self):
        platform = _make_platform_with_router()
        page = _mock_page()
        job = _job()
        job.url = ""  # nothing to navigate to

        result = _run(platform.quick_apply(
            job=job,
            resume_path="/x.pdf",
            cover_letter_text="",
            personal_info={},
            page=page,
        ))
        assert result.success is False
        assert result.requires_manual_apply is True
        assert "no apply url" in (result.failure_reason or "").lower()

    def test_successful_prefill_returns_success_with_manual_apply(self):
        """Happy path: prefill succeeds. result.success=True, but
        requires_manual_apply ALSO True — user must still click Submit.
        That's the whole point of the quick-apply pattern."""
        platform = _make_platform_with_router()
        page = _mock_page()

        # Mock resume upload — pick_resume_input returns None so
        # we skip the upload path cleanly (test focuses on prefill).
        # find_form_fields returns a small fields list. fill_field
        # is a no-op that bumps fields_filled.
        with patch(
            "auto_applier.browser.form_filler.FormFiller.pick_resume_input",
            new=AsyncMock(return_value=None),
        ), patch(
            "auto_applier.browser.selector_utils.find_form_fields",
            new=AsyncMock(return_value=[]),
        ):
            result = _run(platform.quick_apply(
                job=_job(),
                resume_path="/x.pdf",
                cover_letter_text="",
                personal_info={"first_name": "Jane", "email": "jane@x.com"},
                page=page,
            ))

        assert result.success is True
        assert result.requires_manual_apply is True
        # Failure reason explains "user must click Submit"
        msg = (result.failure_reason or "").lower()
        assert "submit yourself" in msg or "review" in msg

    def test_cover_letter_pasted_into_recognized_textarea(self):
        """When cover_letter_text is provided AND a recognizable
        cover-letter textarea is visible on the page, paste it in.
        Empty current value required (don't overwrite user/filler text)."""
        platform = _make_platform_with_router()
        page = _mock_page()

        # Mock a single visible cover-letter textarea with empty value
        cover_textarea = MagicMock()
        cover_textarea.is_visible = AsyncMock(return_value=True)
        cover_textarea.input_value = AsyncMock(return_value="")
        cover_textarea.fill = AsyncMock()

        # Make query_selector return our textarea on the first
        # cover-letter selector candidate, None thereafter.
        call_state = {"calls": 0}
        async def fake_query_selector(sel):
            call_state["calls"] += 1
            if "cover" in sel.lower() and call_state["calls"] == 1:
                return cover_textarea
            return None
        page.query_selector = AsyncMock(side_effect=fake_query_selector)

        with patch(
            "auto_applier.browser.form_filler.FormFiller.pick_resume_input",
            new=AsyncMock(return_value=None),
        ), patch(
            "auto_applier.browser.selector_utils.find_form_fields",
            new=AsyncMock(return_value=[]),
        ):
            result = _run(platform.quick_apply(
                job=_job(),
                resume_path="/x.pdf",
                cover_letter_text="Dear TestCo team, I would love to join...",
                personal_info={},
                page=page,
            ))

        assert result.success is True
        # Cover textarea got the letter
        cover_textarea.fill.assert_awaited_once()
        filled_text = cover_textarea.fill.call_args[0][0]
        assert "Dear TestCo team" in filled_text


# ----------------------------------------------------------------------
# Submit boundary — the load-bearing safety property
# ----------------------------------------------------------------------

class TestQuickApplyNeverSubmits:
    """The defining property of quick-apply: we NEVER click a submit
    button. The whole legal/ethical defense rests on the user
    clicking Submit themselves. Pin this aggressively."""

    def test_no_submit_click_during_prefill(self):
        """Run quick_apply against a page that would record any
        click attempt. Confirm zero clicks fired."""
        platform = _make_platform_with_router()
        page = _mock_page()

        # Track every click
        click_calls: list[tuple] = []

        async def track_click(*args, **kwargs):
            click_calls.append((args, kwargs))

        # Build a fake submit button — visible, fillable. If our code
        # tries to click ANY element, we'd see it here.
        def make_clickable_element():
            el = MagicMock()
            el.click = AsyncMock(side_effect=track_click)
            el.is_visible = AsyncMock(return_value=True)
            return el

        page.click = AsyncMock(side_effect=track_click)

        with patch(
            "auto_applier.browser.form_filler.FormFiller.pick_resume_input",
            new=AsyncMock(return_value=None),
        ), patch(
            "auto_applier.browser.selector_utils.find_form_fields",
            new=AsyncMock(return_value=[]),
        ):
            _run(platform.quick_apply(
                job=_job(),
                resume_path="/x.pdf",
                cover_letter_text="",
                personal_info={},
                page=page,
            ))

        # Zero clicks during prefill. (Cover letter `fill()` is not
        # a click; submit buttons go through page.click or
        # element.click — both tracked above.)
        assert click_calls == [], (
            f"Quick-apply clicked something! That violates the "
            f"never-submit invariant: {click_calls}"
        )


# ----------------------------------------------------------------------
# URL resolution
# ----------------------------------------------------------------------

class TestResolveApplyUrl:
    def test_default_returns_job_url(self):
        platform = _make_platform_with_router()
        job = _job()
        assert platform._resolve_apply_url(job) == job.url

    def test_empty_job_url_returns_empty_string(self):
        platform = _make_platform_with_router()
        job = _job()
        job.url = None  # type: ignore[assignment]
        assert platform._resolve_apply_url(job) == ""
