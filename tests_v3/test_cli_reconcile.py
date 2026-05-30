"""CLI contract tests for `av3 reconcile` (spec §7b, Phase 6 5/M)."""

from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

from av3.cli.main import cli
from av3.db import init_app_db
from av3.db.repositories import JobRepo
from av3.domain.models import Job
from av3.domain.state import JobState
from av3.resume.factbank import FactBank


def _seed(data_dir: Path, *, skills, jobs):
    (data_dir / "profile").mkdir(parents=True, exist_ok=True)
    (data_dir / "profile" / "master.json").write_text(
        json.dumps({
            "contact": {"name": "Ada", "email": "a@b.c"},
            "work_history": [], "education": [], "skills": skills,
            "certifications": [], "allowed_metrics": [],
            "work_authorization": "US citizen", "requires_sponsorship": False, "eeo": {},
        }),
        encoding="utf-8",
    )
    conn = init_app_db(data_dir / "app.db")
    repo = JobRepo(conn)
    for i, desc in enumerate(jobs):
        job = Job(source="lever", source_job_id=f"j{i}", title="Eng", company="Acme",
                  url=f"https://x/j{i}", description=desc)
        repo.add(job)
        repo.set_state(job.id, JobState.DESCRIBED)
    conn.commit()
    conn.close()


def _run(monkeypatch, data_dir: Path, *args: str):
    monkeypatch.setenv("AV3_DATA_DIR", str(data_dir))
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    return CliRunner().invoke(cli, ["reconcile", *args])


def test_reconcile_missing_fact_bank_exits_2(tmp_path, monkeypatch):
    data_dir = tmp_path / "v3"
    data_dir.mkdir()
    result = _run(monkeypatch, data_dir)
    assert result.exit_code == 2
    assert "fact bank" in result.output


def test_reconcile_preview_empty_without_scan(tmp_path, monkeypatch):
    data_dir = tmp_path / "v3"
    data_dir.mkdir()
    _seed(data_dir, skills=["Python"], jobs=["Python AWS role"])
    result = _run(monkeypatch, data_dir)  # no --scan → no gaps recorded yet
    assert result.exit_code == 0
    assert "No skill-gap proposals" in result.output


def test_reconcile_scan_then_preview(tmp_path, monkeypatch):
    data_dir = tmp_path / "v3"
    data_dir.mkdir()
    _seed(data_dir, skills=["Python"], jobs=["Python AWS Docker", "AWS"])
    result = _run(monkeypatch, data_dir, "--scan")
    assert result.exit_code == 0
    assert "recorded" in result.output
    # AWS (2x) + Docker (1x) surface; Python is in-bank so excluded.
    assert "AWS" in result.output
    assert "Docker" in result.output


def test_reconcile_apply_inserts_into_bank(tmp_path, monkeypatch):
    data_dir = tmp_path / "v3"
    data_dir.mkdir()
    _seed(data_dir, skills=["Python"], jobs=["Python AWS Docker"])
    _run(monkeypatch, data_dir, "--scan")
    result = _run(monkeypatch, data_dir, "--apply", "AWS,Docker")
    assert result.exit_code == 0
    assert "applied 2 new skill" in result.output

    bank = FactBank.load(data_dir / "profile" / "master.json")
    assert "AWS" in bank.skills
    assert "Docker" in bank.skills
    assert "Python" in bank.skills  # additive — original kept


def test_reconcile_apply_then_not_reproposed(tmp_path, monkeypatch):
    data_dir = tmp_path / "v3"
    data_dir.mkdir()
    _seed(data_dir, skills=["Python"], jobs=["AWS"])
    _run(monkeypatch, data_dir, "--scan")
    _run(monkeypatch, data_dir, "--apply", "AWS")
    # After applying, a fresh preview shows nothing (gap reconciled + in bank).
    result = _run(monkeypatch, data_dir)
    assert "No skill-gap proposals" in result.output


def test_reconcile_min_count_filter(tmp_path, monkeypatch):
    data_dir = tmp_path / "v3"
    data_dir.mkdir()
    _seed(data_dir, skills=[], jobs=["AWS Docker", "AWS"])  # AWS 2x, Docker 1x
    _run(monkeypatch, data_dir, "--scan")
    result = _run(monkeypatch, data_dir, "--min-count", "2")
    assert "AWS" in result.output
    assert "Docker" not in result.output
