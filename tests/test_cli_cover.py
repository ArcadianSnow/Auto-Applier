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


# --------------------------------------------------------------- av3 cover --generate (BUILD 5)

# A bank whose corpus supports the fake letter's tech claims (SQL Server, Python) → guard PASS.
_MASTER = {
    "contact": {"name": "Joseph Lira", "email": "jl@example.com", "location": "Dallas, TX"},
    "skills": ["sql", "sql server", "python", "etl"],
    "work_history": [{
        "company": "Acme", "title": "Database Administrator",
        "start": "2019", "end": "2024",
        "bullets": ["Refactored the billing database", "Owned SQL Server backups"],
    }],
}


def _write_bank(settings) -> None:
    import json
    d = settings.data_dir / "profile"
    d.mkdir(parents=True, exist_ok=True)
    (d / "master.json").write_text(json.dumps(_MASTER), encoding="utf-8")


def _patch_llm(monkeypatch) -> None:
    """Make the CLI's build_default() return the deterministic fake (no Ollama)."""
    from tests.test_cover_autogen import _CoverLLM
    monkeypatch.setattr("auto_applier.llm.complete.build_default", lambda settings: _CoverLLM())


def _seed_scored(settings, *, sid, total, title="Data Platform Engineer", company="BetaCo",
                 description="We need a SQL Server DBA strong in Python and ETL.") -> str:
    from auto_applier.db.repositories import ScoreRepo
    from auto_applier.domain.models import JobScore
    conn = init_app_db(settings.app_db_path)
    try:
        repo = JobRepo(conn)
        job = Job(source="greenhouse", source_job_id=sid, title=title,
                  company=company, description=description)
        repo.add(job)
        repo.set_state(job.id, JobState.DESCRIBED)
        repo.set_state(job.id, JobState.SCORED)
        repo.set_state(job.id, JobState.DECIDED)
        ScoreRepo(conn).upsert(JobScore(job_id=job.id, total=total))
        return job.id
    finally:
        conn.close()


def test_cover_generate_one_writes_docx(settings, monkeypatch):
    _write_bank(settings)
    _patch_llm(monkeypatch)
    jid = _seed(settings, sid="g1", title="Data Platform Engineer", company="BetaCo")
    # _seed makes a description-less DECIDED job; give it a JD so generation runs.
    conn = init_app_db(settings.app_db_path)
    try:
        JobRepo(conn).update_fields(jid, description="SQL Server + Python + ETL role.")
    finally:
        conn.close()

    res = CliRunner().invoke(cli, ["cover", "--generate", jid])
    assert res.exit_code == 0, res.output
    assert "generated" in res.output
    got = existing_job_cover(settings, jid)
    assert got is not None and got.name == "Joseph Lira Cover Letter.docx"


def test_cover_generate_skips_existing_then_force(settings, monkeypatch):
    _write_bank(settings)
    _patch_llm(monkeypatch)
    jid = _seed(settings, sid="g2")
    conn = init_app_db(settings.app_db_path)
    try:
        JobRepo(conn).update_fields(jid, description="SQL Server + Python role.")
    finally:
        conn.close()

    # first generate
    assert CliRunner().invoke(cli, ["cover", "--generate", jid]).exit_code == 0
    # second without --force: skip (no overwrite)
    res2 = CliRunner().invoke(cli, ["cover", "--generate", jid])
    assert res2.exit_code == 0 and "skipped_existing" in res2.output
    # with --force: regenerates, still exactly one cover
    res3 = CliRunner().invoke(cli, ["cover", "--generate", jid, "--force"])
    assert res3.exit_code == 0 and "generated" in res3.output
    folder = (settings.uploads_dir / jid)
    assert sorted(p.name for p in folder.glob("*Cover Letter.*")) == ["Joseph Lira Cover Letter.docx"]


def test_cover_generate_all_backfills_strong_jobs(settings, monkeypatch):
    _write_bank(settings)
    _patch_llm(monkeypatch)
    strong = _seed_scored(settings, sid="s", total=9.0)
    mid = _seed_scored(settings, sid="m", total=8.4)
    _seed_scored(settings, sid="w", total=6.0)  # below the 8.0 floor

    res = CliRunner().invoke(cli, ["cover", "--generate-all"])
    assert res.exit_code == 0, res.output
    assert "generated=2" in res.output
    assert existing_job_cover(settings, strong) is not None
    assert existing_job_cover(settings, mid) is not None


def test_cover_no_args_errors(settings):
    res = CliRunner().invoke(cli, ["cover"])
    assert res.exit_code == 2
    assert "generate-all" in res.output


def test_cover_generate_needs_job_id(settings):
    res = CliRunner().invoke(cli, ["cover", "--generate"])
    assert res.exit_code == 2
    assert "JOB_ID" in res.output
