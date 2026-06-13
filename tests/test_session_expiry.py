"""Session-expiry graceful degradation (spec section 8b) — contract tests.

Covers the four wiring layers of (4/M):

  1. **Pure detector** (``av3/sources/browser/detect.py``) — ``detect_login_wall``
     happy/sad cases. URL match wins outright; HTML-only match requires BOTH a
     password input AND a sign-in label.
  2. **Health registry** (``av3/sources/health.py``) — mark/unmark, snapshot,
     idempotent telemetry (no flood on repeat marks).
  3. **Driver hook** (``apply_base.check_auth_wall``) — marks the source +
     returns the signal string when detected, leaves health unchanged when not.
  4. **Apply worker integration** — a paused source is silently skipped (no
     state change, the job stays in QUEUED_APPLY for next cycle); other sources
     keep running.

We don't drive a real Playwright page anywhere — a tiny ``_FakePage`` exposes
``url`` + ``content()`` and is enough to exercise the detector. The apply worker
tests already have a fake-driver pattern; we reuse that.
"""

from __future__ import annotations

import asyncio
import random
import sqlite3

import pytest

from auto_applier.config.settings import Settings
from auto_applier.db.repositories import JobRepo
from auto_applier.domain.models import Job
from auto_applier.domain.state import ApplicationStatus, ApplyMode, JobState
from auto_applier.pipeline.apply_worker import ApplyWorker, DriverEntry
from auto_applier.resume.factbank import Contact, FactBank, WorkEntry
from auto_applier.sources.browser.apply_base import check_auth_wall
from auto_applier.sources.browser.detect import detect_login_wall
from auto_applier.sources.health import (
    SourceHealthState,
    is_paused,
    mark_auth_required,
    mark_healthy,
    paused_sources,
    reset_health,
    snapshot,
)


# --------------------------------------------------------------- fixtures

@pytest.fixture(autouse=True)
def _clean_health():
    """Reset the process-global health registry between tests so cases don't
    bleed. ``autouse=True`` because every test in this module touches health."""
    reset_health()
    yield
    reset_health()


class _FakePage:
    """Minimal stand-in for a Playwright Page exposing ``url`` + ``content()``.
    Enough to exercise ``check_auth_wall`` without spinning up a browser."""

    def __init__(self, *, url: str = "", html: str = ""):
        self.url = url
        self._html = html

    async def content(self) -> str:
        return self._html


# --------------------------------------------------------------- detector

def test_detect_login_wall_url_match():
    """A URL marker is sufficient on its own — the page redirected."""
    r = detect_login_wall("https://example.com/account/login?next=/apply", "")
    assert r.present is True
    assert "url:" in r.signal


def test_detect_login_wall_signin_url():
    """Different URL marker, same outcome."""
    r = detect_login_wall("https://example.com/users/sign_in", "")
    assert r.present is True


def test_detect_login_wall_html_requires_both_signals():
    """HTML-only detection requires BOTH a password input AND a sign-in label/
    heading. A password input alone (e.g. a passwordless-candidate widget on a
    normal apply form) isn't enough."""
    # Password input alone -> no wall.
    only_pw = '<input type="password" name="password" />'
    assert detect_login_wall("https://x.example.com/apply", only_pw).present is False

    # Sign-in label alone -> no wall.
    only_label = '<h2>Sign in</h2><input type="email" name="email" />'
    assert detect_login_wall("https://x.example.com/apply", only_label).present is False

    # Both -> wall.
    both = (
        '<h1>Please sign in</h1>'
        '<input type="email" /><input type="password" />'
        '<button>Sign in</button>'
    )
    r = detect_login_wall("https://x.example.com/apply", both)
    assert r.present is True
    assert "html:" in r.signal


def test_detect_login_wall_normal_apply_form_passes():
    """A normal Lever-shape apply form (name + email + résumé, no password) must
    NOT trip the detector - false positives here would pause healthy sources."""
    html = (
        '<form><input name="name" /><input name="email" />'
        '<input type="file" id="resume-upload-input" />'
        '<button id="btn-submit">Apply</button></form>'
    )
    r = detect_login_wall("https://jobs.lever.co/acme/abc-123/apply", html)
    assert r.present is False


# --------------------------------------------------------------- health registry

def test_mark_auth_required_pauses_source():
    """A fresh source defaults to HEALTHY; marking flips it to AUTH_REQUIRED."""
    assert is_paused("lever") is False
    mark_auth_required("lever", reason="session expired")
    assert is_paused("lever") is True
    rec = snapshot()["lever"]
    assert rec.state is SourceHealthState.AUTH_REQUIRED
    assert rec.reason == "session expired"


def test_mark_healthy_clears_pause():
    """Marking a paused source healthy clears the pause; subsequent
    ``is_paused`` returns False."""
    mark_auth_required("lever")
    assert is_paused("lever") is True
    mark_healthy("lever")
    assert is_paused("lever") is False


def test_paused_sources_returns_set():
    """Multiple sources can be paused independently; ``paused_sources`` returns
    the set."""
    mark_auth_required("lever")
    mark_auth_required("greenhouse")
    mark_healthy("greenhouse")
    assert paused_sources() == {"lever"}


def test_snapshot_does_not_leak_internal_state():
    """The returned dict must be a copy - mutating it must not affect the
    registry. This is what lets the dashboard safely poll without locking."""
    mark_auth_required("lever")
    snap = snapshot()
    snap["lever"].reason = "MUTATED"
    snap.pop("lever")
    # Internal state still intact.
    assert is_paused("lever") is True
    assert snapshot()["lever"].reason != "MUTATED"


def test_empty_source_name_is_noop():
    """Defensive: an empty source name on either mark path is a silent no-op
    (callers might pass ``Job.source`` which could in theory be empty)."""
    mark_auth_required("")
    assert paused_sources() == set()
    mark_healthy("")
    assert paused_sources() == set()
    assert is_paused("") is False


def test_telemetry_emitted_on_transition(sink):
    """A HEALTHY -> AUTH_REQUIRED transition emits a ``session_expiry`` event.
    Repeated calls with the same source DO NOT re-emit (idempotent telemetry)."""
    mark_auth_required("lever", reason="login wall detected")
    mark_auth_required("lever", reason="login wall detected again")  # no second event

    rows = [r for r in sink.recent(limit=20) if r["stage"] == "session_expiry"]
    assert len(rows) == 1
    assert rows[0]["status"] == "auth_required"
    assert rows[0]["platform"] == "lever"

    # Flip to healthy: another event.
    mark_healthy("lever")
    rows = [r for r in sink.recent(limit=20) if r["stage"] == "session_expiry"]
    assert len(rows) == 2
    # Most recent first via recent()'s DESC ordering.
    assert rows[0]["status"] == "healthy"


def test_telemetry_no_event_when_already_healthy(sink):
    """``mark_healthy`` on an already-healthy source must not emit (avoid noise
    on the polling that the scheduler will do)."""
    mark_healthy("lever")
    rows = [r for r in sink.recent(limit=20) if r["stage"] == "session_expiry"]
    assert rows == []


# --------------------------------------------------------------- driver hook

def test_check_auth_wall_marks_source_and_returns_signal():
    """The driver-facing wrapper returns the signal string AND flips the source
    to AUTH_REQUIRED in one call - the combined contract is what the per-ATS
    drivers depend on for their early-exit branch."""
    page = _FakePage(url="https://acme.com/account/login", html="")
    signal = asyncio.run(check_auth_wall(page, "lever"))
    assert signal != ""
    assert is_paused("lever") is True


def test_check_auth_wall_clean_form_returns_empty_string():
    """A normal apply form returns an empty signal AND leaves health untouched."""
    page = _FakePage(
        url="https://jobs.lever.co/acme/abc-123/apply",
        html='<form><input name="name"/><input name="email"/></form>',
    )
    signal = asyncio.run(check_auth_wall(page, "lever"))
    assert signal == ""
    assert is_paused("lever") is False


# --------------------------------------------------------------- apply worker

def _bank() -> FactBank:
    return FactBank(
        contact=Contact(name="Pat Doe", email="pat@example.com"),
        skills=["python"],
        work_history=[WorkEntry(company="Acme", title="Eng", start="2020", end="2023")],
    )


def _seed_queued(conn: sqlite3.Connection, *, source: str, source_job_id: str) -> Job:
    """Insert a job at QUEUED_APPLY via the canonical state walk."""
    repo = JobRepo(conn)
    job = Job(
        source=source,
        source_job_id=source_job_id,
        title="Senior Engineer",
        company="BetaCo",
        description="JD",
        url=f"https://example.com/{source}/{source_job_id}",
    )
    repo.add(job)
    repo.set_state(job.id, JobState.DESCRIBED)
    repo.set_state(job.id, JobState.SCORED)
    repo.set_state(job.id, JobState.DECIDED)
    repo.set_state(job.id, JobState.QUEUED_APPLY)
    return repo.get(job.id)  # type: ignore[return-value]


class _RecordingDriver:
    """Fake driver that records every call. If a job's source is in
    ``called_for``, ``prepare`` was hit at least once for it."""

    def __init__(self):
        self.called_for: list[str] = []

    def listing_from_job(self, job):
        return job  # we don't care about the listing shape here

    async def prepare(self, page, listing, applicant, resume_path, *,
                       cover_letter_path="", dry_run, mode, resolver):
        self.called_for.append(listing.source)
        from auto_applier.sources.browser.apply_base import ApplyOutcome
        from auto_applier.sources.browser.detect import classify_captcha
        return ApplyOutcome(
            job_url=listing.url,
            captcha=classify_captcha("", []),
            mode=mode,
            status=None if dry_run else ApplicationStatus.APPLIED,
            submitted=not dry_run,
        )


async def _new_page():  # stub - the recording driver doesn't touch it
    return None


def _build_worker(
    settings: Settings,
    conn: sqlite3.Connection,
    *,
    drivers: dict[str, DriverEntry],
    dry_run: bool = True,
    mode: ApplyMode = ApplyMode.BROWSER_AUTO,
) -> ApplyWorker:
    return ApplyWorker(
        settings=settings,
        conn=conn,
        fact_bank=_bank(),
        resume_path="/tmp/resume.pdf",
        new_page=_new_page,
        mode=mode,
        dry_run=dry_run,
        sleep=lambda s: asyncio.sleep(0),  # no real pacing in tests
        rng=random.Random(0),
        drivers=drivers,
    )


def test_apply_worker_skips_paused_source(settings, conn):
    """When ``lever`` is marked AUTH_REQUIRED, the apply worker skips its jobs
    silently without changing state. The job stays in QUEUED_APPLY for the next
    cycle (after the user re-logs in and marks healthy)."""
    job = _seed_queued(conn, source="lever", source_job_id="paused-1")
    mark_auth_required("lever", reason="test")

    drv = _RecordingDriver()
    drivers = {
        "lever": DriverEntry(drv.listing_from_job, drv.prepare),
    }
    worker = _build_worker(settings, conn, drivers=drivers)

    summary = asyncio.run(worker.run_once())

    assert summary.attempted == 0
    assert summary.skipped == 1
    assert summary.paused == 1
    # Driver was NEVER called - we skipped before dispatch.
    assert drv.called_for == []
    # Job state unchanged - stays in QUEUED_APPLY for next cycle.
    assert JobRepo(conn).get(job.id).state is JobState.QUEUED_APPLY
    assert any("source-paused skip" in n for n in summary.notes)


def test_apply_worker_other_sources_keep_running_when_one_paused(settings, conn):
    """One dead session must NEVER stall the whole bot. With lever paused and
    greenhouse healthy, the lever job is skipped + the greenhouse job processes
    normally - the spec section 8b 'others keep running' contract."""
    paused_job = _seed_queued(conn, source="lever", source_job_id="paused-2")
    healthy_job = _seed_queued(conn, source="greenhouse", source_job_id="healthy-1")
    mark_auth_required("lever")

    lever_drv = _RecordingDriver()
    gh_drv = _RecordingDriver()
    drivers = {
        "lever": DriverEntry(lever_drv.listing_from_job, lever_drv.prepare),
        "greenhouse": DriverEntry(gh_drv.listing_from_job, gh_drv.prepare),
    }
    worker = _build_worker(settings, conn, drivers=drivers)

    summary = asyncio.run(worker.run_once())

    # Lever job: skipped, no driver call, state unchanged.
    assert lever_drv.called_for == []
    assert JobRepo(conn).get(paused_job.id).state is JobState.QUEUED_APPLY

    # Greenhouse job: processed normally (dry-run -> stays in QUEUED_APPLY but
    # the driver WAS called).
    assert gh_drv.called_for == ["greenhouse"]
    assert summary.attempted == 1
    assert summary.skipped == 1
    assert summary.paused == 1


def test_apply_worker_paused_source_recovers_when_marked_healthy(settings, conn):
    """After ``mark_healthy``, the next run picks up the previously-skipped job."""
    job = _seed_queued(conn, source="lever", source_job_id="recover-1")
    mark_auth_required("lever")

    drv = _RecordingDriver()
    drivers = {"lever": DriverEntry(drv.listing_from_job, drv.prepare)}
    worker = _build_worker(settings, conn, drivers=drivers)

    # Cycle 1: paused, skip.
    summary1 = asyncio.run(worker.run_once())
    assert summary1.paused == 1
    assert drv.called_for == []

    # User re-logs in -> mark healthy.
    mark_healthy("lever")

    # Cycle 2: processes normally.
    summary2 = asyncio.run(worker.run_once())
    assert summary2.attempted == 1
    assert summary2.paused == 0
    assert drv.called_for == ["lever"]
    # Still in QUEUED_APPLY because dry-run by default, but the driver ran.
    assert JobRepo(conn).get(job.id).state is JobState.QUEUED_APPLY
