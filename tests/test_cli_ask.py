"""CLI contract tests for `av3 ask` (spec §8f application copilot)."""

from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

from auto_applier.cli.main import cli
from auto_applier.copilot import CopilotAnswer
from auto_applier.db import init_app_db
from auto_applier.db.repositories import AnswerRepo, JobRepo
from auto_applier.domain.models import Job
from auto_applier.domain.state import JobState


def _seed_bank(data_dir: Path) -> None:
    (data_dir / "profile").mkdir(parents=True, exist_ok=True)
    (data_dir / "profile" / "master.json").write_text(
        json.dumps({
            "contact": {"name": "Ada", "email": "a@b.c"},
            "work_history": [{"company": "Acme", "title": "Data Engineer",
                              "start": "2020", "end": "Present",
                              "bullets": ["Built watermark-based incremental sync"]}],
            "education": [], "skills": ["Python", "n8n"], "certifications": [],
            "allowed_metrics": [], "work_authorization": "US citizen",
            "requires_sponsorship": False, "eeo": {},
        }),
        encoding="utf-8",
    )


def _seed_job(data_dir: Path) -> Job:
    conn = init_app_db(data_dir / "app.db")
    repo = JobRepo(conn)
    job = Job(source="lever", source_job_id="j1", title="Data Engineer",
              company="Monzo", url="https://x/j1", description="CDC pipelines.",
              compensation="$100,000 - $140,000")
    repo.add(job)
    repo.set_state(job.id, JobState.DESCRIBED)
    conn.commit()
    conn.close()
    return job


def _run(monkeypatch, data_dir: Path, *args: str):
    monkeypatch.setenv("AV3_DATA_DIR", str(data_dir))
    return CliRunner().invoke(cli, list(args))


def _stub_answer(monkeypatch, answer: CopilotAnswer):
    captured = {}

    async def fake_answer(self, question, bank, *, job=None, salary_ask=""):
        captured["question"] = question
        captured["job"] = job
        captured["salary_ask"] = salary_ask
        answer.question = question
        return answer

    monkeypatch.setattr("auto_applier.copilot.Copilot.answer", fake_answer)
    return captured


def _ok_answer(**over) -> CopilotAnswer:
    base = dict(
        question="", verdict="no", short_answer="No",
        long_answer="Not Debezium specifically. My CDC work is watermark-based sync.",
        reasoning="The bank shows watermark sync, not log-based CDC.",
        framing="Anchor on the n8n suite.", gaps=["Debezium"],
    )
    base.update(over)
    return CopilotAnswer(**base)


def test_ask_prints_structured_answer(tmp_path, monkeypatch):
    data_dir = tmp_path / "v3"
    data_dir.mkdir()
    _seed_bank(data_dir)
    _stub_answer(monkeypatch, _ok_answer())

    result = _run(monkeypatch, data_dir, "ask", "Have you led a Debezium implementation?")
    assert result.exit_code == 0, result.output
    assert "verdict: NO" in result.output
    assert "Not Debezium specifically" in result.output
    assert "gaps to learn: Debezium" in result.output


def test_ask_missing_fact_bank_exits_2(tmp_path, monkeypatch):
    data_dir = tmp_path / "v3"
    data_dir.mkdir()
    result = _run(monkeypatch, data_dir, "ask", "Anything?")
    assert result.exit_code == 2
    assert "fact bank" in result.output


def test_ask_unknown_job_exits_2(tmp_path, monkeypatch):
    data_dir = tmp_path / "v3"
    data_dir.mkdir()
    _seed_bank(data_dir)
    init_app_db(data_dir / "app.db").close()
    result = _run(monkeypatch, data_dir, "ask", "Q?", "--job", "nope")
    assert result.exit_code == 2
    assert "no job" in result.output


def test_ask_passes_job_context_and_salary_ask(tmp_path, monkeypatch):
    data_dir = tmp_path / "v3"
    data_dir.mkdir()
    _seed_bank(data_dir)
    job = _seed_job(data_dir)
    captured = _stub_answer(monkeypatch, _ok_answer())

    result = _run(monkeypatch, data_dir, "ask", "Salary thoughts?", "--job", job.id)
    assert result.exit_code == 0, result.output
    assert captured["job"].company == "Monzo"
    # Posted $100k-$140k → the §8d ask anchors inside the band.
    assert captured["salary_ask"].startswith("$")


def test_ask_review_exits_1(tmp_path, monkeypatch):
    data_dir = tmp_path / "v3"
    data_dir.mkdir()
    _seed_bank(data_dir)
    _stub_answer(monkeypatch, CopilotAnswer(
        question="", verdict="review", needs_review=True,
        audit_notes=["failed closed"]))

    result = _run(monkeypatch, data_dir, "ask", "Overclaim bait?")
    assert result.exit_code == 1
    assert "REVIEW" in result.output
    assert "failed closed" in result.output


def test_ask_json_emits_full_structure(tmp_path, monkeypatch):
    data_dir = tmp_path / "v3"
    data_dir.mkdir()
    _seed_bank(data_dir)
    _stub_answer(monkeypatch, _ok_answer())

    result = _run(monkeypatch, data_dir, "ask", "Q?", "--json")
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["verdict"] == "no"
    assert payload["gaps"] == ["Debezium"]


def test_ask_save_stores_answer_in_bank(tmp_path, monkeypatch):
    data_dir = tmp_path / "v3"
    data_dir.mkdir()
    _seed_bank(data_dir)
    init_app_db(data_dir / "app.db").close()
    _stub_answer(monkeypatch, _ok_answer())

    class _NoEmbed:
        def __init__(self, *a, **k): ...
        async def embed(self, text):
            raise RuntimeError("no ollama in tests")

    monkeypatch.setattr("auto_applier.llm.embed.OllamaEmbeddings", _NoEmbed)

    result = _run(monkeypatch, data_dir, "ask", "Debezium experience?", "--save")
    assert result.exit_code == 0, result.output
    assert "saved to the answer bank" in result.output

    conn = init_app_db(data_dir / "app.db")
    try:
        stored = AnswerRepo(conn).get("Debezium experience?")
    finally:
        conn.close()
    assert stored is not None
    assert "watermark-based sync" in stored.answer


def test_ask_save_refused_for_review_answers(tmp_path, monkeypatch):
    data_dir = tmp_path / "v3"
    data_dir.mkdir()
    _seed_bank(data_dir)
    init_app_db(data_dir / "app.db").close()
    _stub_answer(monkeypatch, CopilotAnswer(
        question="", verdict="review", needs_review=True))

    result = _run(monkeypatch, data_dir, "ask", "Bait?", "--save")
    assert result.exit_code == 1
    assert "not saving" in result.output

    conn = init_app_db(data_dir / "app.db")
    try:
        assert AnswerRepo(conn).get("Bait?") is None  # nothing banked
    finally:
        conn.close()
