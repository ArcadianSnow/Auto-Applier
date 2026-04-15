"""Tests for atomic CSV rewrites.

The non-atomic truncate-then-write pattern can leave user data
corrupted after a crash mid-write. _atomic_rewrite() must write to a
temp file and os.replace() it over the original, so a reader always
sees either the old complete file or the new complete file.
"""
from pathlib import Path

import pytest

from auto_applier.storage.repository import _atomic_rewrite


def test_rewrite_replaces_contents(tmp_path):
    path = tmp_path / "sample.csv"
    path.write_text("old,content\n1,2\n")

    _atomic_rewrite(
        path,
        headers=["job_id", "status"],
        rows=[{"job_id": "a", "status": "applied"}],
    )

    text = path.read_text()
    assert "job_id,status" in text
    assert "a,applied" in text
    assert "old,content" not in text


def test_rewrite_leaves_no_tmp_file_behind(tmp_path):
    path = tmp_path / "data.csv"
    _atomic_rewrite(path, headers=["x"], rows=[{"x": "1"}])
    siblings = list(tmp_path.iterdir())
    assert path in siblings
    assert not any(p.suffix == ".tmp" for p in siblings)


def test_rewrite_handles_empty_rows(tmp_path):
    path = tmp_path / "empty.csv"
    _atomic_rewrite(path, headers=["a", "b"], rows=[])
    text = path.read_text()
    # Header only — no data rows
    assert text.strip() == "a,b"
