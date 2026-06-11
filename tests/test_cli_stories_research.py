"""CLI contract tests for `av3 stories` + `av3 research` (spec §11 extras, 9/M+10/M)."""

from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

from auto_applier.cli.main import cli
from auto_applier.db import init_app_db
from auto_applier.db.repositories import JobRepo
from auto_applier.domain.models import Job
from auto_applier.domain.state import JobState
from auto_applier.resume.story_bank import Story, load_bank, save_bank


def _seed_bank(data_dir: Path) -> None:
    (data_dir / "profile").mkdir(parents=True, exist_ok=True)
    (data_dir / "profile" / "master.json").write_text(
        json.dumps({
            "contact": {"name": "Ada", "email": "a@b.c"},
            "work_history": [{"company": "Acme", "title": "Data Engineer",
                              "start": "2020", "end": "Present", "bullets": ["Built X"]}],
            "education": [], "skills": ["Python"], "certifications": [],
            "allowed_metrics": [], "work_authorization": "US citizen",
            "requires_sponsorship": False, "eeo": {},
        }),
        encoding="utf-8",
    )


def _seed_job(data_dir: Path, *, description="A JD about data pipelines.") -> Job:
    conn = init_app_db(data_dir / "app.db")
    repo = JobRepo(conn)
    job = Job(source="lever", source_job_id="j1", title="Data Engineer",
              company="Acme", url="https://x/j1", description=description)
    repo.add(job)
    repo.set_state(job.id, JobState.DESCRIBED)
    conn.commit()
    conn.close()
    return job


def _run(monkeypatch, data_dir: Path, *args: str, stdin: str | None = None):
    monkeypatch.setenv("AV3_DATA_DIR", str(data_dir))
    return CliRunner().invoke(cli, list(args), input=stdin)


def _stub_generate(stories):
    async def fake_generate(self, bank, jd, *, company="", title="", job_id=""):
        return [
            Story(title=s, question_prompt="Q", situation="s", task="t",
                  action="a", result="r", reflection="x",
                  job_id=job_id, company=company, job_title=title)
            for s in stories
        ]
    return fake_generate


# ------------------------------------------------------------------ stories generate

def test_stories_generate_appends_to_bank(tmp_path, monkeypatch):
    data_dir = tmp_path / "v3"
    data_dir.mkdir()
    _seed_bank(data_dir)
    job = _seed_job(data_dir)
    monkeypatch.setattr("auto_applier.resume.story_bank.StoryGenerator.generate",
                        _stub_generate(["Story A", "Story B", "Story C"]))

    result = _run(monkeypatch, data_dir, "stories", "generate", job.id)
    assert result.exit_code == 0, result.output
    assert "added 3 stories" in result.output

    stories = load_bank(data_dir / "story_bank.json")
    assert [s.title for s in stories] == ["Story A", "Story B", "Story C"]
    assert stories[0].job_id == job.id and stories[0].company == "Acme"


def test_stories_generate_missing_fact_bank_exits_2(tmp_path, monkeypatch):
    data_dir = tmp_path / "v3"
    data_dir.mkdir()
    result = _run(monkeypatch, data_dir, "stories", "generate", "whatever")
    assert result.exit_code == 2
    assert "fact bank" in result.output


def test_stories_generate_unknown_job_exits_2(tmp_path, monkeypatch):
    data_dir = tmp_path / "v3"
    data_dir.mkdir()
    _seed_bank(data_dir)
    init_app_db(data_dir / "app.db").close()
    result = _run(monkeypatch, data_dir, "stories", "generate", "nope")
    assert result.exit_code == 2
    assert "no job" in result.output


def test_stories_generate_no_description_exits_2(tmp_path, monkeypatch):
    data_dir = tmp_path / "v3"
    data_dir.mkdir()
    _seed_bank(data_dir)
    job = _seed_job(data_dir, description="")
    result = _run(monkeypatch, data_dir, "stories", "generate", job.id)
    assert result.exit_code == 2
    assert "no stored description" in result.output


def test_stories_generate_llm_empty_exits_1(tmp_path, monkeypatch):
    data_dir = tmp_path / "v3"
    data_dir.mkdir()
    _seed_bank(data_dir)
    job = _seed_job(data_dir)
    monkeypatch.setattr("auto_applier.resume.story_bank.StoryGenerator.generate",
                        _stub_generate([]))
    result = _run(monkeypatch, data_dir, "stories", "generate", job.id)
    assert result.exit_code == 1
    assert "no stories generated" in result.output
    assert not (data_dir / "story_bank.json").exists()  # nothing persisted


# ------------------------------------------------------------------ stories list/export

def test_stories_list_empty_hint(tmp_path, monkeypatch):
    data_dir = tmp_path / "v3"
    data_dir.mkdir()
    result = _run(monkeypatch, data_dir, "stories", "list")
    assert result.exit_code == 0
    assert "empty" in result.output


def test_stories_list_shows_titles_and_provenance(tmp_path, monkeypatch):
    data_dir = tmp_path / "v3"
    data_dir.mkdir()
    save_bank(data_dir / "story_bank.json", [
        Story(title="T1", question_prompt="", situation="s", task="t", action="a",
              result="r", reflection="x", company="Acme", job_title="DE"),
    ])
    result = _run(monkeypatch, data_dir, "stories", "list")
    assert result.exit_code == 0
    assert "T1" in result.output and "DE @ Acme" in result.output


def test_stories_export_writes_markdown(tmp_path, monkeypatch):
    data_dir = tmp_path / "v3"
    data_dir.mkdir()
    save_bank(data_dir / "story_bank.json", [
        Story(title="T1", question_prompt="", situation="s", task="t", action="a",
              result="r", reflection="x"),
    ])
    result = _run(monkeypatch, data_dir, "stories", "export")
    assert result.exit_code == 0, result.output
    md = (data_dir / "story_bank.md").read_text(encoding="utf-8")
    assert "## 1. T1" in md


# ------------------------------------------------------------------ research

def _stub_research(payload: dict | None):
    async def fake_research(self, company, source_material):
        if payload is None:
            return None
        from auto_applier.research import CompanyBriefing
        return CompanyBriefing(company=company, **payload)
    return fake_research


def test_research_saves_and_prints_briefing(tmp_path, monkeypatch):
    data_dir = tmp_path / "v3"
    data_dir.mkdir()
    src = tmp_path / "notes.txt"
    src.write_text("They build data tools on Snowflake.", encoding="utf-8")
    monkeypatch.setattr("auto_applier.research.CompanyResearcher.research",
                        _stub_research({"what_they_do": "Builds data tools.",
                                        "tech_stack_signals": ["Snowflake"]}))

    result = _run(monkeypatch, data_dir, "research", "Acme Data",
                  "--source-file", str(src))
    assert result.exit_code == 0, result.output
    assert "wrote briefing" in result.output
    assert (data_dir / "research" / "acme_data.md").exists()
    assert (data_dir / "research" / "acme_data.json").exists()


def test_research_show_prints_saved_briefing(tmp_path, monkeypatch):
    from auto_applier.research import CompanyBriefing, save_briefing

    data_dir = tmp_path / "v3"
    data_dir.mkdir()
    save_briefing(data_dir / "research",
                  CompanyBriefing(company="Acme", what_they_do="Data tools."))
    result = _run(monkeypatch, data_dir, "research", "Acme", "--show")
    assert result.exit_code == 0
    assert "Data tools." in result.output


def test_research_show_missing_exits_2(tmp_path, monkeypatch):
    data_dir = tmp_path / "v3"
    data_dir.mkdir()
    result = _run(monkeypatch, data_dir, "research", "Ghost Co", "--show")
    assert result.exit_code == 2
    assert "no saved briefing" in result.output


def test_research_missing_source_file_exits_2(tmp_path, monkeypatch):
    data_dir = tmp_path / "v3"
    data_dir.mkdir()
    result = _run(monkeypatch, data_dir, "research", "Acme",
                  "--source-file", str(tmp_path / "nope.txt"))
    assert result.exit_code == 2
    assert "source file not found" in result.output


def test_research_empty_stdin_exits_2(tmp_path, monkeypatch):
    data_dir = tmp_path / "v3"
    data_dir.mkdir()
    result = _run(monkeypatch, data_dir, "research", "Acme", stdin="   \n")
    assert result.exit_code == 2
    assert "no source material" in result.output


def test_research_llm_failure_exits_1(tmp_path, monkeypatch):
    data_dir = tmp_path / "v3"
    data_dir.mkdir()
    src = tmp_path / "notes.txt"
    src.write_text("some text", encoding="utf-8")
    monkeypatch.setattr("auto_applier.research.CompanyResearcher.research",
                        _stub_research(None))
    result = _run(monkeypatch, data_dir, "research", "Acme",
                  "--source-file", str(src))
    assert result.exit_code == 1
    assert "no briefing produced" in result.output
