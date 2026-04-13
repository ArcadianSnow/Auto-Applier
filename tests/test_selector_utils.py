"""Tests for browser/selector_utils.py — _classify_element and FormField."""

import asyncio
from unittest.mock import AsyncMock

import pytest

from auto_applier.browser.selector_utils import FormField, _classify_element


def _mock_element(tag="input", input_type="text", options=None):
    """Build a mock element handle that mimics Playwright's async API."""
    el = AsyncMock()
    el.evaluate = AsyncMock(return_value=tag)
    el.get_attribute = AsyncMock(return_value=input_type if tag == "input" else None)
    if options:
        opt_els = []
        for text, value in options:
            opt_el = AsyncMock()
            opt_el.inner_text = AsyncMock(return_value=text)
            opt_el.get_attribute = AsyncMock(return_value=value)
            opt_els.append(opt_el)
        el.query_selector_all = AsyncMock(return_value=opt_els)
    else:
        el.query_selector_all = AsyncMock(return_value=[])
    return el


class TestClassifyElement:
    def test_text_input(self):
        el = _mock_element(tag="input", input_type="text")
        page = AsyncMock()
        result = asyncio.run(_classify_element(el, "First Name", page))
        assert result is not None
        assert result.field_type == "text"
        assert result.label == "First Name"

    def test_textarea(self):
        el = _mock_element(tag="textarea")
        page = AsyncMock()
        result = asyncio.run(_classify_element(el, "Cover Letter", page))
        assert result.field_type == "textarea"

    def test_select_with_options(self):
        el = _mock_element(tag="select", options=[
            ("Yes", "yes"),
            ("No", "no"),
        ])
        page = AsyncMock()
        result = asyncio.run(_classify_element(el, "Authorized?", page))
        assert result.field_type == "select"
        assert result.options == ["Yes", "No"]

    def test_select_empty_options(self):
        el = _mock_element(tag="select", options=[])
        page = AsyncMock()
        result = asyncio.run(_classify_element(el, "Choose", page))
        assert result.field_type == "select"
        assert result.options == []

    def test_radio(self):
        el = _mock_element(tag="input", input_type="radio")
        page = AsyncMock()
        result = asyncio.run(_classify_element(el, "Yes/No", page))
        assert result.field_type == "radio"

    def test_checkbox(self):
        el = _mock_element(tag="input", input_type="checkbox")
        page = AsyncMock()
        result = asyncio.run(_classify_element(el, "I agree", page))
        assert result.field_type == "checkbox"

    def test_file(self):
        el = _mock_element(tag="input", input_type="file")
        page = AsyncMock()
        result = asyncio.run(_classify_element(el, "Resume", page))
        assert result.field_type == "file"

    def test_date(self):
        el = _mock_element(tag="input", input_type="date")
        page = AsyncMock()
        result = asyncio.run(_classify_element(el, "Start Date", page))
        assert result.field_type == "date"

    def test_datetime_local(self):
        el = _mock_element(tag="input", input_type="datetime-local")
        page = AsyncMock()
        result = asyncio.run(_classify_element(el, "Interview Time", page))
        assert result.field_type == "date"

    def test_number(self):
        el = _mock_element(tag="input", input_type="number")
        page = AsyncMock()
        result = asyncio.run(_classify_element(el, "Years", page))
        assert result.field_type == "number"

    def test_unknown_tag_defaults_to_text(self):
        el = _mock_element(tag="div")
        page = AsyncMock()
        result = asyncio.run(_classify_element(el, "Custom", page))
        assert result.field_type == "text"

    def test_evaluate_failure_returns_none(self):
        el = AsyncMock()
        el.evaluate = AsyncMock(side_effect=Exception("detached"))
        page = AsyncMock()
        result = asyncio.run(_classify_element(el, "Label", page))
        assert result is None


class TestFormFieldDataclass:
    def test_default_options(self):
        el = AsyncMock()
        field = FormField(label="Name", element=el, field_type="text")
        assert field.options == []

    def test_with_options(self):
        el = AsyncMock()
        field = FormField(label="Q", element=el, field_type="select", options=["A", "B"])
        assert field.options == ["A", "B"]
