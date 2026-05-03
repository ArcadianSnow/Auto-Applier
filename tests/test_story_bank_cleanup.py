"""Tests for the prune-orphan-stories CLI helper.

Mirrors the temp-path patterns in tests/test_story_bank.py — every test
monkeypatches the data file paths so the user's real story_bank.json
and applications.csv are untouched.
"""
from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from auto_applier import main as main_module
from auto_applier.main import cli
from auto_applier.resume import story_bank


def _write_apps_csv(path: Path, rows: list[dict]) -> None:
    fieldnames = ["job_id", "status", "failure_reason"]
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for r in rows:
            writer.writerow({k: r.get(k, "") for k in fieldnames})


def _make_story_dict(job_id: str, title: str = "Untitled") -> dict:
    return {
        "title": title,
        "question_prompt": "Tell me about a time...",
        "situation": "s", "task": "t", "action": "a",
        "result": "r", "reflection": "ref",
        "job_id": job_id,
        "company": "Acme",
        "job_title": "Analyst",
        "resume_label": "data",
        "created_at": "2026-04-30T00:00:00+00:00",
    }


@pytest.fixture
def isolated_data(tmp_path, monkeypatch):
    """Redirect every path the prune helper touches into a temp dir."""
    apps_csv = tmp_path / "applications.csv"
    bank_file = tmp_path / "story_bank.json"
    backup_dir = tmp_path / ".backups"

    # auto_applier.config defines the canonical paths; the helper imports
    # them inside the function body, so we patch the source modules.
    from auto_applier import config as cfg
    monkeypatch.setattr(cfg, "APPLICATIONS_CSV", apps_csv)
    monkeypatch.setattr(cfg, "BACKUP_DIR", backup_dir)
    monkeypatch.setattr(story_bank, "STORY_BANK_FILE", bank_file)

    return {
        "apps_csv": apps_csv,
        "bank_file": bank_file,
        "backup_dir": backup_dir,
        "tmp_path": tmp_path,
    }


def _run_prune(args: list[str] | None = None) -> "object":
    runner = CliRunner()
    return runner.invoke(cli, ["prune-orphan-stories", *(args or [])])


class TestPruneOrphanStories:
    def test_applied_stories_kept(self, isolated_data):
        """Stories tied to job_ids with status='applied' must survive."""
        _write_apps_csv(isolated_data["apps_csv"], [
            {"job_id": "job-applied-1", "status": "applied"},
            {"job_id": "job-applied-2", "status": "applied"},
        ])
        bank = [
            _make_story_dict("job-applied-1", "kept-1"),
            _make_story_dict("job-applied-2", "kept-2"),
        ]
        isolated_data["bank_file"].write_text(json.dumps(bank), encoding="utf-8")

        result = _run_prune(["--yes"])
        assert result.exit_code == 0, result.output

        survivors = json.loads(isolated_data["bank_file"].read_text("utf-8"))
        titles = sorted(s["title"] for s in survivors)
        assert titles == ["kept-1", "kept-2"]
        assert "Nothing to prune." in result.output

    def test_failed_stories_removed(self, isolated_data):
        """Stories whose Application was reclassified to 'failed' get pruned."""
        _write_apps_csv(isolated_data["apps_csv"], [
            {"job_id": "job-applied", "status": "applied"},
            {"job_id": "job-failed", "status": "failed",
             "failure_reason": "false-positive submit"},
        ])
        bank = [
            _make_story_dict("job-applied", "kept"),
            _make_story_dict("job-failed", "orphan-1"),
            _make_story_dict("job-failed", "orphan-2"),
            _make_story_dict("job-failed", "orphan-3"),
        ]
        isolated_data["bank_file"].write_text(json.dumps(bank), encoding="utf-8")

        result = _run_prune(["--yes"])
        assert result.exit_code == 0, result.output

        survivors = json.loads(isolated_data["bank_file"].read_text("utf-8"))
        assert len(survivors) == 1
        assert survivors[0]["title"] == "kept"
        # Backup must be created before the write.
        backups = list(isolated_data["backup_dir"].glob("story_bank.*.json"))
        assert len(backups) == 1

    def test_unknown_job_ids_removed(self, isolated_data):
        """Stories whose job_id has no row in applications.csv are orphans."""
        _write_apps_csv(isolated_data["apps_csv"], [
            {"job_id": "job-applied", "status": "applied"},
        ])
        bank = [
            _make_story_dict("job-applied", "kept"),
            _make_story_dict("job-vanished-from-csv", "ghost"),
            _make_story_dict("", "blank-job-id"),
        ]
        isolated_data["bank_file"].write_text(json.dumps(bank), encoding="utf-8")

        result = _run_prune(["--yes"])
        assert result.exit_code == 0, result.output

        survivors = json.loads(isolated_data["bank_file"].read_text("utf-8"))
        titles = [s["title"] for s in survivors]
        assert titles == ["kept"]

    def test_dry_run_does_not_write(self, isolated_data):
        """--dry-run must report orphans but leave the file (and backups) alone."""
        _write_apps_csv(isolated_data["apps_csv"], [
            {"job_id": "job-applied", "status": "applied"},
        ])
        bank = [
            _make_story_dict("job-applied", "kept"),
            _make_story_dict("job-orphan", "to-remove"),
        ]
        original = json.dumps(bank)
        isolated_data["bank_file"].write_text(original, encoding="utf-8")

        result = _run_prune(["--dry-run"])
        assert result.exit_code == 0, result.output
        assert "--dry-run: no file changes made." in result.output

        # File contents byte-identical to the original.
        assert isolated_data["bank_file"].read_text("utf-8") == original
        # No backup directory written either (dry-run never reaches mkdir).
        if isolated_data["backup_dir"].exists():
            assert list(isolated_data["backup_dir"].glob("*")) == []
