"""Apply worker (spec §7 #7) — contract tests for the QUEUED_APPLY drain loop.

We fake the *drivers* (one async stub per source) instead of the per-ATS FakePages, so
these tests stay focused on the worker's contract:

  * state-machine translation across all four ``ApplyOutcome.status`` outcomes (APPLIED,
    ASSISTED_PENDING, UNCONFIRMED, FAILED) — and the new APPLYING→REVIEW edge for
    ASSISTED_PENDING (spec §5 docstring);
  * per-job error isolation — one driver crash doesn't kill the run;
  * per-company/day rate limit (spec §7) — silent skip, no state change;
  * unknown-source skip — no state change;
  * Application row written for every real (non-dry-run) attempt;
  * resolver construction works with + without LLM/embed clients;
  * inferred-resolution telemetry mirrors metadata only (no answer value);
  * EEO resolutions never mirror, regardless of source (§8d);
  * pacing sleep is called between *real* applies only (not rate-limit skips);
  * dry-run leaves jobs in QUEUED_APPLY (no APPLYING ping-pong in the event log).

The per-ATS FakePage paths are covered by ``test_apply_driver.py`` (Greenhouse) and
``test_lever_apply.py`` (Lever). Re-testing them here would just couple this file to
driver internals.
"""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime, timezone

import pytest

from av3.config.settings import Settings
from av3.db.repositories import ApplicationRepo, JobRepo
from av3.domain.models import Application, Job, utcnow_iso
from av3.domain.state import ApplicationStatus, ApplyMode, JobState
from av3.pipeline.apply_worker import (
    ApplyRunSummary,
    ApplyWorker,
    DriverEntry,
    _gh_token_from_url,
    _job_to_greenhouse_listing,
    _job_to_lever_listing,
    default_drivers,
)
from av3.resume.answer_resolver import (
    Resolution,
    ResolutionSource,
    SensitiveClass,
)
from av3.resume.factbank import Contact, FactBank
from av3.sources.browser.apply_base import (
    Applicant,
    ApplyOutcome,
    CustomQuestion,
)
from av3.sources.browser.detect import CaptchaResult, CaptchaType


# --------------------------------------------------------------- minimal fakes

class _FakePage:
    """No-op page — the fake drivers ignore it."""


def _make_outcome(
    *,
    status: ApplicationStatus | None,
    mode: ApplyMode = ApplyMode.BROWSER_AUTO,
    submitted: bool | None = None,
    resolutions: list[Resolution] | None = None,
) -> ApplyOutcome:
    """Build a synthetic ApplyOutcome that mimics what a driver would return."""
    submitted = (status is ApplicationStatus.APPLIED) if submitted is None else submitted
    return ApplyOutcome(
        job_url="https://example.test/apply",
        captcha=CaptchaResult(type=CaptchaType.NONE, is_invisible=False),
        mode=mode,
        submitted=submitted,
        status=status,
        resolutions=resolutions or [],
    )


def _fake_driver(outcomes: list[ApplyOutcome] | ApplyOutcome | Exception):
    """Build a DriverEntry whose ``prepare`` returns the queued outcomes (or raises).

    ``outcomes`` may be a single ApplyOutcome (reused), a list (popped in order, error
    if exhausted), or an Exception class/instance to raise on every call.
    """
    if isinstance(outcomes, list):
        queue = list(outcomes)

        async def prepare(*_a, **_kw):
            if not queue:
                raise AssertionError("fake driver exhausted")
            return queue.pop(0)
    elif isinstance(outcomes, BaseException) or (
        isinstance(outcomes, type) and issubclass(outcomes, BaseException)
    ):

        async def prepare(*_a, **_kw):
            raise outcomes if isinstance(outcomes, BaseException) else outcomes("boom")
    else:

        async def prepare(*_a, **_kw):
            return outcomes

    def listing_from_job(job):
        return job  # the fake driver doesn't read it; identity is enough

    return DriverEntry(listing_from_job=listing_from_job, prepare=prepare)


def _seed_queued_lever(conn: sqlite3.Connection, *, company: str, source_job_id: str) -> Job:
    """Insert a job already at QUEUED_APPLY (post optimize+gate). Uses the canonical
    state-machine walk so the row passes referential expectations."""
    repo = JobRepo(conn)
    job = Job(
        source="lever",
        source_job_id=source_job_id,
        title="Senior Data Analyst",
        company=company,
        url=f"https://jobs.lever.co/{company}/{source_job_id}",
    )
    repo.add(job)
    for nxt in (JobState.DESCRIBED, JobState.SCORED, JobState.DECIDED, JobState.QUEUED_APPLY):
        repo.set_state(job.id, nxt)
    return repo.get(job.id)  # type: ignore[return-value]


def _seed_applied_lever(conn: sqlite3.Connection, *, company: str, source_job_id: str) -> Job:
    """Insert a job already at APPLIED (for rate-limit setup)."""
    repo = JobRepo(conn)
    job = Job(
        source="lever",
        source_job_id=source_job_id,
        title="Past Apply",
        company=company,
        url=f"https://jobs.lever.co/{company}/{source_job_id}",
    )
    repo.add(job)
    for nxt in (
        JobState.DESCRIBED, JobState.SCORED, JobState.DECIDED, JobState.QUEUED_APPLY,
        JobState.APPLYING, JobState.APPLIED,
    ):
        repo.set_state(job.id, nxt)
    return repo.get(job.id)  # type: ignore[return-value]


def _bank() -> FactBank:
    return FactBank(
        contact=Contact(
            name="Pat Doe", email="pat@example.com", phone="555-0100", location="Remote",
        ),
        work_authorization="US citizen",
        requires_sponsorship=False,
    )


async def _noop_sleep(_seconds: float) -> None:
    return None


async def _new_page() -> _FakePage:
    return _FakePage()


def _build_worker(
    settings: Settings,
    conn: sqlite3.Connection,
    *,
    driver: DriverEntry,
    dry_run: bool = False,
    sleep=_noop_sleep,
) -> ApplyWorker:
    return ApplyWorker(
        settings=settings,
        conn=conn,
        fact_bank=_bank(),
        resume_path="/tmp/resume.pdf",
        new_page=_new_page,
        applicant=Applicant("Pat", "Doe", "pat@example.com", "555-0100"),
        dry_run=dry_run,
        sleep=sleep,
        drivers={"lever": driver},
    )


# --------------------------------------------------------------- happy path

def test_clean_applied_transitions_job_to_applied_terminal(settings, conn):
    job = _seed_queued_lever(conn, company="acmeco", source_job_id="abc-1")
    worker = _build_worker(
        settings, conn,
        driver=_fake_driver(_make_outcome(status=ApplicationStatus.APPLIED)),
    )

    summary = asyncio.run(worker.run_once())

    assert summary.applied == 1
    assert summary.review == 0
    assert summary.errors == 0
    assert summary.attempted == 1
    assert JobRepo(conn).get(job.id).state is JobState.APPLIED

    apps = ApplicationRepo(conn).list_by_job(job.id)
    assert len(apps) == 1
    assert apps[0].status is ApplicationStatus.APPLIED
    assert apps[0].submitted_at  # populated on a positive submit


# --------------------------------------------------------------- assisted handoff

def test_assisted_pending_transitions_directly_to_review(settings, conn):
    """ASSISTED_PENDING uses the added APPLYING→REVIEW edge (not via FAILED) so the
    event spine doesn't log a deliberate handoff as an error."""
    job = _seed_queued_lever(conn, company="acmeco", source_job_id="abc-2")
    worker = _build_worker(
        settings, conn,
        driver=_fake_driver(_make_outcome(
            status=ApplicationStatus.ASSISTED_PENDING,
            mode=ApplyMode.BROWSER_ASSISTED,
            submitted=False,
        )),
    )

    summary = asyncio.run(worker.run_once())

    assert summary.applied == 0
    assert summary.review == 1
    assert JobRepo(conn).get(job.id).state is JobState.REVIEW

    apps = ApplicationRepo(conn).list_by_job(job.id)
    assert len(apps) == 1
    assert apps[0].status is ApplicationStatus.ASSISTED_PENDING
    assert apps[0].mode is ApplyMode.BROWSER_ASSISTED
    assert apps[0].submitted_at == ""  # no real submit fired


# --------------------------------------------------------------- failure paths

def test_unconfirmed_routes_via_failed_to_review(settings, conn):
    """UNCONFIRMED -> FAILED -> REVIEW per spec §5 wording; dedup keys off APPLIED,
    so an UNCONFIRMED attempt is safely retryable next cycle (handoff §6 gotcha)."""
    job = _seed_queued_lever(conn, company="acmeco", source_job_id="abc-3")
    worker = _build_worker(
        settings, conn,
        driver=_fake_driver(_make_outcome(
            status=ApplicationStatus.UNCONFIRMED, submitted=True,
        )),
    )

    summary = asyncio.run(worker.run_once())

    assert summary.review == 1
    assert JobRepo(conn).get(job.id).state is JobState.REVIEW
    apps = ApplicationRepo(conn).list_by_job(job.id)
    assert apps[0].status is ApplicationStatus.UNCONFIRMED
    assert apps[0].submitted_at  # submit fired, no confirmation


def test_driver_returned_failed_routes_to_review(settings, conn):
    job = _seed_queued_lever(conn, company="acmeco", source_job_id="abc-4")
    worker = _build_worker(
        settings, conn,
        driver=_fake_driver(_make_outcome(
            status=ApplicationStatus.FAILED, submitted=False,
        )),
    )

    summary = asyncio.run(worker.run_once())

    assert summary.review == 1
    assert summary.errors == 0
    assert JobRepo(conn).get(job.id).state is JobState.REVIEW


def test_driver_exception_isolates_one_job_and_routes_to_review(settings, conn):
    """A driver crash on job A must NOT prevent job B from running; A goes to
    FAILED→REVIEW with a FAILED application row recording the attempt."""
    a = _seed_queued_lever(conn, company="acmeco", source_job_id="abc-5a")
    b = _seed_queued_lever(conn, company="acmeco", source_job_id="abc-5b")

    # Driver raises on the FIRST call, then succeeds for the SECOND.
    state = {"calls": 0}

    async def flaky_prepare(*_a, **_kw):
        state["calls"] += 1
        if state["calls"] == 1:
            raise RuntimeError("simulated selector drift")
        return _make_outcome(status=ApplicationStatus.APPLIED)

    driver = DriverEntry(listing_from_job=lambda j: j, prepare=flaky_prepare)
    worker = _build_worker(settings, conn, driver=driver)

    summary = asyncio.run(worker.run_once())

    assert summary.errors == 1
    assert summary.applied == 1
    assert JobRepo(conn).get(a.id).state is JobState.REVIEW
    assert JobRepo(conn).get(b.id).state is JobState.APPLIED

    # The failed job has a FAILED application row carrying the attempt's mode.
    a_apps = ApplicationRepo(conn).list_by_job(a.id)
    assert len(a_apps) == 1
    assert a_apps[0].status is ApplicationStatus.FAILED


# --------------------------------------------------------------- rate limit

def test_per_company_rate_limit_skips_silently(settings, conn):
    """When max_per_company_per_day is hit, the job stays in QUEUED_APPLY (next cycle
    picks it up tomorrow) and no Application row is written."""
    # Seed two already-applied jobs at acmeco today so we're at the default limit (2).
    _seed_applied_lever(conn, company="acmeco", source_job_id="past-1")
    _seed_applied_lever(conn, company="acmeco", source_job_id="past-2")
    queued = _seed_queued_lever(conn, company="acmeco", source_job_id="abc-6")

    # Sanity: confirm the rate-limit query sees both past applies as today's count.
    assert JobRepo(conn).company_applied_count("acmeco") == 2
    assert settings.pacing.max_per_company_per_day == 2

    worker = _build_worker(
        settings, conn,
        driver=_fake_driver(_make_outcome(status=ApplicationStatus.APPLIED)),
    )

    summary = asyncio.run(worker.run_once())

    assert summary.skipped == 1
    assert summary.attempted == 0
    assert summary.applied == 0
    assert JobRepo(conn).get(queued.id).state is JobState.QUEUED_APPLY
    assert ApplicationRepo(conn).list_by_job(queued.id) == []
    assert any("rate-limit" in n for n in summary.notes)


# --------------------------------------------------------------- unknown source

def test_unknown_source_skips_without_state_change(settings, conn):
    repo = JobRepo(conn)
    job = Job(
        source="ashby",  # not in the driver registry
        source_job_id="zzz-1",
        title="x", company="acmeco",
    )
    repo.add(job)
    for nxt in (JobState.DESCRIBED, JobState.SCORED, JobState.DECIDED, JobState.QUEUED_APPLY):
        repo.set_state(job.id, nxt)

    worker = _build_worker(
        settings, conn,
        driver=_fake_driver(_make_outcome(status=ApplicationStatus.APPLIED)),
    )

    summary = asyncio.run(worker.run_once())

    assert summary.skipped == 1
    assert summary.attempted == 0
    assert JobRepo(conn).get(job.id).state is JobState.QUEUED_APPLY
    assert any("ashby" in n for n in summary.notes)


# --------------------------------------------------------------- dry-run

def test_dry_run_leaves_job_in_queued_apply(settings, conn):
    """Dry-run never sets APPLYING -> no ping-pong in the event log."""
    job = _seed_queued_lever(conn, company="acmeco", source_job_id="abc-7")
    worker = _build_worker(
        settings, conn,
        driver=_fake_driver(_make_outcome(status=None)),
        dry_run=True,
    )

    summary = asyncio.run(worker.run_once())

    assert summary.attempted == 1
    assert summary.dry_run_count == 1
    assert summary.applied == 0
    assert summary.review == 0
    assert JobRepo(conn).get(job.id).state is JobState.QUEUED_APPLY
    assert ApplicationRepo(conn).list_by_job(job.id) == []


# --------------------------------------------------------------- pacing

def test_pacing_sleep_called_between_successful_applies(settings, conn):
    """Inter-apply delay applied between *real* applies, NOT before the first one and
    NOT around skipped jobs (those don't burn a delay slot)."""
    _seed_queued_lever(conn, company="acmeco", source_job_id="abc-8a")
    _seed_queued_lever(conn, company="otherco", source_job_id="abc-8b")
    _seed_queued_lever(conn, company="thirdco", source_job_id="abc-8c")

    sleep_calls: list[float] = []

    async def recording_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)

    worker = _build_worker(
        settings, conn,
        driver=_fake_driver(_make_outcome(status=ApplicationStatus.APPLIED)),
        sleep=recording_sleep,
    )

    asyncio.run(worker.run_once())

    # 3 successful applies -> 2 sleeps (between 1↔2 and 2↔3, not before 1).
    assert len(sleep_calls) == 2
    for s in sleep_calls:
        assert settings.pacing.min_delay_s <= s <= settings.pacing.max_delay_s


def test_pacing_does_not_sleep_around_skipped_jobs(settings, conn):
    """Rate-limit skip in the middle of the queue must not burn a delay slot."""
    # acmeco already at limit (2 prior applies).
    _seed_applied_lever(conn, company="acmeco", source_job_id="past-1")
    _seed_applied_lever(conn, company="acmeco", source_job_id="past-2")

    _seed_queued_lever(conn, company="otherco", source_job_id="abc-9a")     # ok
    _seed_queued_lever(conn, company="acmeco",  source_job_id="abc-9b")     # SKIP
    _seed_queued_lever(conn, company="thirdco", source_job_id="abc-9c")     # ok

    sleep_calls: list[float] = []

    async def recording_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)

    worker = _build_worker(
        settings, conn,
        driver=_fake_driver(_make_outcome(status=ApplicationStatus.APPLIED)),
        sleep=recording_sleep,
    )

    asyncio.run(worker.run_once())

    # 2 real applies overall; the skipped middle job means there's only ONE legal
    # delay slot (between otherco's success and thirdco's success — skip in between
    # resets prior_was_apply, so no sleep around it).
    assert len(sleep_calls) == 1


# --------------------------------------------------------------- limit

def test_run_once_respects_limit(settings, conn):
    for sid in ("abc-10a", "abc-10b", "abc-10c"):
        _seed_queued_lever(conn, company=f"co-{sid}", source_job_id=sid)

    worker = _build_worker(
        settings, conn,
        driver=_fake_driver(_make_outcome(status=ApplicationStatus.APPLIED)),
    )

    summary = asyncio.run(worker.run_once(limit=2))

    assert summary.applied == 2
    assert summary.attempted == 2
    remaining = JobRepo(conn).list_by_state(JobState.QUEUED_APPLY)
    assert len(remaining) == 1


# --------------------------------------------------------------- telemetry mirror

def test_inferred_resolution_mirrors_metadata_only(settings, conn, sink):
    """Spec §9 / §8b: mirror question label + category + confidence + outcome.
    The answer value never leaves the box."""
    job = _seed_queued_lever(conn, company="acmeco", source_job_id="abc-11")
    q = CustomQuestion("question_1", "How many years of SQL?", required=False, kind="input")
    inferred = Resolution(
        question=q, value="5 years", source=ResolutionSource.INFERRED,
        confidence=0.82, sensitive=SensitiveClass.NONE,
    )
    bank_hit = Resolution(
        question=CustomQuestion("question_2", "City?", False, "input"),
        value="Remote", source=ResolutionSource.BANK, confidence=1.0,
    )

    worker = _build_worker(
        settings, conn,
        driver=_fake_driver(_make_outcome(
            status=ApplicationStatus.APPLIED,
            resolutions=[inferred, bank_hit],
        )),
    )

    asyncio.run(worker.run_once())

    rows = sink.conn.execute(
        "SELECT * FROM events WHERE stage = 'resolver_inferred'"
    ).fetchall()
    assert len(rows) == 1
    row = rows[0]
    import json
    ctx = json.loads(row["context_json"])
    assert ctx["question"] == "How many years of SQL?"
    assert ctx["category"] == "none"
    assert ctx["confidence"] == 0.82
    assert ctx["outcome"] == "answered"
    # CRITICAL: the answer value MUST NOT be in the mirrored payload.
    assert "5 years" not in row["context_json"]
    assert "5 years" not in (row["error_msg"] or "")


def test_eeo_resolutions_never_mirror(settings, conn, sink):
    """Spec §8d: EEO self-ID values stay local — including the metadata row."""
    _seed_queued_lever(conn, company="acmeco", source_job_id="abc-12")
    eeo_inferred = Resolution(
        question=CustomQuestion("eeo_gender", "Gender", False, "select"),
        value="Prefer not to answer",
        source=ResolutionSource.INFERRED,  # even an inferred EEO must not mirror
        confidence=0.99,
        sensitive=SensitiveClass.EEO,
    )

    worker = _build_worker(
        settings, conn,
        driver=_fake_driver(_make_outcome(
            status=ApplicationStatus.APPLIED, resolutions=[eeo_inferred],
        )),
    )

    asyncio.run(worker.run_once())

    rows = sink.conn.execute(
        "SELECT * FROM events WHERE stage = 'resolver_inferred'"
    ).fetchall()
    assert rows == []


def test_bank_resolutions_do_not_emit_iteration_events(settings, conn, sink):
    """Only INFERRED resolutions feed the §8e feedback loop. Bank hits are already
    canonical and shouldn't pollute the iteration signal."""
    _seed_queued_lever(conn, company="acmeco", source_job_id="abc-13")
    bank_hit = Resolution(
        question=CustomQuestion("q", "City?", False, "input"),
        value="Remote", source=ResolutionSource.BANK, confidence=0.9,
    )

    worker = _build_worker(
        settings, conn,
        driver=_fake_driver(_make_outcome(
            status=ApplicationStatus.APPLIED, resolutions=[bank_hit],
        )),
    )

    asyncio.run(worker.run_once())

    rows = sink.conn.execute(
        "SELECT * FROM events WHERE stage = 'resolver_inferred'"
    ).fetchall()
    assert rows == []


# --------------------------------------------------------------- resolver wiring

def test_resolver_constructed_without_llm_or_embed_clients(settings, conn):
    """Both clients are optional. With neither, the resolver still falls back to exact
    text matches in the bank + sensitive-field policy."""
    worker = ApplyWorker(
        settings=settings, conn=conn, fact_bank=_bank(),
        resume_path="/tmp/r.pdf", new_page=_new_page,
        embed_client=None, llm_client=None,  # both omitted
        dry_run=True, sleep=_noop_sleep,
        drivers={"lever": _fake_driver(_make_outcome(status=None))},
    )
    assert worker._resolver is not None
    assert worker._resolver.embed_client is None
    assert worker._resolver.llm_client is None


def test_resolver_threads_explicit_salary_expectation(settings, conn):
    worker = ApplyWorker(
        settings=settings, conn=conn, fact_bank=_bank(),
        resume_path="/tmp/r.pdf", new_page=_new_page,
        salary_expectation="$140,000",
        dry_run=True, sleep=_noop_sleep,
        drivers={"lever": _fake_driver(_make_outcome(status=None))},
    )
    assert worker._resolver.salary_expectation == "$140,000"


def test_applicant_built_from_fact_bank_when_not_provided(settings, conn):
    worker = ApplyWorker(
        settings=settings, conn=conn,
        fact_bank=FactBank(contact=Contact(name="Sam Lee", email="sam@x.io", phone="555")),
        resume_path="/tmp/r.pdf", new_page=_new_page,
        dry_run=True, sleep=_noop_sleep,
        drivers={"lever": _fake_driver(_make_outcome(status=None))},
    )
    assert worker._applicant.first_name == "Sam"
    assert worker._applicant.last_name == "Lee"
    assert worker._applicant.email == "sam@x.io"


# --------------------------------------------------------------- helpers

def test_gh_token_extracted_from_canonical_url():
    url = "https://job-boards.greenhouse.io/acmeco/jobs/12345"
    assert _gh_token_from_url(url) == "acmeco"


def test_gh_token_empty_on_non_greenhouse_url():
    assert _gh_token_from_url("https://jobs.lever.co/x/y") == ""
    assert _gh_token_from_url("") == ""


def test_job_to_lever_listing_appends_apply_path():
    job = Job(
        source="lever", source_job_id="uuid-1", title="X", company="acmeco",
        url="https://jobs.lever.co/acmeco/uuid-1",
    )
    listing = _job_to_lever_listing(job)
    assert listing.apply_url == "https://jobs.lever.co/acmeco/uuid-1/apply"
    assert listing.source_job_id == "uuid-1"


def test_job_to_greenhouse_listing_recovers_board_token():
    job = Job(
        source="greenhouse", source_job_id="42", title="X", company="Acme",
        url="https://job-boards.greenhouse.io/acmeco/jobs/42",
    )
    listing = _job_to_greenhouse_listing(job)
    assert listing.board_token == "acmeco"


def test_default_drivers_registers_both_atses():
    drivers = default_drivers()
    assert set(drivers.keys()) == {"lever", "greenhouse"}
