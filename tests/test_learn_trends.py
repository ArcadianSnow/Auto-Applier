"""What-to-learn skill-gap trends (spec §10/§7b, Phase 6 6/M).

Pure compute_skill_gap_trends + the `av3 learn` CLI + ScoreRepo.totals_by_job.
"""

from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

from auto_applier.analytics import HIGH_BAND_MIN, compute_skill_gap_trends
from auto_applier.cli.main import cli
from auto_applier.db import init_app_db
from auto_applier.db.repositories import JobRepo, ScoreRepo
from auto_applier.domain.models import Job, JobScore
from auto_applier.domain.state import JobState
from auto_applier.resume.factbank import FactBank


def _job(sjid, desc) -> Job:
    return Job(source="lever", source_job_id=sjid, title="Eng", company="Acme",
               url=f"https://x/{sjid}", description=desc)


# --------------------------------------------------------------- pure compute

def test_trends_rank_by_high_fit_then_demand():
    jobs = [
        _job("a", "AWS Docker"),         # high-fit
        _job("b", "AWS"),                # high-fit
        _job("c", "Docker Kubernetes"),  # low-fit
    ]
    scores = {jobs[0].id: 9.0, jobs[1].id: 8.0, jobs[2].id: 3.0}
    trends = compute_skill_gap_trends(jobs, scores, bank_skills=[])
    by = {t.skill: t for t in trends}
    assert by["AWS"].high_fit_count == 2
    assert by["Docker"].high_fit_count == 1
    assert by["Kubernetes"].high_fit_count == 0
    assert trends[0].skill == "AWS"


def test_trends_exclude_bank_skills():
    jobs = [_job("a", "Python AWS")]
    trends = compute_skill_gap_trends(jobs, {jobs[0].id: 9.0}, bank_skills=["Python"])
    assert {t.skill for t in trends} == {"AWS"}


def test_trends_avg_demanding_score():
    jobs = [_job("a", "AWS"), _job("b", "AWS")]
    trends = compute_skill_gap_trends(jobs, {jobs[0].id: 8.0, jobs[1].id: 6.0}, bank_skills=[])
    aws = next(t for t in trends if t.skill == "AWS")
    assert aws.avg_demanding_score == 7.0
    assert aws.demand_count == 2
    assert aws.high_fit_count == 1  # only the 8.0 job is >= HIGH_BAND_MIN (7.0)


def test_trends_unscored_jobs_have_none_avg():
    jobs = [_job("a", "Rust")]
    trends = compute_skill_gap_trends(jobs, {}, bank_skills=[])  # no scores
    rust = trends[0]
    assert rust.demand_count == 1
    assert rust.high_fit_count == 0
    assert rust.avg_demanding_score is None


def test_trends_top_caps():
    jobs = [_job(str(i), "AWS Docker Kubernetes Terraform Redis") for i in range(2)]
    trends = compute_skill_gap_trends(jobs, {}, bank_skills=[], top=2)
    assert len(trends) == 2


def test_trends_empty_jobs():
    assert compute_skill_gap_trends([], {}, bank_skills=[]) == []


def test_high_band_min_value():
    assert HIGH_BAND_MIN == 7.0


# --------------------------------------------------------------- ScoreRepo.totals_by_job

def test_totals_by_job(tmp_path):
    conn = init_app_db(tmp_path / "app.db")
    repo = JobRepo(conn)
    j = _job("s1", "Python")
    repo.add(j)
    ScoreRepo(conn).upsert(JobScore(job_id=j.id, total=7.5, dimensions={}))
    assert ScoreRepo(conn).totals_by_job() == {j.id: 7.5}
    conn.close()


# --------------------------------------------------------------- av3 learn CLI

def _seed(data_dir: Path, *, skills, jobs_scores):
    (data_dir / "profile").mkdir(parents=True, exist_ok=True)
    (data_dir / "profile" / "master.json").write_text(
        json.dumps({
            "contact": {"name": "Ada", "email": "a@b.c"}, "work_history": [],
            "education": [], "skills": skills, "certifications": [], "allowed_metrics": [],
            "work_authorization": "US citizen", "requires_sponsorship": False, "eeo": {},
        }),
        encoding="utf-8",
    )
    conn = init_app_db(data_dir / "app.db")
    repo, srepo = JobRepo(conn), ScoreRepo(conn)
    for i, (desc, score) in enumerate(jobs_scores):
        job = _job(f"j{i}", desc)
        repo.add(job)
        repo.set_state(job.id, JobState.DESCRIBED)
        if score is not None:
            srepo.upsert(JobScore(job_id=job.id, total=score, dimensions={}))
    conn.commit()
    conn.close()


def _run(monkeypatch, data_dir, *args):
    monkeypatch.setenv("AV3_DATA_DIR", str(data_dir))
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    return CliRunner().invoke(cli, ["learn", *args])


def test_learn_missing_bank_exits_2(tmp_path, monkeypatch):
    d = tmp_path / "v3"
    d.mkdir()
    r = _run(monkeypatch, d)
    assert r.exit_code == 2


def test_learn_lists_high_fit_skills(tmp_path, monkeypatch):
    d = tmp_path / "v3"
    d.mkdir()
    _seed(d, skills=["Python"], jobs_scores=[("Python AWS Docker", 9.0), ("AWS", 8.0)])
    r = _run(monkeypatch, d)
    assert r.exit_code == 0
    assert "AWS" in r.output
    assert "Docker" in r.output


def test_learn_empty(tmp_path, monkeypatch):
    d = tmp_path / "v3"
    d.mkdir()
    _seed(d, skills=["Python"], jobs_scores=[])
    r = _run(monkeypatch, d)
    assert r.exit_code == 0
    assert "No skill-gap trends" in r.output


def test_learn_json(tmp_path, monkeypatch):
    d = tmp_path / "v3"
    d.mkdir()
    _seed(d, skills=[], jobs_scores=[("AWS Docker", 9.0)])
    r = _run(monkeypatch, d, "--json")
    assert r.exit_code == 0
    data = json.loads(r.output)
    skills = {row["skill"] for row in data}
    assert "AWS" in skills and "Docker" in skills


def test_learn_min_demand_filter(tmp_path, monkeypatch):
    d = tmp_path / "v3"
    d.mkdir()
    _seed(d, skills=[], jobs_scores=[("AWS Docker", 9.0), ("AWS", 8.0)])
    r = _run(monkeypatch, d, "--min-demand", "2")
    assert "AWS" in r.output
    assert "Docker" not in r.output.split("scanned")[-1]
