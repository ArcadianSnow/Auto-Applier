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
from auto_applier.resume.generate import existing_job_cover, existing_job_resume


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


# --------------------------------------------------------------- av3 resume

def test_resume_assigns_under_generic_name(settings, tmp_path):
    jid = _seed(settings, sid="r")
    src = tmp_path / "Joseph_Lira_Resume_Solutions_Engineer.pdf"
    src.write_text("RESUME", encoding="utf-8")

    res = CliRunner().invoke(cli, ["resume", jid, str(src)])
    assert res.exit_code == 0, res.output
    assert "assigned" in res.output
    assert "'Resume.pdf'" in res.output  # generic upload basename

    got = existing_job_resume(settings, jid)
    assert got is not None and got.name == "Resume.pdf"
    assert got.read_text(encoding="utf-8") == "RESUME"


def test_resume_show_and_unknown_job(settings, tmp_path):
    jid = _seed(settings, sid="rs")
    res = CliRunner().invoke(cli, ["resume", jid])
    assert res.exit_code == 0 and "no résumé assigned" in res.output

    bad = CliRunner().invoke(cli, ["resume", "no-such-id"])
    assert bad.exit_code == 2 and "not found" in bad.output


def test_cover_uses_applicant_name_from_fact_bank(settings, tmp_path):
    """When the fact bank has a name, the upload basename is name-prefixed
    (Joseph Lira Cover Letter.docx) — pulled from profile/master.json by the CLI."""
    import json
    profile_dir = settings.data_dir / "profile"
    profile_dir.mkdir(parents=True, exist_ok=True)
    (profile_dir / "master.json").write_text(
        json.dumps({"contact": {"name": "Joseph Lira"}}), encoding="utf-8")

    jid = _seed(settings, sid="named")
    src = tmp_path / "CoverLetter_Tailscale_SE_Commercial.docx"
    src.write_text("C", encoding="utf-8")
    res = CliRunner().invoke(cli, ["cover", jid, str(src)])
    assert res.exit_code == 0, res.output
    assert "'Joseph Lira Cover Letter.docx'" in res.output
    assert existing_job_cover(settings, jid).name == "Joseph Lira Cover Letter.docx"
