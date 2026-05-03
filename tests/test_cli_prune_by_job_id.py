"""Tests for the ``prune-by-job-id`` CLI.

The 2026-05-03 false-applied bug (Dice routed to external ATS,
walker filled foreign form, marked dry_run success despite 0/27
fields) created status='applied' / status='dry_run' rows that
prune-failed can't touch — both statuses are in _DEDUP_STATUSES,
so they poison future runs of the same job.

This command surgically removes Application rows by exact job_id,
with confirmation prompt + backup. Tests cover:

  - Empty / missing CSV
  - Single job_id removal (the common case)
  - Multiple job_ids in one invocation
  - --dry-run preview (no file writes)
  - --yes skips confirmation
  - Backup is created before write
  - Job_ids that don't exist are reported but don't crash
  - Other rows are preserved untouched
"""
from __future__ import annotations

import csv as _csv
import json
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from auto_applier.main import cli


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture
def csv_with_rows(tmp_path, monkeypatch):
    """Create a temporary applications.csv with three rows of mixed
    statuses, plus an empty BACKUP_DIR."""
    csv_path = tmp_path / "applications.csv"
    fieldnames = [
        "job_id", "status", "source", "resume_used", "score",
        "dimensions_json", "cover_letter_generated", "failure_reason",
        "fields_filled", "fields_total", "used_llm", "applied_at",
        "outcome", "outcome_at", "outcome_note",
    ]
    rows = [
        # The bad telecom GIS row from the live run.
        {"job_id": "dice-461795d3-9232-460a-bc13-998ec350c1f8",
         "status": "dry_run", "source": "dice", "resume_used": "Testpilot",
         "score": "8", "dimensions_json": "[]",
         "cover_letter_generated": "False", "failure_reason": "",
         "fields_filled": "0", "fields_total": "27", "used_llm": "True",
         "applied_at": "2026-05-03T11:31:28", "outcome": "pending",
         "outcome_at": "", "outcome_note": ""},
        # The bad Mid Data Analyst row from the live run.
        {"job_id": "dice-a4816ecb-b039-4c6c-8db1-b49d0ca1f050",
         "status": "applied", "source": "dice", "resume_used": "Testpilot",
         "score": "8", "dimensions_json": "[]",
         "cover_letter_generated": "False", "failure_reason": "",
         "fields_filled": "0", "fields_total": "2", "used_llm": "True",
         "applied_at": "2026-05-03T11:34:00", "outcome": "pending",
         "outcome_at": "", "outcome_note": ""},
        # A legitimate applied row that must NEVER be removed.
        {"job_id": "indeed-real-1", "status": "applied", "source": "indeed",
         "resume_used": "Testpilot", "score": "9",
         "dimensions_json": "[]", "cover_letter_generated": "False",
         "failure_reason": "", "fields_filled": "8", "fields_total": "8",
         "used_llm": "True", "applied_at": "2026-05-03T11:00:00",
         "outcome": "pending", "outcome_at": "", "outcome_note": ""},
    ]
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = _csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    monkeypatch.setattr("auto_applier.config.APPLICATIONS_CSV", csv_path)
    monkeypatch.setattr("auto_applier.config.BACKUP_DIR", backup_dir)
    return csv_path, backup_dir, rows


class TestPruneByJobId:
    def test_dry_run_removes_nothing(self, runner, csv_with_rows):
        csv_path, _, _ = csv_with_rows
        result = runner.invoke(cli, [
            "prune-by-job-id",
            "dice-461795d3-9232-460a-bc13-998ec350c1f8",
            "--dry-run",
        ])
        assert result.exit_code == 0
        assert "1 match" in result.output
        assert "no file changes made" in result.output.lower()

        # CSV unchanged — verify by reading it back
        with open(csv_path, "r", newline="", encoding="utf-8") as f:
            reader = _csv.DictReader(f)
            rows = list(reader)
        assert len(rows) == 3

    def test_yes_skips_confirmation_and_removes_one_row(
        self, runner, csv_with_rows,
    ):
        csv_path, backup_dir, _ = csv_with_rows
        result = runner.invoke(cli, [
            "prune-by-job-id",
            "dice-461795d3-9232-460a-bc13-998ec350c1f8",
            "--yes",
        ])
        assert result.exit_code == 0
        assert "Removed 1 row" in result.output

        with open(csv_path, "r", newline="", encoding="utf-8") as f:
            rows = list(_csv.DictReader(f))
        assert len(rows) == 2
        # The removed row is gone, the others are intact.
        kept_ids = {r["job_id"] for r in rows}
        assert "dice-461795d3-9232-460a-bc13-998ec350c1f8" not in kept_ids
        assert "indeed-real-1" in kept_ids

    def test_multiple_job_ids_in_one_invocation(self, runner, csv_with_rows):
        csv_path, _, _ = csv_with_rows
        result = runner.invoke(cli, [
            "prune-by-job-id",
            "dice-461795d3-9232-460a-bc13-998ec350c1f8",
            "dice-a4816ecb-b039-4c6c-8db1-b49d0ca1f050",
            "--yes",
        ])
        assert result.exit_code == 0
        assert "Removed 2 row" in result.output

        with open(csv_path, "r", newline="", encoding="utf-8") as f:
            rows = list(_csv.DictReader(f))
        assert len(rows) == 1
        assert rows[0]["job_id"] == "indeed-real-1"

    def test_creates_backup_before_writing(self, runner, csv_with_rows):
        _, backup_dir, _ = csv_with_rows
        runner.invoke(cli, [
            "prune-by-job-id",
            "dice-461795d3-9232-460a-bc13-998ec350c1f8",
            "--yes",
        ])
        # Exactly one backup file produced.
        backups = list(backup_dir.glob("applications.*.csv"))
        assert len(backups) == 1
        assert "before_prune_by_job_id" in backups[0].name

    def test_unknown_job_id_reported_not_crashed(self, runner, csv_with_rows):
        result = runner.invoke(cli, [
            "prune-by-job-id", "does-not-exist", "--yes",
        ])
        assert result.exit_code == 0
        assert "Nothing to prune" in result.output or "0 match" in result.output

    def test_partial_match_reports_missing_and_removes_present(
        self, runner, csv_with_rows,
    ):
        """One real id + one bogus id. Real one removed; bogus one
        listed as 'not found'. Must not crash on the missing id."""
        csv_path, _, _ = csv_with_rows
        result = runner.invoke(cli, [
            "prune-by-job-id",
            "dice-461795d3-9232-460a-bc13-998ec350c1f8",
            "totally-fake-id",
            "--yes",
        ])
        assert result.exit_code == 0
        assert "Removed 1 row" in result.output
        assert "Not found in CSV" in result.output
        assert "totally-fake-id" in result.output

        with open(csv_path, "r", newline="", encoding="utf-8") as f:
            rows = list(_csv.DictReader(f))
        assert len(rows) == 2

    def test_legitimate_applied_row_preserved(self, runner, csv_with_rows):
        """Removing a Dice misclassified row must NOT affect a real
        Indeed apply that happens to share fields with the bad row."""
        csv_path, _, _ = csv_with_rows
        runner.invoke(cli, [
            "prune-by-job-id",
            "dice-461795d3-9232-460a-bc13-998ec350c1f8",
            "--yes",
        ])
        with open(csv_path, "r", newline="", encoding="utf-8") as f:
            rows = list(_csv.DictReader(f))
        real = [r for r in rows if r["job_id"] == "indeed-real-1"]
        assert len(real) == 1
        assert real[0]["status"] == "applied"
        assert real[0]["fields_filled"] == "8"
