"""Tests for the hover-target safety filter in anti_detect.

Regression coverage for the ZipRecruiter "Save & Exit" mid-flow popup:
`simulate_organic_behavior` previously hovered random a/button elements
including close-glyph buttons in iframe corners, sweeping the mouse
through them and triggering exit-confirmation dialogs.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from auto_applier.browser.anti_detect import _is_safe_hover_target


def _run(coro):
    return asyncio.run(coro)


def _make_element(
    aria_label: str | None = None,
    class_name: str | None = None,
    inner_text: str = "",
    box: dict | None = None,
    viewport: dict | None = None,
):
    """Build a MagicMock element with the async surface used by the filter."""
    el = MagicMock()

    async def _attr(name):
        return {"aria-label": aria_label, "class": class_name}.get(name)

    el.get_attribute = AsyncMock(side_effect=_attr)
    el.inner_text = AsyncMock(return_value=inner_text)
    el.bounding_box = AsyncMock(return_value=box)
    el.evaluate = AsyncMock(return_value=viewport)
    return el


class TestHoverTargetFilter:
    def test_rejects_close_aria_label(self):
        el = _make_element(
            aria_label="Close",
            inner_text="",
            box={"x": 100, "y": 200, "width": 30, "height": 30},
            viewport={"w": 1280, "h": 800},
        )
        assert _run(_is_safe_hover_target(el)) is False

    def test_rejects_x_glyph_text(self):
        el = _make_element(
            aria_label=None,
            inner_text="X",
            box={"x": 100, "y": 200, "width": 30, "height": 30},
            viewport={"w": 1280, "h": 800},
        )
        assert _run(_is_safe_hover_target(el)) is False

    def test_accepts_normal_submit_button(self):
        el = _make_element(
            aria_label="Submit application",
            class_name="btn btn-primary",
            inner_text="Submit",
            box={"x": 400, "y": 500, "width": 120, "height": 40},
            viewport={"w": 1280, "h": 800},
        )
        assert _run(_is_safe_hover_target(el)) is True
