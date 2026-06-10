"""Contract tests for ``av3 applied`` (+ ``av3 pass``) — manual-mode mark-down."""

from __future__ import annotations

import json

from click.testing import CliRunner

from auto_applier.cli.main import cli
from auto_applier.db import init_app_db
from auto_applier.db.repositories import ApplicationRepo, JobRepo
from auto_applier.domain.models import Job
from auto_applier.domain.state import ApplyMode, JobState


def _seed(settings, *, state=JobState.DECIDED, sid="s", title="Data Engineer", company="Acme"):
    conn = init_app_db(settings.app_db_path)
    try:
        JobRepo(conn).add(Job(source="greenhouse", source_job_id=sid, title=title,
                              company=company, state=state))
        return JobRepo(conn).get_by_source("greenhouse", sid).id
    finally:
        conn.close()


def _state(settings, jid):
    conn = init_app_db(settings.app_db_path)
    try:
        return JobRepo(conn).get(jid).state
    finally:
        conn.close()


def test_applied_positional_ids(settings):
    a = _seed(settings, sid="a")
    b = _seed(settings, sid="b")
    res = CliRunner().invoke(cli, ["applied", a, b])
    assert res.exit_code == 0, res.output
    assert "applied=2 already=0 errors=0" in res.output
    assert _state(settings, a) is JobState.APPLIED
    assert _state(settings, b) is JobState.APPLIED


def test_applied_mixed_batch_exit_code(settings):
    good = _seed(settings, sid="good")
    done = _seed(settings, sid="done", state=JobState.APPLIED)
    res = CliRunner().invoke(cli, ["applied", good, done, "no-such-id"])
    assert "applied=1 already=1 errors=1" in res.output
    assert res.exit_code == 1  # errors → non-zero


def test_applied_resume_path_recorded(settings):
    jid = _seed(settings, sid="r")
    res = CliRunner().invoke(cli, ["applied", jid, "--resume", r"C:\x\Data_Platform.docx"])
    assert res.exit_code == 0, res.output
    conn = init_app_db(settings.app_db_path)
    try:
        app = ApplicationRepo(conn).list_by_job(jid)[0]
        assert app.mode is ApplyMode.MANUAL
        assert app.generated_resume_path.endswith("Data_Platform.docx")
    finally:
        conn.close()


def test_applied_from_shortlist_all(settings):
    a = _seed(settings, sid="a")
    b = _seed(settings, sid="b")
    settings.shortlist_dir.mkdir(parents=True, exist_ok=True)
    (settings.shortlist_dir / "batch.json").write_text(
        json.dumps([{"job_id": a}, {"job_id": b}]), encoding="utf-8")
    res = CliRunner().invoke(cli, ["applied", "--shortlist", "batch", "--all"])
    assert res.exit_code == 0, res.output
    assert "applied=2" in res.output
    assert _state(settings, a) is JobState.APPLIED and _state(settings, b) is JobState.APPLIED


def test_applied_missing_shortlist_exit_2(settings):
    res = CliRunner().invoke(cli, ["applied", "--shortlist", "nope", "--all"])
    assert res.exit_code == 2
    assert "no saved shortlist" in res.output


def test_applied_no_ids_exit_2(settings):
    res = CliRunner().invoke(cli, ["applied"])
    assert res.exit_code == 2


def test_pass_marks_skipped(settings):
    jid = _seed(settings, sid="p")
    res = CliRunner().invoke(cli, ["pass", jid])
    assert res.exit_code == 0, res.output
    assert "passed=1 errors=0" in res.output
    assert _state(settings, jid) is JobState.SKIPPED


def test_pass_rejects_non_decided(settings):
    jid = _seed(settings, sid="q", state=JobState.APPLIED)
    res = CliRunner().invoke(cli, ["pass", jid])
    assert res.exit_code == 1  # APPLIED → SKIPPED is illegal, surfaces as an error
    assert _state(settings, jid) is JobState.APPLIED
