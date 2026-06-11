"""Web surface tests for the application copilot (spec §8f) — /api/copilot/ask + /copilot."""

from __future__ import annotations

import sqlite3

import pytest
from fastapi.testclient import TestClient

from auto_applier.config import Settings
from auto_applier.copilot import CopilotAnswer
from auto_applier.db.repositories import JobRepo
from auto_applier.domain.models import Job, utcnow_iso
from auto_applier.domain.state import JobState
from auto_applier.resume.factbank import FactBank
from auto_applier.web import WebState, create_app
from auto_applier.web.onboarding import save_fact_bank


@pytest.fixture
def web_state(settings: Settings, conn: sqlite3.Connection) -> WebState:
    save_fact_bank(settings.data_dir, FactBank(skills=["Python"]))
    return WebState(
        settings=settings,
        app_db_path=settings.app_db_path,
        events_db_path=settings.events_db_path,
    )


def _make_client(web_state: WebState) -> TestClient:
    app = create_app(state=web_state, service=None)
    return TestClient(app)


def _stub_answer(monkeypatch, answer: CopilotAnswer):
    captured = {}

    async def fake_answer(self, question, bank, *, job=None, salary_ask=""):
        captured["question"] = question
        captured["job"] = job
        answer.question = question
        return answer

    monkeypatch.setattr("auto_applier.copilot.Copilot.answer", fake_answer)
    return captured


def test_ask_returns_structured_answer(web_state, monkeypatch):
    _stub_answer(monkeypatch, CopilotAnswer(
        question="", verdict="no", short_answer="No",
        long_answer="Not the literal thing.", gaps=["Debezium"]))
    client = _make_client(web_state)
    r = client.post("/api/copilot/ask", json={"question": "Debezium?"})
    assert r.status_code == 200
    body = r.json()
    assert body["verdict"] == "no"
    assert body["question"] == "Debezium?"
    assert body["gaps"] == ["Debezium"]


def test_ask_with_job_id_threads_the_job(web_state, conn, monkeypatch):
    now = utcnow_iso()
    job = Job(id="job-1", source="lever", source_job_id="src-1",
              canonical_hash="h1", title="DE", company="Monzo", location="UK",
              url="u", description="jd", compensation="", posted_at=now,
              ghost_score=None, state=JobState.DESCRIBED,
              discovered_at=now, updated_at=now)
    JobRepo(conn).add(job)
    conn.commit()

    captured = _stub_answer(monkeypatch, CopilotAnswer(question="", verdict="no"))
    client = _make_client(web_state)
    r = client.post("/api/copilot/ask", json={"question": "Q?", "job_id": "job-1"})
    assert r.status_code == 200
    assert captured["job"].company == "Monzo"


def test_ask_unknown_job_404(web_state, monkeypatch):
    _stub_answer(monkeypatch, CopilotAnswer(question="", verdict="no"))
    client = _make_client(web_state)
    r = client.post("/api/copilot/ask", json={"question": "Q?", "job_id": "ghost"})
    assert r.status_code == 404


@pytest.mark.parametrize("payload", [
    {},
    {"question": ""},
    {"question": "   "},
    {"question": 42},
])
def test_ask_rejects_bad_payloads(web_state, payload):
    client = _make_client(web_state)
    r = client.post("/api/copilot/ask", json=payload)
    assert r.status_code == 400


def test_copilot_page_renders_and_nav_links(web_state):
    client = _make_client(web_state)
    r = client.get("/copilot")
    assert r.status_code == 200
    assert "Application copilot" in r.text
    assert 'x-data="copilot()"' in r.text

    home = client.get("/")
    assert 'href="/copilot"' in home.text
