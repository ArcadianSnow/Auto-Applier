"""Truncated-JSON repair tests (llm/complete.py) — found live: qwen3:8b emitted a
complete object minus the final ``}`` plus newline padding, losing the whole
(honest!) copilot answer to a parse failure."""

from __future__ import annotations

import pytest

from auto_applier.llm.complete import repair_truncated_json


def test_repairs_missing_closing_brace():
    raw = '{"verdict": "no", "gaps": ["Debezium"]' + "\n" * 50
    assert repair_truncated_json(raw) == {"verdict": "no", "gaps": ["Debezium"]}


def test_repairs_nested_truncation():
    raw = '{"a": {"b": [1, 2'
    assert repair_truncated_json(raw) == {"a": {"b": [1, 2]}}


def test_repairs_dangling_string():
    raw = '{"answer": "watermark sync'
    assert repair_truncated_json(raw) == {"answer": "watermark sync"}


def test_braces_inside_strings_dont_count():
    raw = '{"text": "an { unbalanced ] string", "n": 1'
    assert repair_truncated_json(raw) == {"text": "an { unbalanced ] string", "n": 1}


def test_escaped_quotes_inside_strings():
    raw = '{"text": "she said \\"hi\\"", "n": [1'
    assert repair_truncated_json(raw) == {"text": 'she said "hi"', "n": [1]}


def test_already_valid_json_roundtrips():
    assert repair_truncated_json('{"ok": true}') == {"ok": True}
    assert repair_truncated_json("[1, 2") == [1, 2]


@pytest.mark.parametrize("raw", [
    "",                       # nothing
    "   \n  ",                # whitespace only
    "verdict: no",            # prose, not JSON
    '{"a": 1}}',              # mismatched extra closer
    '{"a": ]',                # wrong closer for the open brace
])
def test_unrepairable_returns_none(raw):
    assert repair_truncated_json(raw) is None
