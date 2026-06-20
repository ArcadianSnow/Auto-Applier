"""Phase 4 (5/M) — onboarding wizard tests.

Coverage:
  * OnboardingStatus completeness gates (each step)
  * Atomic write helpers (save_fact_bank, save_user_config)
  * Each step endpoint persists + the status snapshot reflects it
  * Step-wise idempotency (re-post overwrites cleanly)
  * Wholesale-replace semantics on work_history / skills
  * "No silent default" invariant for work_authorization + sponsorship
  * Onboarding HTML page renders
  * Telemetry decision counts even when disabled

The wizard touches the same files the rest of v3 reads from, so these
tests verify both the API response shape AND the on-disk state.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from auto_applier.config import Settings
from auto_applier.resume.factbank import Contact, FactBank, WorkEntry
from auto_applier.web import WebState, create_app
from auto_applier.web.onboarding import (
    OnboardingStatus,
    fact_bank_path,
    load_fact_bank,
    load_user_config,
    merge_contact,
    merge_skills,
    merge_work_auth,
    merge_work_history,
    onboarding_status,
    save_fact_bank,
    save_user_config,
)


@pytest.fixture
def web_state(settings: Settings, conn: sqlite3.Connection) -> WebState:
    return WebState(
        settings=settings,
        app_db_path=settings.app_db_path,
        events_db_path=settings.events_db_path,
    )


def _make_client(web_state: WebState) -> TestClient:
    app = create_app(state=web_state, service=None)
    return TestClient(app)


# --------------------------------------------------------------- résumé extract endpoint

class _StubLLM:
    """complete_json returns a fixed fact-bank dict (accepts the think/num_predict kwargs the
    extractor passes)."""

    def __init__(self, payload: dict):
        self._payload = payload

    async def complete_json(self, prompt, *, system="", think=None, num_predict=None):
        return self._payload


class TestExtractResume:

    _FB = {
        "contact": {"name": "Jane Doe", "email": "jane@x.com"},
        "work_history": [
            {"company": "Acme", "title": "Analyst", "start": "2020",
             "end": "Present", "bullets": ["did things"]},
        ],
        "skills": ["SQL", "Python"],
    }

    def _client(self, web_state, monkeypatch, payload=None):
        import auto_applier.llm.complete as cmod
        monkeypatch.setattr(cmod, "build_default", lambda settings: _StubLLM(payload or self._FB))
        return _make_client(web_state)

    def test_extract_returns_factbank_draft_without_persisting(self, web_state, monkeypatch):
        import base64
        client = self._client(web_state, monkeypatch)
        b64 = base64.b64encode(b"Jane Doe\nAnalyst at Acme\nSQL, Python").decode()
        r = client.post("/api/onboarding/extract-resume",
                        json={"filename": "resume.txt", "content_b64": b64})
        assert r.status_code == 200
        body = r.json()
        assert body["contact"]["name"] == "Jane Doe"
        assert len(body["work_history"]) == 1
        assert body["work_history"][0]["company"] == "Acme"
        assert body["skills"] == ["SQL", "Python"]
        # DRAFT only — extraction must not persist; the per-step Save endpoints are the writers.
        assert client.get("/api/onboarding/state").json()["has_work_history"] is False

    def test_extract_unsupported_extension_is_400(self, web_state, monkeypatch):
        import base64
        client = self._client(web_state, monkeypatch)
        b64 = base64.b64encode(b"x").decode()
        r = client.post("/api/onboarding/extract-resume",
                        json={"filename": "resume.doc", "content_b64": b64})
        assert r.status_code == 400

    def test_extract_missing_fields_is_400(self, web_state, monkeypatch):
        client = self._client(web_state, monkeypatch)
        r = client.post("/api/onboarding/extract-resume", json={"filename": "resume.txt"})
        assert r.status_code == 400


# --------------------------------------------------------------- background seed-boards

class _FakeSeeder:
    """Stand-in BoardSeeder: instant run() + a fixed result, so the background job tests never
    touch the network."""

    def __init__(self, **kw):
        self.kw = kw

    def run(self):
        from auto_applier.pipeline.seed_worker import SeedSummary
        s = SeedSummary(probed=5, kept=2, dead=1)
        s.added = {"greenhouse": ["acme", "globex"]}
        return s

    def merged_targeting(self, summary):
        return {"greenhouse_boards": ["acme", "globex"], "lever_boards": [], "ashby_boards": []}


class TestSeedBoardsBackground:

    def test_run_seed_job_saves_boards_and_marks_done(self, settings: Settings, monkeypatch):
        import asyncio

        import auto_applier.pipeline.seed_worker as sw
        from auto_applier.web import routes as R
        monkeypatch.setattr(sw, "BoardSeeder", _FakeSeeder)
        R._SEED = {"status": "idle"}

        asyncio.run(R._run_seed_job(settings, ["data analyst"], 50))

        assert R._SEED["status"] == "done"
        assert R._SEED["kept"] == 2
        cfg = load_user_config(settings.data_dir)
        assert cfg["targeting"]["greenhouse_boards"] == ["acme", "globex"]

    def test_start_endpoint_runs_in_background_then_done(self, web_state, monkeypatch):
        import time

        import auto_applier.pipeline.seed_worker as sw
        from auto_applier.web import routes as R
        monkeypatch.setattr(sw, "BoardSeeder", _FakeSeeder)
        R._SEED = {"status": "idle"}
        client = _make_client(web_state)

        r = client.post("/api/onboarding/seed-boards/start", json={"titles": ["data analyst"]})
        assert r.status_code == 200
        assert r.json()["status"] in ("running", "done")

        final = None
        for _ in range(60):
            s = client.get("/api/onboarding/seed-boards/status").json()
            if s["status"] != "running":
                final = s
                break
            time.sleep(0.05)
        assert final is not None and final["status"] == "done"
        assert final["kept"] == 2
        cfg = load_user_config(web_state.settings.data_dir)
        assert cfg["targeting"]["greenhouse_boards"] == ["acme", "globex"]

    def test_start_rejects_non_list_titles(self, web_state, monkeypatch):
        import auto_applier.pipeline.seed_worker as sw
        from auto_applier.web import routes as R
        monkeypatch.setattr(sw, "BoardSeeder", _FakeSeeder)
        R._SEED = {"status": "idle"}
        client = _make_client(web_state)
        r = client.post("/api/onboarding/seed-boards/start", json={"titles": "data analyst"})
        assert r.status_code == 400


# --------------------------------------------------------------- persistence

class TestPersistence:

    def test_save_and_load_fact_bank_round_trip(self, settings: Settings):
        bank = FactBank(
            contact=Contact(name="A", email="a@x"),
            skills=["Python"],
            work_authorization="US citizen",
            requires_sponsorship=False,
        )
        save_fact_bank(settings.data_dir, bank)
        loaded = load_fact_bank(settings.data_dir)
        assert loaded.contact.name == "A"
        assert loaded.contact.email == "a@x"
        assert loaded.skills == ["Python"]
        assert loaded.work_authorization == "US citizen"
        assert loaded.requires_sponsorship is False

    def test_load_fact_bank_returns_empty_when_missing(self, settings: Settings):
        # No file yet → empty bank, not an exception.
        loaded = load_fact_bank(settings.data_dir)
        assert loaded.contact.name == ""
        assert loaded.skills == []

    def test_save_user_config_is_atomic(self, settings: Settings):
        save_user_config(settings.data_dir, {"foo": 1})
        # The tmp sibling must NOT linger after success.
        tmp = settings.data_dir / "user_config.json.tmp"
        assert not tmp.exists()
        # The real file is there with our content.
        assert load_user_config(settings.data_dir) == {"foo": 1}

    def test_corrupt_user_config_is_quarantined(self, settings: Settings):
        p = settings.data_dir / "user_config.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("{not valid json", encoding="utf-8")
        # Loading should NOT raise — it returns {} after moving the bad
        # file to .broken.
        result = load_user_config(settings.data_dir)
        assert result == {}
        assert (settings.data_dir / "user_config.json.broken").exists()


# --------------------------------------------------------------- merges

class TestMergeHelpers:

    def test_merge_contact_replaces_fields(self):
        bank = FactBank(contact=Contact(name="old", email="old@x"))
        merge_contact(bank, {"name": "new", "email": "new@x"})
        assert bank.contact.name == "new"
        assert bank.contact.email == "new@x"

    def test_merge_work_history_is_wholesale_replace(self):
        bank = FactBank(work_history=[
            WorkEntry(company="old-co", title="old-title",
                      start="", end="", bullets=[])
        ])
        merge_work_history(bank, [
            {"company": "new-co", "title": "new-title",
             "start": "2020", "end": "2024",
             "bullets": ["b1", "b2"]},
        ])
        assert len(bank.work_history) == 1
        assert bank.work_history[0].company == "new-co"
        assert bank.work_history[0].bullets == ["b1", "b2"]

    def test_merge_skills_dedupes_case_insensitively(self):
        bank = FactBank()
        merge_skills(bank, ["Python", "python", "SQL", " ", ""])
        # First case wins; later case-variants are skipped.
        assert bank.skills == ["Python", "SQL"]

    def test_merge_work_auth_no_silent_default(self):
        bank = FactBank()
        merge_work_auth(bank, {
            "work_authorization": "US citizen",
            "requires_sponsorship": None,  # explicit null = skip
        })
        assert bank.work_authorization == "US citizen"
        # Explicit null stays null — NOT silently coerced to False.
        assert bank.requires_sponsorship is None

    def test_merge_work_auth_accepts_false(self):
        bank = FactBank()
        merge_work_auth(bank, {"requires_sponsorship": False})
        assert bank.requires_sponsorship is False


# --------------------------------------------------------------- status

class TestOnboardingStatus:

    def test_empty_state_is_not_complete(self, settings: Settings):
        status = onboarding_status(settings.data_dir)
        assert status.is_complete is False
        assert status.has_contact is False
        assert status.has_skills is False
        assert status.has_work_auth is False
        assert status.has_targeting is False
        assert status.has_telemetry_decision is False

    def test_complete_when_all_gates_pass(self, settings: Settings):
        # Fully populated fact bank.
        bank = FactBank(
            contact=Contact(name="A", email="a@x"),
            work_history=[
                WorkEntry(company="X", title="T",
                          start="2020", end="2024", bullets=["b"]),
            ],
            skills=["Python"],
            work_authorization="US citizen",
            requires_sponsorship=False,
        )
        save_fact_bank(settings.data_dir, bank)
        # Targeting + telemetry decision in user_config.
        save_user_config(settings.data_dir, {
            "targeting": {"titles": ["Senior Eng"], "locations": ["Remote"]},
            "telemetry": {"enabled": False},
        })
        status = onboarding_status(settings.data_dir)
        assert status.is_complete is True

    def test_telemetry_decision_counts_when_disabled(self, settings: Settings):
        # The explicit decision (even "no") is what flips the gate.
        save_user_config(settings.data_dir, {
            "telemetry": {"enabled": False},
        })
        status = onboarding_status(settings.data_dir)
        assert status.has_telemetry_decision is True

    def test_work_auth_gate_passes_on_sponsorship_alone(
        self, settings: Settings
    ):
        # User left work_authorization blank but answered sponsorship.
        bank = FactBank(requires_sponsorship=False)
        save_fact_bank(settings.data_dir, bank)
        status = onboarding_status(settings.data_dir)
        assert status.has_work_auth is True


# --------------------------------------------------------------- endpoints

class TestEndpointContact:

    def test_post_persists_and_returns_status(
        self, settings: Settings, web_state: WebState
    ):
        with _make_client(web_state) as client:
            r = client.post("/api/onboarding/contact", json={
                "name": "Pat Example",
                "email": "pat@example.com",
                "phone": "+1 555 0100",
                "location": "Seattle, WA",
            })
        assert r.status_code == 200
        body = r.json()
        assert body["has_contact"] is True
        assert body["contact"]["name"] == "Pat Example"
        # On disk.
        assert load_fact_bank(settings.data_dir).contact.email == "pat@example.com"

    def test_post_400_when_body_not_json_object(self, web_state: WebState):
        with _make_client(web_state) as client:
            r = client.post("/api/onboarding/contact",
                            content=b"[not json]",
                            headers={"Content-Type": "application/json"})
        assert r.status_code == 400


class TestEndpointWorkHistory:

    def test_replace_wholesale(
        self, settings: Settings, web_state: WebState
    ):
        # Seed an old entry that the next save must overwrite.
        save_fact_bank(settings.data_dir, FactBank(work_history=[
            WorkEntry(company="OLD", title="X", start="", end="", bullets=[]),
        ]))
        with _make_client(web_state) as client:
            r = client.post("/api/onboarding/work-history", json={
                "work_history": [
                    {"company": "Acme", "title": "Eng",
                     "start": "2020", "end": "Present",
                     "bullets": ["did things"]},
                ],
            })
        assert r.status_code == 200
        body = r.json()
        assert body["has_work_history"] is True
        bank = load_fact_bank(settings.data_dir)
        assert [w.company for w in bank.work_history] == ["Acme"]

    def test_400_when_payload_not_list(self, web_state: WebState):
        with _make_client(web_state) as client:
            r = client.post("/api/onboarding/work-history",
                            json={"work_history": "not a list"})
        assert r.status_code == 400


class TestEndpointSkills:

    def test_dedupe_and_save(
        self, settings: Settings, web_state: WebState
    ):
        with _make_client(web_state) as client:
            r = client.post("/api/onboarding/skills", json={
                "skills": ["Python", "python", "SQL"],
            })
        assert r.status_code == 200
        assert load_fact_bank(settings.data_dir).skills == ["Python", "SQL"]


class TestEndpointWorkAuth:

    def test_explicit_null_sponsorship_persists(
        self, settings: Settings, web_state: WebState
    ):
        with _make_client(web_state) as client:
            r = client.post("/api/onboarding/work-auth", json={
                "work_authorization": "US citizen",
                "requires_sponsorship": None,
            })
        assert r.status_code == 200
        bank = load_fact_bank(settings.data_dir)
        assert bank.requires_sponsorship is None
        assert bank.work_authorization == "US citizen"

    def test_no_default_when_unset(
        self, settings: Settings, web_state: WebState
    ):
        with _make_client(web_state) as client:
            # User submitted nothing — the bank's sponsorship stays None.
            r = client.post("/api/onboarding/work-auth", json={})
        assert r.status_code == 200
        bank = load_fact_bank(settings.data_dir)
        assert bank.requires_sponsorship is None


class TestEndpointTargeting:

    def test_persists_into_user_config(
        self, settings: Settings, web_state: WebState
    ):
        with _make_client(web_state) as client:
            r = client.post("/api/onboarding/targeting", json={
                "titles": ["Senior Eng", "Staff Eng"],
                "locations": ["Remote"],
                "remote_ok": True,
                "onsite_ok": False,
                "salary_floor": 150000,
                "seniority": "senior",
            })
        assert r.status_code == 200
        cfg = load_user_config(settings.data_dir)
        assert cfg["targeting"]["titles"] == ["Senior Eng", "Staff Eng"]
        assert cfg["targeting"]["salary_floor"] == 150000

    def test_partial_update_preserves_other_keys(
        self, settings: Settings, web_state: WebState
    ):
        save_user_config(settings.data_dir, {
            "targeting": {
                "titles": ["X"], "locations": ["L"],
                "remote_ok": True, "onsite_ok": True,
                "salary_floor": 100000, "seniority": "",
            },
        })
        with _make_client(web_state) as client:
            r = client.post("/api/onboarding/targeting",
                            json={"salary_floor": 200000})
        assert r.status_code == 200
        cfg = load_user_config(settings.data_dir)
        # Salary floor updated, but titles + locations preserved.
        assert cfg["targeting"]["salary_floor"] == 200000
        assert cfg["targeting"]["titles"] == ["X"]
        assert cfg["targeting"]["locations"] == ["L"]


    def test_persists_preferences(self, settings: Settings, web_state: WebState):
        # The goal-chat's soft-preferences blob round-trips through the single targeting writer.
        with _make_client(web_state) as client:
            r = client.post("/api/onboarding/targeting", json={
                "titles": ["Backend Engineer"],
                "preferences": ["work-life balance", "Python stack"],
            })
        assert r.status_code == 200
        cfg = load_user_config(settings.data_dir)
        assert cfg["targeting"]["preferences"] == ["work-life balance", "Python stack"]


class TestGoalChat:
    """The scripted goal-elicitation chat endpoint (Direction 1, Phase B). A combined stub payload
    serves every step — each step's finalize picks only its own keys."""

    _PAYLOAD = {
        "titles": ["Senior Backend Engineer"], "seniority": "senior",
        "locations": ["Remote (US)"], "remote_ok": True, "onsite_ok": False,
        "preferences": ["work-life balance"],
    }

    def _client(self, web_state, monkeypatch):
        import auto_applier.llm.complete as cmod
        monkeypatch.setattr(cmod, "build_default",
                            lambda settings: _StubLLM(self._PAYLOAD))
        return _make_client(web_state)

    def test_start_returns_first_question(self, web_state, monkeypatch):
        client = self._client(web_state, monkeypatch)
        r = client.post("/api/onboarding/goal-chat", json={})
        assert r.status_code == 200
        body = r.json()
        assert body["next_step"] == "roles"
        assert body["done"] is False
        assert body["reply"]  # the scripted first question

    def test_answer_advances_and_fills_draft(self, web_state, monkeypatch):
        client = self._client(web_state, monkeypatch)
        r = client.post("/api/onboarding/goal-chat",
                        json={"step": "roles", "answer": "sr be dev", "draft": {}})
        body = r.json()
        assert body["next_step"] == "location"
        assert body["draft"]["titles"] == ["Senior Backend Engineer"]
        assert body["draft"]["seniority"] == "senior"

    def test_unknown_step_is_400(self, web_state, monkeypatch):
        client = self._client(web_state, monkeypatch)
        r = client.post("/api/onboarding/goal-chat",
                        json={"step": "bogus", "answer": "x", "draft": {}})
        assert r.status_code == 400

    def test_full_walk_to_done_produces_draft(self, web_state, monkeypatch):
        client = self._client(web_state, monkeypatch)
        draft: dict = {}
        # roles → location → comp → priorities; the last turn is done.
        for step, answer in [
            ("roles", "senior backend engineer"),
            ("location", "remote in the US"),
            ("comp", "150k"),
            ("priorities", "work-life balance"),
        ]:
            r = client.post("/api/onboarding/goal-chat",
                            json={"step": step, "answer": answer, "draft": draft})
            assert r.status_code == 200
            body = r.json()
            draft = body["draft"]
        assert body["done"] is True
        assert draft["titles"] == ["Senior Backend Engineer"]
        assert draft["salary_floor"] == 150000           # deterministic parse
        assert draft["preferences"] == ["work-life balance"]
        # The chat does NOT persist — nothing is written until the user saves via /targeting.
        assert client.get("/api/onboarding/state").json()["has_targeting"] is False


class TestEndpointTelemetry:

    def test_disabled_decision_counts(
        self, settings: Settings, web_state: WebState
    ):
        with _make_client(web_state) as client:
            r = client.post("/api/onboarding/telemetry",
                            json={"enabled": False})
        body = r.json()
        assert body["has_telemetry_decision"] is True
        cfg = load_user_config(settings.data_dir)
        assert cfg["telemetry"]["enabled"] is False

    def test_default_enabled_false_when_omitted(
        self, settings: Settings, web_state: WebState
    ):
        with _make_client(web_state) as client:
            r = client.post("/api/onboarding/telemetry",
                            json={"handle": "me"})
        assert r.status_code == 200
        cfg = load_user_config(settings.data_dir)
        # Telemetry must default to OFF when the user submitted no
        # explicit enabled flag — spec §9 opt-in invariant.
        assert cfg["telemetry"]["enabled"] is False
        assert cfg["telemetry"]["handle"] == "me"


class TestEndpointWebPrefs:

    def test_persists_hotkey_and_idle(
        self, settings: Settings, web_state: WebState
    ):
        with _make_client(web_state) as client:
            r = client.post("/api/onboarding/web-prefs", json={
                "hotkey_enabled": True,
                "hotkey": "F7",
                "idle_detect_enabled": True,
                "idle_threshold_s": 120,
            })
        assert r.status_code == 200
        cfg = load_user_config(settings.data_dir)
        assert cfg["web"]["hotkey"] == "F7"
        assert cfg["web"]["idle_threshold_s"] == 120


class TestStateEndpoint:

    def test_empty_install(self, web_state: WebState):
        with _make_client(web_state) as client:
            r = client.get("/api/onboarding/state")
        assert r.status_code == 200
        body = r.json()
        assert body["is_complete"] is False
        assert body["has_contact"] is False
        # Empty defaults render so the wizard can hydrate fields.
        assert body["contact"] == {
            "name": "", "email": "", "phone": "", "location": "",
            "links": {},
        }


class TestOnboardingPage:

    def test_renders(self, web_state: WebState):
        with _make_client(web_state) as client:
            r = client.get("/onboarding")
        assert r.status_code == 200
        assert "text/html" in r.headers["content-type"]
        assert "onboarding" in r.text.lower()


class TestFullFlowCompletes:
    """End-to-end: walking every step flips is_complete to True."""

    def test_step_by_step(self, settings: Settings, web_state: WebState):
        with _make_client(web_state) as client:
            client.post("/api/onboarding/contact", json={
                "name": "Pat", "email": "p@x",
            })
            client.post("/api/onboarding/work-history", json={
                "work_history": [{"company": "X", "title": "T",
                                  "start": "2020", "end": "2024",
                                  "bullets": ["b"]}],
            })
            client.post("/api/onboarding/skills",
                        json={"skills": ["Python"]})
            client.post("/api/onboarding/work-auth", json={
                "work_authorization": "US citizen",
                "requires_sponsorship": False,
            })
            client.post("/api/onboarding/targeting", json={
                "titles": ["Senior Eng"], "locations": ["Remote"],
            })
            client.post("/api/onboarding/telemetry",
                        json={"enabled": False})
            r = client.get("/api/onboarding/state")
        body = r.json()
        assert body["is_complete"] is True


# --------------------------------------------------------------- connect-email endpoint (Direction 4 Phase D)

class _FakeIMAP:
    """Stand-in for imaplib.IMAP4_SSL. Configure class attrs per test to simulate
    connect/login outcomes. Records login calls so tests can assert credentials flowed."""

    raise_on_connect = False
    raise_on_login = False     # raises imaplib.IMAP4.error (auth failure)
    last_login: tuple | None = None

    def __init__(self, host, port, timeout=None):
        if type(self).raise_on_connect:
            raise OSError("connection refused")
        self.host = host

    def login(self, user, password):
        import imaplib
        if type(self).raise_on_login:
            raise imaplib.IMAP4.error("auth failed")
        type(self).last_login = (user, password)

    def logout(self):
        pass


@pytest.fixture(autouse=True)
def _reset_fake_imap(monkeypatch):
    _FakeIMAP.raise_on_connect = False
    _FakeIMAP.raise_on_login = False
    _FakeIMAP.last_login = None
    # Register the env key with monkeypatch so any write by the endpoint is reverted on teardown.
    monkeypatch.setenv("AV3_IMAP_PASSWORD", "")
    monkeypatch.delenv("AV3_IMAP_PASSWORD", raising=False)
    yield


class TestInboxConnect:
    """The guided email-setup endpoint: verify creds live, then split secret (.env) from
    non-secret config (user_config.json). The wizard's alternative to hand-editing files."""

    def test_connect_persists_config_and_secret(self, web_state: WebState, monkeypatch):
        monkeypatch.setattr("imaplib.IMAP4_SSL", _FakeIMAP)
        with _make_client(web_state) as client:
            r = client.post("/api/onboarding/inbox", json={
                "user": "me@gmail.com", "password": "abcd efgh ijkl mnop",
            })
        assert r.status_code == 200, r.text
        assert r.json()["ok"] is True
        # Non-secret config → user_config.json
        cfg = load_user_config(web_state.settings.data_dir)
        assert cfg["inbox"]["enabled"] is True
        assert cfg["inbox"]["user"] == "me@gmail.com"
        assert cfg["inbox"]["host"] == "imap.gmail.com"
        # Secret → .env in the data dir, NEVER user_config.json
        assert "abcd" not in json.dumps(cfg)
        env_text = (web_state.settings.data_dir / ".env").read_text(encoding="utf-8")
        assert "AV3_IMAP_PASSWORD" in env_text
        assert "abcd efgh ijkl mnop" in env_text
        # Current process can see it immediately
        import os as _os
        assert _os.environ["AV3_IMAP_PASSWORD"] == "abcd efgh ijkl mnop"
        # The live verify actually ran with the given creds
        assert _FakeIMAP.last_login == ("me@gmail.com", "abcd efgh ijkl mnop")

    def test_auth_failure_400_and_saves_nothing(self, web_state: WebState, monkeypatch):
        _FakeIMAP.raise_on_login = True
        monkeypatch.setattr("imaplib.IMAP4_SSL", _FakeIMAP)
        with _make_client(web_state) as client:
            r = client.post("/api/onboarding/inbox", json={
                "user": "me@gmail.com", "password": "wrong",
            })
        assert r.status_code == 400
        assert "authentication failed" in r.json()["detail"].lower()
        # Nothing persisted on a failed verify.
        cfg = load_user_config(web_state.settings.data_dir)
        assert "inbox" not in cfg
        assert not (web_state.settings.data_dir / ".env").exists()

    def test_connect_failure_400(self, web_state: WebState, monkeypatch):
        _FakeIMAP.raise_on_connect = True
        monkeypatch.setattr("imaplib.IMAP4_SSL", _FakeIMAP)
        with _make_client(web_state) as client:
            r = client.post("/api/onboarding/inbox", json={
                "user": "me@gmail.com", "password": "x", "host": "imap.bad.example",
            })
        assert r.status_code == 400
        assert "could not connect" in r.json()["detail"].lower()

    def test_missing_fields_400_without_imap_call(self, web_state: WebState, monkeypatch):
        def _boom(*a, **k):
            raise AssertionError("must not attempt IMAP without both fields")
        monkeypatch.setattr("imaplib.IMAP4_SSL", _boom)
        with _make_client(web_state) as client:
            r = client.post("/api/onboarding/inbox", json={"user": "me@gmail.com"})
        assert r.status_code == 400

    def test_state_echoes_inbox_without_password(self, web_state: WebState, monkeypatch):
        monkeypatch.setattr("imaplib.IMAP4_SSL", _FakeIMAP)
        with _make_client(web_state) as client:
            client.post("/api/onboarding/inbox", json={
                "user": "me@gmail.com", "password": "secret-pw-here",
            })
            body = client.get("/api/onboarding/state").json()
        assert body["inbox"]["enabled"] is True
        assert body["inbox"]["user"] == "me@gmail.com"
        # The status snapshot must never carry the password.
        assert "secret-pw-here" not in json.dumps(body)
