"""CLI contract tests for `av3 outcome` + `av3 analytics` (spec §8e, Phase 6 4/M)."""

from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

from av3.cli.main import cli
from av3.db import init_app_db
from av3.db.repositories import JobRepo, OutcomeRepo, ScoreRepo
from av3.domain.models import Job, JobScore, Outcome
from av3.domain.state import JobState, OutcomeKind


def _applied_job(conn, *, source_job_id="j1", source="lever", score=8.0) -> Job:
    repo = JobRepo(conn)
    job = Job(source=source, source_job_id=source_job_id, title="Data Engineer",
              company="Acme", url=f"https://x/{source_job_id}")
    repo.add(job)
    for nxt in (JobState.DESCRIBED, JobState.SCORED, JobState.DECIDED,
                JobState.QUEUED_APPLY, JobState.APPLYING, JobState.APPLIED):
        repo.set_state(job.id, nxt)
    ScoreRepo(conn).upsert(JobScore(job_id=job.id, total=score, dimensions={}))
    conn.commit()
    return repo.get(job.id)


def _run(monkeypatch, data_dir: Path, *args: str):
    monkeypatch.setenv("AV3_DATA_DIR", str(data_dir))
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    return CliRunner().invoke(cli, list(args))


def test_outcome_records_for_applied_job(tmp_path, monkeypatch):
    data_dir = tmp_path / "v3data"
    data_dir.mkdir()
    conn = init_app_db(data_dir / "app.db")
    job = _applied_job(conn)
    conn.close()

    result = _run(monkeypatch, data_dir, "outcome", job.id, "interview")
    assert result.exit_code == 0, result.output
    assert "recorded outcome=interview" in result.output

    conn = init_app_db(data_dir / "app.db")
    try:
        got = OutcomeRepo(conn).list_by_job(job.id)
    finally:
        conn.close()
    assert len(got) == 1 and got[0].kind.value == "interview"


def test_outcome_unknown_job_exits_2(tmp_path, monkeypatch):
    data_dir = tmp_path / "v3data"
    data_dir.mkdir()
    init_app_db(data_dir / "app.db").close()
    result = _run(monkeypatch, data_dir, "outcome", "nope", "response")
    assert result.exit_code == 2
    assert "no job" in result.output


def test_outcome_warns_for_non_applied_job(tmp_path, monkeypatch):
    data_dir = tmp_path / "v3data"
    data_dir.mkdir()
    conn = init_app_db(data_dir / "app.db")
    repo = JobRepo(conn)
    job = Job(source="lever", source_job_id="na", title="X", company="Y", url="u")
    repo.add(job)
    repo.set_state(job.id, JobState.DESCRIBED)
    conn.commit()
    conn.close()

    result = _run(monkeypatch, data_dir, "outcome", job.id, "response")
    assert result.exit_code == 0
    assert "warning" in result.output
    assert "recorded outcome=response" in result.output


def test_outcome_rejects_bad_kind(tmp_path, monkeypatch):
    data_dir = tmp_path / "v3data"
    data_dir.mkdir()
    init_app_db(data_dir / "app.db").close()
    result = _run(monkeypatch, data_dir, "outcome", "x", "banana")
    assert result.exit_code != 0  # click.Choice rejects


def test_analytics_empty(tmp_path, monkeypatch):
    data_dir = tmp_path / "v3data"
    data_dir.mkdir()
    init_app_db(data_dir / "app.db").close()
    result = _run(monkeypatch, data_dir, "analytics")
    assert result.exit_code == 0
    assert "nothing to analyze" in result.output


def test_analytics_reports_conversion(tmp_path, monkeypatch):
    data_dir = tmp_path / "v3data"
    data_dir.mkdir()
    conn = init_app_db(data_dir / "app.db")
    j1 = _applied_job(conn, source_job_id="a", score=9.0)
    _applied_job(conn, source_job_id="b", score=8.0)  # silent
    OutcomeRepo(conn).add(Outcome(job_id=j1.id, kind=OutcomeKind.OFFER))
    conn.commit()
    conn.close()

    result = _run(monkeypatch, data_dir, "analytics")
    assert result.exit_code == 0
    assert "Applied=2" in result.output
    assert "converted=1" in result.output


def test_analytics_json(tmp_path, monkeypatch):
    data_dir = tmp_path / "v3data"
    data_dir.mkdir()
    conn = init_app_db(data_dir / "app.db")
    _applied_job(conn, source_job_id="a", score=9.0)
    conn.commit()
    conn.close()

    result = _run(monkeypatch, data_dir, "analytics", "--json")
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["total_applied"] == 1
    assert "by_source" in data and "nudges" in data
