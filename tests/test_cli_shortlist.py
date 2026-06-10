"""Contract tests for ``av3 shortlist`` — the manual-mode apply-shortlist view."""

from __future__ import annotations

import json

from click.testing import CliRunner

from auto_applier.cli.main import cli
from auto_applier.db import init_app_db
from auto_applier.db.repositories import JobRepo, ScoreRepo
from auto_applier.domain.models import Job, JobScore, utcnow_iso
from auto_applier.domain.state import JobState


def _seed(settings, *, state, title, company, sid, location="Remote", score=9.0):
    conn = init_app_db(settings.app_db_path)
    try:
        JobRepo(conn).add(Job(
            source="greenhouse", source_job_id=sid, title=title, company=company,
            state=state, location=location, url=f"https://jobs.example/{sid}",
        ))
        jid = JobRepo(conn).get_by_source("greenhouse", sid).id
        ScoreRepo(conn).upsert(JobScore(
            job_id=jid, total=score, dimensions={}, model="test", scored_at=utcnow_iso(),
        ))
    finally:
        conn.close()
    return jid


def test_shortlist_excludes_applied_and_unscored_states(settings):
    # The #1 regression: list_ranked isn't state-filtered. An APPLIED-but-scored job and a
    # SCORED (not DECIDED) job must NOT appear; only DECIDED jobs do.
    decided = _seed(settings, state=JobState.DECIDED, title="Data Engineer", company="Good", sid="d1")
    _seed(settings, state=JobState.APPLIED, title="Data Engineer", company="Done", sid="a1")
    _seed(settings, state=JobState.SCORED, title="Data Engineer", company="Early", sid="sc1")

    res = CliRunner().invoke(cli, ["shortlist", "--family", "data_platform", "--name", "t"])
    assert res.exit_code == 0, res.output

    data = json.loads((settings.shortlist_dir / "t.json").read_text(encoding="utf-8"))
    ids = {row["job_id"] for row in data}
    assert ids == {decided}


def test_shortlist_family_filter(settings):
    dp = _seed(settings, state=JobState.DECIDED, title="Senior Data Platform Engineer",
               company="DP", sid="dp")
    _seed(settings, state=JobState.DECIDED, title="Solutions Engineer", company="SE", sid="se")

    res = CliRunner().invoke(cli, ["shortlist", "--family", "data_platform", "--name", "fam"])
    assert res.exit_code == 0, res.output
    data = json.loads((settings.shortlist_dir / "fam.json").read_text(encoding="utf-8"))
    assert {r["job_id"] for r in data} == {dp}


def test_shortlist_location_filter(settings):
    remote = _seed(settings, state=JobState.DECIDED, title="Data Engineer", company="R",
                   sid="r", location="Remote - US")
    onsite = _seed(settings, state=JobState.DECIDED, title="Data Engineer", company="O",
                   sid="o", location="Bengaluru, India")

    res = CliRunner().invoke(cli, ["shortlist", "--location", "remote", "--name", "loc"])
    assert res.exit_code == 0, res.output
    data = json.loads((settings.shortlist_dir / "loc.json").read_text(encoding="utf-8"))
    ids = {r["job_id"] for r in data}
    assert remote in ids          # remote-US passes the `remote` filter
    assert onsite not in ids      # on-site India is filtered out


def test_shortlist_writes_both_files_with_documented_keys(settings):
    _seed(settings, state=JobState.DECIDED, title="Data Engineer", company="X", sid="x")
    res = CliRunner().invoke(cli, ["shortlist", "--name", "both"])
    assert res.exit_code == 0, res.output
    assert (settings.shortlist_dir / "both.md").exists()
    data = json.loads((settings.shortlist_dir / "both.json").read_text(encoding="utf-8"))
    assert data and set(data[0]) == {
        "rank", "job_id", "score", "title", "company", "location", "url", "fit", "family"
    }
    assert [r["rank"] for r in data] == list(range(1, len(data) + 1))


def test_shortlist_empty_is_friendly(settings):
    res = CliRunner().invoke(cli, ["shortlist", "--family", "data_platform", "--name", "none"])
    assert res.exit_code == 0
    assert "No DECIDED jobs match" in res.output
    assert not (settings.shortlist_dir / "none.json").exists()
