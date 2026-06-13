"""Contract tests for ``av3 queue`` — the manual-résumé route DECIDED/REVIEW → QUEUED_APPLY.

This is the path that lets the apply worker upload a hand-crafted résumé instead of a
generated one (skip optimize). See research/automated-apply-go-live.md, blocker B.
"""

from __future__ import annotations

import json

from click.testing import CliRunner

from auto_applier.cli.main import cli
from auto_applier.db import init_app_db
from auto_applier.db.repositories import JobRepo
from auto_applier.domain.models import Job
from auto_applier.domain.state import JobState


def _seed(settings, *, state=JobState.DECIDED, sid="s", title="Solutions Engineer", company="Acme"):
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


def test_queue_decided_jobs(settings):
    a = _seed(settings, sid="a")
    b = _seed(settings, sid="b")
    res = CliRunner().invoke(cli, ["queue", a, b])
    assert res.exit_code == 0, res.output
    assert "queued=2 already=0 errors=0" in res.output
    assert _state(settings, a) is JobState.QUEUED_APPLY
    assert _state(settings, b) is JobState.QUEUED_APPLY


def test_queue_from_review(settings):
    jid = _seed(settings, sid="r", state=JobState.REVIEW)
    res = CliRunner().invoke(cli, ["queue", jid])
    assert res.exit_code == 0, res.output
    assert _state(settings, jid) is JobState.QUEUED_APPLY


def test_queue_idempotent_already(settings):
    jid = _seed(settings, sid="q", state=JobState.QUEUED_APPLY)
    res = CliRunner().invoke(cli, ["queue", jid])
    assert res.exit_code == 0, res.output
    assert "queued=0 already=1 errors=0" in res.output


def test_queue_rejects_illegal_source_state(settings):
    # APPLIED is terminal — must not be silently queued (would corrupt dedup).
    applied = _seed(settings, sid="ap", state=JobState.APPLIED)
    scored = _seed(settings, sid="sc", state=JobState.SCORED)
    res = CliRunner().invoke(cli, ["queue", applied, scored])
    assert res.exit_code == 1
    assert "only DECIDED/REVIEW can be queued" in res.output
    assert _state(settings, applied) is JobState.APPLIED
    assert _state(settings, scored) is JobState.SCORED


def test_queue_mixed_batch_exit_code(settings):
    good = _seed(settings, sid="good")
    res = CliRunner().invoke(cli, ["queue", good, "no-such-id"])
    assert "queued=1 already=0 errors=1" in res.output
    assert res.exit_code == 1
    assert _state(settings, good) is JobState.QUEUED_APPLY


def test_queue_from_shortlist_all(settings):
    a = _seed(settings, sid="a")
    b = _seed(settings, sid="b")
    settings.shortlist_dir.mkdir(parents=True, exist_ok=True)
    (settings.shortlist_dir / "solutions2.json").write_text(
        json.dumps([{"job_id": a}, {"job_id": b}]), encoding="utf-8")
    res = CliRunner().invoke(cli, ["queue", "--shortlist", "solutions2", "--all"])
    assert res.exit_code == 0, res.output
    assert "queued=2" in res.output
    assert _state(settings, a) is JobState.QUEUED_APPLY
    assert _state(settings, b) is JobState.QUEUED_APPLY


def test_queue_no_ids_exit_2(settings):
    res = CliRunner().invoke(cli, ["queue"])
    assert res.exit_code == 2
