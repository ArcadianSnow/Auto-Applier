"""Contract tests for ``av3 cover`` — per-job cover-letter assignment (BUILD 1.1).

Assigns a hand-authored letter to a job under the generic upload basename ``Cover Letter<ext>``
(anti-detection); shows the current assignment; surfaces missing job / missing file.
"""

from __future__ import annotations

from click.testing import CliRunner

from auto_applier.cli.main import cli
from auto_applier.db import init_app_db
from auto_applier.db.repositories import JobRepo
from auto_applier.domain.models import Job
from auto_applier.domain.state import JobState
from auto_applier.resume.generate import existing_job_cover


def _seed(settings, *, sid="s", company="Tailscale", title="Solutions Engineer"):
    conn = init_app_db(settings.app_db_path)
    try:
        JobRepo(conn).add(Job(source="greenhouse", source_job_id=sid, title=title,
                              company=company, state=JobState.DECIDED))
        return JobRepo(conn).get_by_source("greenhouse", sid).id
    finally:
        conn.close()


def test_cover_assigns_under_generic_name(settings, tmp_path):
    jid = _seed(settings)
    src = tmp_path / "CoverLetter_Tailscale_SE_Commercial.docx"
    src.write_text("Dear Tailscale", encoding="utf-8")

    res = CliRunner().invoke(cli, ["cover", jid, str(src)])
    assert res.exit_code == 0, res.output
    assert "assigned" in res.output
    assert "'Cover Letter.docx'" in res.output  # uploads under the generic basename

    got = existing_job_cover(settings, jid)
    assert got is not None and got.name == "Cover Letter.docx"
    assert got.read_text(encoding="utf-8") == "Dear Tailscale"


def test_cover_show_when_no_source(settings, tmp_path):
    jid = _seed(settings, sid="show")
    # nothing assigned yet
    res = CliRunner().invoke(cli, ["cover", jid])
    assert res.exit_code == 0, res.output
    assert "no cover letter assigned" in res.output

    # assign, then show
    src = tmp_path / "l.docx"
    src.write_text("x", encoding="utf-8")
    CliRunner().invoke(cli, ["cover", jid, str(src)])
    res2 = CliRunner().invoke(cli, ["cover", jid])
    assert res2.exit_code == 0, res2.output
    assert "Cover Letter.docx" in res2.output


def test_cover_unknown_job_exit_2(settings):
    res = CliRunner().invoke(cli, ["cover", "no-such-id", "whatever.docx"])
    assert res.exit_code == 2
    assert "not found" in res.output


def test_cover_missing_source_exit_2(settings):
    jid = _seed(settings, sid="ms")
    res = CliRunner().invoke(cli, ["cover", jid, "does-not-exist.docx"])
    assert res.exit_code == 2
    assert "not found" in res.output
