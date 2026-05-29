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

from av3.config import Settings
from av3.resume.factbank import Contact, FactBank, WorkEntry
from av3.web import WebState, create_app
from av3.web.onboarding import (
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
