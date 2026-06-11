"""Phase 6 (7/M) — interactive skill-reconciliation web surface tests.

Coverage:
  * /api/reconcile/proposals shape + min_count filter
  * /api/reconcile/scan records gaps from stored JDs (gather-only)
  * /api/reconcile/apply inserts confirmed skills (additive), marks gaps
    certified, and rejects bad payloads — the Rule 2.6 gated act
  * /reconcile HTML renders with the Alpine hooks + the nav carries the link
"""

from __future__ import annotations

import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from auto_applier.config import Settings
from auto_applier.db.repositories import JobRepo, SkillGapRepo
from auto_applier.domain.models import Job, utcnow_iso
from auto_applier.domain.state import JobState
from auto_applier.resume.factbank import FactBank
from auto_applier.web import WebState, create_app
from auto_applier.web.onboarding import load_fact_bank, save_fact_bank


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


def _seed_bank(settings: Settings, *, skills=("Python",)) -> None:
    bank = FactBank(skills=list(skills))
    save_fact_bank(settings.data_dir, bank)


def _seed_described_job(conn, *, id="job-1", description="Python and Kubernetes role"):
    now = utcnow_iso()
    job = Job(
        id=id, source="greenhouse", source_job_id=f"src-{id}",
        canonical_hash=f"hash-{id}", title="Engineer", company="Acme",
        location="Remote", url=f"https://example.com/{id}",
        description=description, compensation="", posted_at=now,
        ghost_score=None, state=JobState.DESCRIBED,
        discovered_at=now, updated_at=now,
    )
    JobRepo(conn).add(job)
    conn.commit()
    return job


# ------------------------------------------------------------------ proposals

def test_proposals_empty(settings, web_state):
    _seed_bank(settings)
    client = _make_client(web_state)
    r = client.get("/api/reconcile/proposals")
    assert r.status_code == 200
    data = r.json()
    assert data["proposals"] == []
    assert data["bank_skill_count"] == 1


def test_proposals_lists_open_gaps_not_in_bank(settings, web_state, conn):
    _seed_bank(settings, skills=("Python",))
    gaps = SkillGapRepo(conn)
    gaps.bump("Kubernetes")
    gaps.bump("Kubernetes")
    gaps.bump("Python")  # in bank → filtered out
    conn.commit()

    client = _make_client(web_state)
    data = client.get("/api/reconcile/proposals").json()
    skills = {p["skill"]: p["count"] for p in data["proposals"]}
    assert skills.get("Kubernetes") == 2
    assert "Python" not in skills


def test_proposals_min_count_filters(settings, web_state, conn):
    _seed_bank(settings)
    gaps = SkillGapRepo(conn)
    gaps.bump("Kubernetes")
    gaps.bump("Kubernetes")
    gaps.bump("Terraform")
    conn.commit()

    client = _make_client(web_state)
    data = client.get("/api/reconcile/proposals", params={"min_count": 2}).json()
    assert [p["skill"] for p in data["proposals"]] == ["Kubernetes"]


# ------------------------------------------------------------------ scan

def test_scan_records_gaps_from_jds(settings, web_state, conn):
    _seed_bank(settings, skills=("Python",))
    _seed_described_job(conn, description="Need Kubernetes and Python experience")

    client = _make_client(web_state)
    r = client.post("/api/reconcile/scan")
    assert r.status_code == 200
    body = r.json()
    assert body["scanned"] == 1
    assert body["bumps"] >= 1  # Kubernetes at minimum; Python is in-bank → skipped

    data = client.get("/api/reconcile/proposals").json()
    skills = [p["skill"] for p in data["proposals"]]
    assert "Kubernetes" in skills and "Python" not in skills


# ------------------------------------------------------------------ apply (the gated act)

def test_apply_inserts_additively_and_certifies(settings, web_state, conn):
    _seed_bank(settings, skills=("Python",))
    gaps = SkillGapRepo(conn)
    gaps.bump("Kubernetes")
    conn.commit()

    client = _make_client(web_state)
    r = client.post("/api/reconcile/apply", json={"skills": ["Kubernetes"]})
    assert r.status_code == 200
    body = r.json()
    assert body["added"] == 1
    assert body["bank_skill_count"] == 2

    bank = load_fact_bank(settings.data_dir)
    assert set(bank.skills) == {"Python", "Kubernetes"}  # additive, nothing lost

    # The gap is now certified → no longer proposed.
    data = client.get("/api/reconcile/proposals").json()
    assert data["proposals"] == []


def test_apply_dedupes_case_insensitively(settings, web_state):
    _seed_bank(settings, skills=("Python",))
    client = _make_client(web_state)
    r = client.post("/api/reconcile/apply", json={"skills": ["python"]})
    assert r.status_code == 200
    assert r.json()["added"] == 0  # already in the bank
    assert load_fact_bank(settings.data_dir).skills == ["Python"]


@pytest.mark.parametrize("payload", [
    {},                       # missing key
    {"skills": "Kubernetes"},  # not a list
    {"skills": [1, 2]},        # not strings
    {"skills": ["  ", ""]},    # nothing after stripping
])
def test_apply_rejects_bad_payloads(settings, web_state, payload):
    _seed_bank(settings)
    client = _make_client(web_state)
    r = client.post("/api/reconcile/apply", json=payload)
    assert r.status_code == 400
    # Bank untouched on every rejected payload.
    assert load_fact_bank(settings.data_dir).skills == ["Python"]


# ------------------------------------------------------------------ HTML

def test_reconcile_page_renders(settings, web_state):
    client = _make_client(web_state)
    r = client.get("/reconcile")
    assert r.status_code == 200
    assert "Skill reconciliation" in r.text
    assert "x-data=\"reconcile()\"" in r.text


def test_nav_links_to_reconcile(settings, web_state):
    client = _make_client(web_state)
    r = client.get("/")
    assert r.status_code == 200
    assert 'href="/reconcile"' in r.text
