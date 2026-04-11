"""Tests for browser/liveness.py pure-function classifier."""
import asyncio
from unittest.mock import AsyncMock, MagicMock

from auto_applier.browser.liveness import (
    Liveness,
    DEFAULT_DEAD_PHRASES,
    check_liveness_on_page,
)


def _fake_page(body_text: str = "", selector_counts: dict | None = None):
    """Build a mock Playwright page with configurable body text and selectors."""
    page = MagicMock()
    page.inner_text = AsyncMock(return_value=body_text)

    counts = selector_counts or {}

    def locator(sel: str):
        loc = MagicMock()
        loc_first = MagicMock()
        loc_first.count = AsyncMock(return_value=counts.get(sel, 0))
        loc_first.is_visible = AsyncMock(return_value=counts.get(sel, 0) > 0)
        loc.first = loc_first
        return loc

    page.locator = locator
    return page


def _run(coro):
    return asyncio.run(coro)


class TestCheckLivenessOnPage:
    def test_http_4xx_is_dead(self):
        page = _fake_page(body_text="Normal page")
        result = _run(check_liveness_on_page(page, [], [], response_status=404))
        assert result == Liveness.DEAD

    def test_http_5xx_is_dead(self):
        page = _fake_page(body_text="Normal page")
        result = _run(check_liveness_on_page(page, [], [], response_status=503))
        assert result == Liveness.DEAD

    def test_http_200_with_clean_page_is_live(self):
        page = _fake_page(body_text="Apply to this great role at Acme")
        result = _run(check_liveness_on_page(page, [], [], response_status=200))
        assert result == Liveness.LIVE

    def test_default_dead_phrase_triggers(self):
        page = _fake_page(
            body_text="Sorry, this job is no longer accepting applications."
        )
        result = _run(check_liveness_on_page(page, [], [], response_status=200))
        assert result == Liveness.DEAD

    def test_platform_dead_phrase_triggers(self):
        page = _fake_page(body_text="This requisition has been closed internally")
        result = _run(check_liveness_on_page(
            page, [], ["this requisition has been closed"], response_status=200,
        ))
        assert result == Liveness.DEAD

    def test_dead_selector_match_triggers(self):
        page = _fake_page(
            body_text="Job details",
            selector_counts={".jobs-details__no-longer-accepting": 1},
        )
        result = _run(check_liveness_on_page(
            page, [".jobs-details__no-longer-accepting"], [], response_status=200,
        ))
        assert result == Liveness.DEAD

    def test_dead_selector_zero_count_is_live(self):
        page = _fake_page(
            body_text="Job details",
            selector_counts={".jobs-details__no-longer-accepting": 0},
        )
        result = _run(check_liveness_on_page(
            page, [".jobs-details__no-longer-accepting"], [], response_status=200,
        ))
        assert result == Liveness.LIVE

    def test_case_insensitive_phrase_match(self):
        page = _fake_page(body_text="PAGE NOT FOUND")
        result = _run(check_liveness_on_page(page, [], [], response_status=200))
        assert result == Liveness.DEAD

    def test_body_text_error_returns_live_when_no_other_signal(self):
        page = MagicMock()
        page.inner_text = AsyncMock(side_effect=RuntimeError("page closed"))

        def locator(sel):
            loc = MagicMock()
            loc_first = MagicMock()
            loc_first.count = AsyncMock(return_value=0)
            loc_first.is_visible = AsyncMock(return_value=False)
            loc.first = loc_first
            return loc

        page.locator = locator
        # No dead signal — falls through to LIVE.
        result = _run(check_liveness_on_page(page, [], [], response_status=200))
        assert result == Liveness.LIVE

    def test_status_checked_before_other_signals(self):
        # Dead HTTP status short-circuits even if body text and selectors
        # would have classified the page as live.
        page = _fake_page(body_text="Apply today!")
        result = _run(check_liveness_on_page(page, [], [], response_status=404))
        assert result == Liveness.DEAD


class TestDefaultPhrases:
    def test_contains_common_phrases(self):
        joined = " ".join(DEFAULT_DEAD_PHRASES).lower()
        assert "no longer" in joined
        assert "expired" in joined
        assert "filled" in joined
        assert "404" in joined

    def test_all_lowercase(self):
        for p in DEFAULT_DEAD_PHRASES:
            assert p == p.lower()
