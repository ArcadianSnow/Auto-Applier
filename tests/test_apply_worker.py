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

from auto_applier.config.settings import Settings
from auto_applier.db.repositories import ApplicationRepo, JobRepo
from auto_applier.domain.models import Application, Job, utcnow_iso
from auto_applier.domain.state import ApplicationStatus, ApplyMode, JobState
from auto_applier.pipeline.apply_worker import (
    ApplyRunSummary,
    ApplyWorker,
    DriverEntry,
    _gh_token_from_url,
    _job_to_greenhouse_listing,
    _job_to_lever_listing,
    default_drivers,
)
from auto_applier.resume.answer_resolver import (
    Resolution,
    ResolutionSource,
    SensitiveClass,
)
from auto_applier.config.settings import PacingConfig, StrategyConfig
from auto_applier.config.strategy import RiskBias, StrategyProfile
from auto_applier.resume.factbank import Contact, FactBank
from auto_applier.resume.generate import (
    generated_cover_letter_path,
    generated_resume_path,
    resolve_generated_cover_letter,
    resolve_generated_resume,
)
from auto_applier.resume.proposed import load_proposed, proposed_path
from auto_applier.pipeline.review_batch import ReviewBatch
from auto_applier.sources.browser.apply_base import (
    Applicant,
    ApplyOutcome,
    CustomQuestion,
)
from auto_applier.sources.browser.detect import CaptchaResult, CaptchaType


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
    review_batch=None,
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
        review_batch=review_batch,
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


def test_proposed_application_artifact_is_persisted(settings, conn):
    """Batched assisted review, Phase 1 (prep-complete): every prepared job persists its COMPLETE
    proposed application as a per-job JSON artifact (the 'In Progress' page reads it). Submit
    behavior is unchanged — this is an additive, local-only artifact written for dry-run + real."""
    job = _seed_queued_lever(conn, company="acmeco", source_job_id="prop-1")
    q = CustomQuestion(
        field_id="cq1", label="Why do you want to work here?", required=True, kind="textarea",
    )
    outcome = _make_outcome(status=None, resolutions=[
        Resolution(question=q, value=None, source=ResolutionSource.REVIEW, needs_review=True),
    ])
    outcome.custom_questions = [q]
    worker = _build_worker(settings, conn, driver=_fake_driver(outcome), dry_run=True)

    asyncio.run(worker.run_once())

    assert proposed_path(settings, job.id).exists()
    proposed = load_proposed(settings, job.id)
    assert proposed is not None
    keys = {f.key for f in proposed.fields}
    assert {"applicant:email", "doc:resume"} <= keys
    field = next(f for f in proposed.fields if f.key == "q:cq1")
    assert field.label == "Why do you want to work here?"
    # No LLM client on this worker → the essay gap is NOT drafted; it stays a needs-input row.
    assert field.needs_verify is True
    assert field.is_draft is False


def test_dry_run_second_cycle_skips_already_tested_job(settings, conn):
    """Dry-run jobs stay in QUEUED_APPLY; the worker must not re-test them every cycle
    (the infinite dry-run re-apply loop)."""
    job = _seed_queued_lever(conn, company="acmeco", source_job_id="abc-8")
    worker = _build_worker(
        settings, conn,
        driver=_fake_driver(_make_outcome(status=None)),
        dry_run=True,
    )

    first = asyncio.run(worker.run_once())
    assert first.attempted == 1 and first.dry_run_count == 1

    second = asyncio.run(worker.run_once())
    assert second.attempted == 0          # NOT re-tested
    assert second.skipped >= 1
    assert JobRepo(conn).get(job.id).state is JobState.QUEUED_APPLY


# --------------------------------------------------------------- batch barrier (Phase 2)

def test_batch_barrier_holds_after_n_prepared(settings, conn):
    """With a review batch of size N, the worker prepares N jobs then defers the rest, and the
    barrier holds (so the scheduler's apply_gate pauses the apply stage until the owner releases)."""
    for i in range(3):
        _seed_queued_lever(conn, company="acmeco", source_job_id=f"batch-{i}")
    batch = ReviewBatch(size=2)
    worker = _build_worker(
        settings, conn,
        driver=_fake_driver(_make_outcome(status=None)),
        dry_run=True,
        review_batch=batch,
    )

    summary = asyncio.run(worker.run_once())

    assert summary.attempted == 2         # exactly N prepared
    assert summary.dry_run_count == 2
    assert summary.deferred_batch == 1    # the 3rd deferred
    assert batch.count == 2
    assert batch.is_holding() is True


def test_batch_barrier_resumes_after_release(settings, conn):
    """Releasing the batch lifts the hold; the next run prepares the previously-deferred job."""
    for i in range(3):
        _seed_queued_lever(conn, company="acmeco", source_job_id=f"rel-{i}")
    batch = ReviewBatch(size=2)
    worker = _build_worker(
        settings, conn,
        driver=_fake_driver(_make_outcome(status=None)),
        dry_run=True,
        review_batch=batch,
    )

    first = asyncio.run(worker.run_once())
    assert first.attempted == 2 and batch.is_holding() is True

    batch.release()
    assert batch.is_holding() is False

    second = asyncio.run(worker.run_once())
    assert second.attempted == 1          # the one deferred job — the first two are already tested
    assert second.skipped == 2            # the two already-prepared dry-run jobs
    assert batch.count == 1
    assert batch.is_holding() is False


def test_no_review_batch_drains_all(settings, conn):
    """Default (no batch) keeps today's behavior — apply drains every queued job, no deferral."""
    for i in range(3):
        _seed_queued_lever(conn, company="acmeco", source_job_id=f"nob-{i}")
    worker = _build_worker(
        settings, conn,
        driver=_fake_driver(_make_outcome(status=None)),
        dry_run=True,
    )

    summary = asyncio.run(worker.run_once())

    assert summary.attempted == 3
    assert summary.deferred_batch == 0


def test_batch_auto_advances_when_all_dispositioned(settings, conn):
    """Once the owner has dispositioned every job in a held batch, the next run releases the spent
    batch and prepares the next N (batched assisted review, Phase 4 auto-advance)."""
    for i in range(4):
        _seed_queued_lever(conn, company="acmeco", source_job_id=f"adv-{i}")
    batch = ReviewBatch(size=2)
    worker = _build_worker(
        settings, conn,
        driver=_fake_driver(_make_outcome(status=None)),
        dry_run=True,
        review_batch=batch,
    )

    first = asyncio.run(worker.run_once())
    assert first.attempted == 2 and batch.is_holding() is True
    members = list(batch.snapshot()["members"])

    # Owner dispositions both prepared jobs → the hold lifts.
    batch.dispose(members[0], "applied")
    batch.dispose(members[1], "skipped")
    assert batch.is_holding() is False
    assert batch.all_dispositioned() is True

    # Next run auto-advances: releases the spent batch, prepares the remaining 2 into a fresh one.
    second = asyncio.run(worker.run_once())
    assert second.attempted == 2
    assert batch.count == 2
    assert set(batch.snapshot()["members"]).isdisjoint(members)  # a genuinely fresh batch


def test_batch_barrier_self_defers_when_already_holding(settings, conn):
    """A worker that finds the batch already full (e.g. ``av3 apply --once`` bypassing the scheduler
    gate) defers immediately without preparing anything."""
    for i in range(2):
        _seed_queued_lever(conn, company="acmeco", source_job_id=f"held-{i}")
    batch = ReviewBatch(size=1)
    batch.add("some-other-job")           # batch already full from a prior cycle
    assert batch.is_holding() is True
    worker = _build_worker(
        settings, conn,
        driver=_fake_driver(_make_outcome(status=None)),
        dry_run=True,
        review_batch=batch,
    )

    summary = asyncio.run(worker.run_once())

    assert summary.attempted == 0
    assert summary.deferred_batch == 2


def test_salary_ask_falls_back_to_targeting_floor(settings, conn):
    """When salary.floor is unset, the per-job salary ask falls back to targeting.salary_floor
    (onboarding writes the latter, not the former)."""
    settings.salary.floor = None
    settings.targeting.salary_floor = 82000
    job = _seed_queued_lever(conn, company="acmeco", source_job_id="sal-1")
    worker = _build_worker(
        settings, conn,
        driver=_fake_driver(_make_outcome(status=None)),
        dry_run=True,
    )
    worker._apply_salary_ask(JobRepo(conn).get(job.id))
    assert worker._resolver.salary_expectation          # non-empty (was "" before the fix)
    assert "82" in worker._resolver.salary_expectation


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


# --------------------------------------------------------------- per-job artifacts (Phase 6 1/M)

def _recording_resume_driver(seen: dict, *, status=ApplicationStatus.APPLIED) -> DriverEntry:
    """A fake driver that records the résumé path it was handed (4th positional arg of
    ``prepare`` — ``page, listing, applicant, resume_path``)."""

    async def prepare(*a, **_kw):
        seen["resume_path"] = a[3]
        return _make_outcome(status=status)

    return DriverEntry(listing_from_job=lambda j: j, prepare=prepare)


def test_uses_per_job_generated_resume_when_present(settings, conn):
    """When the optimize worker has written a per-job résumé PDF (keyed by job.id), the
    apply worker uploads THAT — not the global fallback — and records it on the row."""
    job = _seed_queued_lever(conn, company="acmeco", source_job_id="art-1")

    # Simulate the optimize worker's output: a per-job résumé keyed by job.id.
    pdf = generated_resume_path(settings, job.id)
    pdf.parent.mkdir(parents=True, exist_ok=True)
    pdf.write_bytes(b"%PDF per-job\n")

    seen: dict = {}
    worker = _build_worker(settings, conn, driver=_recording_resume_driver(seen))

    asyncio.run(worker.run_once())

    assert seen["resume_path"] == str(pdf)
    apps = ApplicationRepo(conn).list_by_job(job.id)
    assert apps[0].generated_resume_path == str(pdf)


def test_falls_back_to_global_resume_when_no_per_job_pdf(settings, conn):
    """No per-job PDF on disk → the worker hands the driver the single global
    ``resume.pdf`` it was constructed with, and records that on the row."""
    job = _seed_queued_lever(conn, company="acmeco", source_job_id="art-2")

    seen: dict = {}
    worker = _build_worker(settings, conn, driver=_recording_resume_driver(seen))

    asyncio.run(worker.run_once())

    assert seen["resume_path"] == "/tmp/resume.pdf"  # the _build_worker fallback
    apps = ApplicationRepo(conn).list_by_job(job.id)
    assert apps[0].generated_resume_path == "/tmp/resume.pdf"
    assert apps[0].cover_letter_path == ""  # no per-job cover letter generated


def test_records_per_job_cover_letter_path_when_present(settings, conn):
    """The per-job cover letter (.txt) the optimize worker wrote is recorded on the
    Application row alongside the per-job résumé."""
    job = _seed_queued_lever(conn, company="acmeco", source_job_id="art-3")

    pdf = generated_resume_path(settings, job.id)
    pdf.parent.mkdir(parents=True, exist_ok=True)
    pdf.write_bytes(b"%PDF\n")
    cover = generated_cover_letter_path(settings, job.id)
    cover.write_text("Dear hiring team, ...", encoding="utf-8")

    worker = _build_worker(
        settings, conn,
        driver=_fake_driver(_make_outcome(status=ApplicationStatus.APPLIED)),
    )

    asyncio.run(worker.run_once())

    apps = ApplicationRepo(conn).list_by_job(job.id)
    assert apps[0].generated_resume_path == str(pdf)
    assert apps[0].cover_letter_path == str(cover)


def test_picks_up_legacy_bare_named_artifacts(settings, conn):
    """Regression (2026-06-21 readable-filename change): an artifact written BEFORE the
    rename lives on disk as the bare ``{job_id}.pdf`` / ``{job_id}_cover.txt``. The readable
    path no longer points at it, so the worker must fall back to the legacy name instead of
    orphaning it to the (often-absent) global résumé."""
    job = _seed_queued_lever(conn, company="acmeco", source_job_id="legacy-1")

    generated_dir = settings.artifacts_dir / "generated"
    generated_dir.mkdir(parents=True, exist_ok=True)
    legacy_pdf = generated_dir / f"{job.id}.pdf"
    legacy_pdf.write_bytes(b"%PDF legacy\n")
    legacy_cover = generated_dir / f"{job.id}_cover.txt"
    legacy_cover.write_text("Dear team (legacy)", encoding="utf-8")

    # Sanity: the readable path does NOT match the on-disk legacy file (the bug's precondition).
    assert generated_resume_path(settings, job.id) != legacy_pdf

    seen: dict = {}
    worker = _build_worker(settings, conn, driver=_recording_resume_driver(seen))
    asyncio.run(worker.run_once())

    assert seen["resume_path"] == str(legacy_pdf)  # legacy file used, NOT "/tmp/resume.pdf"
    apps = ApplicationRepo(conn).list_by_job(job.id)
    assert apps[0].generated_resume_path == str(legacy_pdf)
    assert apps[0].cover_letter_path == str(legacy_cover)


def test_resolve_generated_prefers_readable_then_legacy_then_id8(settings, conn):
    """Resolution order across the three on-disk layouts: current per-job-folder clean name →
    legacy flat bare ``{job_id}.pdf`` → legacy flat readable ``..._{id8}.pdf``."""
    job = _seed_queued_lever(conn, company="acmeco", source_job_id="resolve-1")
    jid = job.id
    generated_dir = settings.artifacts_dir / "generated"
    generated_dir.mkdir(parents=True, exist_ok=True)
    current = generated_resume_path(settings, jid)        # generated/<jid>/Resume.pdf
    legacy = generated_dir / f"{jid}.pdf"                  # legacy flat bare
    assert current.parent == generated_dir / jid          # the per-job folder is the unique key

    # 1. Nothing on disk → None.
    assert resolve_generated_resume(settings, jid) is None
    assert resolve_generated_cover_letter(settings, jid) is None

    # 2. A legacy flat readable file (same id8 suffix) is found via the flat id-glob.
    drifted = generated_dir / f"Old_Name_Resume_Acme_Eng_{jid[:8]}.pdf"
    drifted.write_bytes(b"%PDF drift\n")
    assert resolve_generated_resume(settings, jid) == drifted

    # 3. The legacy flat bare name is an exact match → wins over the ambiguous flat glob.
    legacy.write_bytes(b"%PDF legacy\n")
    assert resolve_generated_resume(settings, jid) == legacy

    # 4. The current per-job-folder path wins over everything when present.
    current.parent.mkdir(parents=True, exist_ok=True)
    current.write_bytes(b"%PDF current\n")
    assert resolve_generated_resume(settings, jid) == current


def _recording_cover_driver(seen: dict, *, status=ApplicationStatus.APPLIED) -> DriverEntry:
    """A fake driver that records the cover_letter_path kwarg it was handed."""

    async def prepare(*_a, **kw):
        seen["cover_letter_path"] = kw.get("cover_letter_path")
        return _make_outcome(status=status)

    return DriverEntry(listing_from_job=lambda j: j, prepare=prepare)


def test_passes_per_job_cover_letter_to_driver(settings, conn):
    """The optimize-generated cover (.txt) is handed to the driver for upload, not just
    recorded — BUILD 1 wired cover_letter_path through driver.prepare."""
    job = _seed_queued_lever(conn, company="acmeco", source_job_id="cov-1")
    cover = generated_cover_letter_path(settings, job.id)
    cover.parent.mkdir(parents=True, exist_ok=True)
    cover.write_text("Dear team", encoding="utf-8")

    seen: dict = {}
    worker = _build_worker(settings, conn, driver=_recording_cover_driver(seen))
    asyncio.run(worker.run_once())

    assert seen["cover_letter_path"] == str(cover)


def test_manual_per_job_cover_uploaded_then_archived_on_applied(settings, conn, tmp_path):
    """Per-job manual cover (av3 cover): the worker uploads the generic-named file from the
    job folder, and on a confirmed APPLIED moves it to uploads/_archive and records THAT path
    on the row (BUILD 1.1 lifecycle)."""
    from auto_applier.resume.generate import (
        assign_cover_letter, existing_job_cover, job_cover_upload_path,
    )

    job = _seed_queued_lever(conn, company="Tailscale", source_job_id="cov-2")
    src = tmp_path / "CoverLetter_Tailscale_SE_Commercial.docx"
    src.write_text("Dear Tailscale", encoding="utf-8")
    upload_path = assign_cover_letter(settings, job.id, src)
    assert upload_path == job_cover_upload_path(settings, job.id, ".docx")

    seen: dict = {}
    worker = _build_worker(settings, conn, driver=_recording_cover_driver(seen))
    asyncio.run(worker.run_once())

    # Driver was handed the live (generic-named) upload path...
    assert seen["cover_letter_path"] == str(upload_path)
    # ...and after APPLIED the file is archived (moved) with the job id appended.
    assert existing_job_cover(settings, job.id) is None
    archived = settings.uploads_dir / "_archive" / f"Cover Letter - {job.id}.docx"
    assert archived.exists()
    apps = ApplicationRepo(conn).list_by_job(job.id)
    assert apps[0].cover_letter_path == str(archived)


def test_manual_cover_not_archived_when_not_applied(settings, conn, tmp_path):
    """A non-APPLIED outcome (assisted) leaves the cover in the job folder — not yet
    confirmed used — and records the upload path."""
    from auto_applier.resume.generate import assign_cover_letter, existing_job_cover

    job = _seed_queued_lever(conn, company="Tailscale", source_job_id="cov-2b")
    src = tmp_path / "letter.docx"
    src.write_text("Dear Tailscale", encoding="utf-8")
    upload_path = assign_cover_letter(settings, job.id, src)

    seen: dict = {}
    worker = _build_worker(
        settings, conn,
        driver=_recording_cover_driver(seen, status=ApplicationStatus.ASSISTED_PENDING),
    )
    asyncio.run(worker.run_once())

    assert seen["cover_letter_path"] == str(upload_path)
    assert existing_job_cover(settings, job.id) == upload_path  # still there
    assert not (settings.uploads_dir / "_archive").exists()
    apps = ApplicationRepo(conn).list_by_job(job.id)
    assert apps[0].cover_letter_path == str(upload_path)


def test_no_cover_letter_anywhere_passes_empty(settings, conn):
    """No manual cover and no optimize cover → driver gets '' (no attach)."""
    job = _seed_queued_lever(conn, company="Nowhere", source_job_id="cov-3")

    seen: dict = {}
    worker = _build_worker(settings, conn, driver=_recording_cover_driver(seen))
    asyncio.run(worker.run_once())

    assert seen["cover_letter_path"] == ""
    apps = ApplicationRepo(conn).list_by_job(job.id)
    assert apps[0].cover_letter_path == ""


def test_manual_per_job_resume_uploaded_then_archived_on_applied(settings, conn, tmp_path):
    """A manually-assigned résumé (av3 resume) is uploaded from the generic-named job folder
    and, on a confirmed APPLIED, archived with the job id appended (BUILD 1.2 — mirrors cover)."""
    from auto_applier.resume.generate import (
        assign_resume, existing_job_resume, job_resume_upload_path,
    )

    job = _seed_queued_lever(conn, company="Tailscale", source_job_id="res-1")
    src = tmp_path / "Joseph_Lira_Resume_Solutions_Engineer.docx"
    src.write_text("RESUME", encoding="utf-8")
    upload_path = assign_resume(settings, job.id, src)
    assert upload_path == job_resume_upload_path(settings, job.id, ".docx")

    seen: dict = {}
    worker = _build_worker(settings, conn, driver=_recording_resume_driver(seen))
    asyncio.run(worker.run_once())

    assert seen["resume_path"] == str(upload_path)  # uploaded the generic-named file
    assert existing_job_resume(settings, job.id) is None  # moved
    archived = settings.uploads_dir / "_archive" / f"Resume - {job.id}.docx"
    assert archived.exists()
    apps = ApplicationRepo(conn).list_by_job(job.id)
    assert apps[0].generated_resume_path == str(archived)


def test_manual_resume_takes_precedence_over_optimize_pdf(settings, conn, tmp_path):
    """When BOTH a manual résumé and an optimize-generated PDF exist, the manual one wins."""
    from auto_applier.resume.generate import assign_resume

    job = _seed_queued_lever(conn, company="acmeco", source_job_id="res-2")
    pdf = generated_resume_path(settings, job.id)
    pdf.parent.mkdir(parents=True, exist_ok=True)
    pdf.write_bytes(b"%PDF optimize\n")
    src = tmp_path / "hand.pdf"
    src.write_text("hand-crafted", encoding="utf-8")
    upload_path = assign_resume(settings, job.id, src)

    seen: dict = {}
    worker = _build_worker(settings, conn, driver=_recording_resume_driver(seen))
    asyncio.run(worker.run_once())

    assert seen["resume_path"] == str(upload_path)  # NOT the optimize pdf


def test_failed_recovery_records_per_job_resume_path(settings, conn):
    """A driver crash routes the job to REVIEW with a FAILED Application row carrying the
    per-job résumé path, so dashboard triage shows which résumé it would have used."""
    job = _seed_queued_lever(conn, company="acmeco", source_job_id="art-4")
    pdf = generated_resume_path(settings, job.id)
    pdf.parent.mkdir(parents=True, exist_ok=True)
    pdf.write_bytes(b"%PDF\n")

    worker = _build_worker(settings, conn, driver=_fake_driver(RuntimeError))

    summary = asyncio.run(worker.run_once())

    assert summary.errors == 1
    assert JobRepo(conn).get(job.id).state is JobState.REVIEW
    apps = ApplicationRepo(conn).list_by_job(job.id)
    assert apps[0].status is ApplicationStatus.FAILED
    assert apps[0].generated_resume_path == str(pdf)


# --------------------------------------------------------------- salary intelligence (Phase 6 3/M)

def _job_with_comp(conn, *, company, source_job_id, compensation):
    """A QUEUED_APPLY lever job carrying a posted-comp string."""
    repo = JobRepo(conn)
    job = Job(
        source="lever", source_job_id=source_job_id, title="Senior Data Analyst",
        company=company, url=f"https://jobs.lever.co/{company}/{source_job_id}",
        compensation=compensation,
    )
    repo.add(job)
    for nxt in (JobState.DESCRIBED, JobState.SCORED, JobState.DECIDED, JobState.QUEUED_APPLY):
        repo.set_state(job.id, nxt)
    return repo.get(job.id)


def _salary_settings(settings, **kw):
    from auto_applier.config.settings import SalaryConfig
    return settings.model_copy(update={"salary": SalaryConfig(**kw)})


def test_salary_ask_anchors_to_posted_range(settings, conn):
    """With a posted range, the per-job ask is the upper-middle of that band, set on the
    resolver before the job's questions resolve."""
    job = _job_with_comp(conn, company="acmeco", source_job_id="sal-1",
                         compensation="$120,000 - $160,000")
    worker = _build_worker(
        _salary_settings(settings, floor=100_000), conn,
        driver=_fake_driver(_make_outcome(status=None)), dry_run=True,
    )
    worker._apply_salary_ask(job)
    assert worker._resolver.salary_expectation == "$150,000"  # 120k + 3/4*40k


def test_salary_ask_falls_back_to_user_ceiling_without_posted(settings, conn):
    job = _job_with_comp(conn, company="acmeco", source_job_id="sal-2", compensation="")
    worker = _build_worker(
        _salary_settings(settings, floor=100_000, ceiling=130_000), conn,
        driver=_fake_driver(_make_outcome(status=None)), dry_run=True,
    )
    worker._apply_salary_ask(job)
    assert worker._resolver.salary_expectation == "$130,000"


def test_salary_ask_empty_when_no_config_and_no_posted(settings, conn):
    """No salary config + no posted comp → no ask → resolver bails salary Qs to REVIEW."""
    job = _job_with_comp(conn, company="acmeco", source_job_id="sal-3", compensation="")
    worker = _build_worker(
        settings, conn,
        driver=_fake_driver(_make_outcome(status=None)), dry_run=True,
    )
    worker._apply_salary_ask(job)
    assert worker._resolver.salary_expectation == ""


# --------------------------------------------------------------- fact-bank hot-reload (live 2026-06-21)

def test_refreshes_fact_bank_when_master_json_changes(settings, conn):
    """A profile edit saved AFTER the worker started (e.g. the 'More details' wizard adding
    nationality/notice) is hot-reloaded on the next run — the resolver must not keep serving
    the stale in-memory bank (live bug: saved extras filled blank until restart)."""
    from auto_applier.web.onboarding import save_fact_bank

    # Built with no master.json on disk yet → records mtime=None, bank has no extras.
    worker = _build_worker(settings, conn,
                           driver=_fake_driver(_make_outcome(status=ApplicationStatus.APPLIED)))
    assert worker._resolver.fact_bank.primary_nationality == ""

    # The user fills "More details" → master.json now exists with the extras.
    bank = _bank()
    bank.primary_nationality = "Brazil"
    bank.notice_period = "Two weeks"
    save_fact_bank(settings.data_dir, bank)

    worker._refresh_fact_bank()
    assert worker._resolver.fact_bank.primary_nationality == "Brazil"
    assert worker._resolver.fact_bank.notice_period == "Two weeks"
    assert worker._fact_bank.primary_nationality == "Brazil"


def test_refresh_fact_bank_keeps_current_bank_on_unreadable_file(settings, conn):
    """A half-written / malformed master.json (mid-save) must NEVER break the apply loop —
    the worker keeps its current bank and retries next cycle."""
    from auto_applier.web.onboarding import save_fact_bank

    # Build first (no master.json → mtime=None), then save + reload a good bank.
    worker = _build_worker(settings, conn,
                           driver=_fake_driver(_make_outcome(status=ApplicationStatus.APPLIED)))
    bank = _bank()
    bank.primary_nationality = "Canada"
    save_fact_bank(settings.data_dir, bank)
    worker._refresh_fact_bank()
    assert worker._resolver.fact_bank.primary_nationality == "Canada"

    # Now corrupt the file and force a reload attempt — it must keep the good bank, not crash.
    master = settings.data_dir / "profile" / "master.json"
    master.write_text("{ this is not valid json", encoding="utf-8")
    worker._fact_bank_mtime = None  # force the reload attempt
    worker._refresh_fact_bank()
    assert worker._resolver.fact_bank.primary_nationality == "Canada"  # unchanged, no crash


# --------------------------------------------------------------- strategy profiles (Phase 6 2/M)

def _mode_recording_driver(seen: dict) -> DriverEntry:
    """A fake driver that records the apply ``mode`` kwarg it was handed."""

    async def prepare(*_a, **kw):
        seen["mode"] = kw.get("mode")
        return _make_outcome(status=ApplicationStatus.APPLIED, mode=kw.get("mode"))

    return DriverEntry(listing_from_job=lambda j: j, prepare=prepare)


def _with_profile(settings: Settings, profile: StrategyProfile) -> Settings:
    return settings.model_copy(update={"strategy": StrategyConfig(profile=profile)})


def test_cautious_profile_forces_assisted_mode(settings, conn):
    """risk_bias=LEANS_ASSISTED (Cautious) starts every job assisted even when the worker
    was constructed with BROWSER_AUTO (spec §8a starting posture — not the safety floor)."""
    _seed_queued_lever(conn, company="acmeco", source_job_id="strat-1")
    seen: dict = {}
    worker = ApplyWorker(
        settings=_with_profile(settings, StrategyProfile.CAUTIOUS),
        conn=conn, fact_bank=_bank(), resume_path="/tmp/r.pdf", new_page=_new_page,
        applicant=Applicant("Pat", "Doe", "pat@example.com", "555"),
        mode=ApplyMode.BROWSER_AUTO, dry_run=False, sleep=_noop_sleep,
        drivers={"lever": _mode_recording_driver(seen)},
    )

    asyncio.run(worker.run_once())

    assert seen["mode"] is ApplyMode.BROWSER_ASSISTED


def test_balanced_profile_honours_requested_auto_mode(settings, conn):
    _seed_queued_lever(conn, company="acmeco", source_job_id="strat-2")
    seen: dict = {}
    worker = ApplyWorker(
        settings=_with_profile(settings, StrategyProfile.BALANCED),
        conn=conn, fact_bank=_bank(), resume_path="/tmp/r.pdf", new_page=_new_page,
        applicant=Applicant("Pat", "Doe", "pat@example.com", "555"),
        mode=ApplyMode.BROWSER_AUTO, dry_run=False, sleep=_noop_sleep,
        drivers={"lever": _mode_recording_driver(seen)},
    )

    asyncio.run(worker.run_once())

    assert seen["mode"] is ApplyMode.BROWSER_AUTO


def test_soft_daily_target_defers_remaining_jobs(settings, conn):
    """Once the day's APPLIED count reaches the profile's daily_target, the worker stops
    INITIATING new applies this run and leaves the rest in QUEUED_APPLY (soft — no error,
    no state change). Custom profile with daily_target=2 over 3 queued jobs."""
    for sid in ("dt-1", "dt-2", "dt-3"):
        _seed_queued_lever(conn, company=f"co-{sid}", source_job_id=sid)

    custom = settings.model_copy(update={
        "strategy": StrategyConfig(profile=StrategyProfile.CUSTOM),
        "pacing": PacingConfig(min_delay_s=0.0, max_delay_s=0.0, daily_target=2,
                               max_per_company_per_day=2, risk_bias=RiskBias.BALANCED),
    })
    worker = _build_worker(custom, conn,
                           driver=_fake_driver(_make_outcome(status=ApplicationStatus.APPLIED)))

    summary = asyncio.run(worker.run_once())

    assert summary.applied == 2
    assert summary.deferred_daily_target == 1
    # The deferred job is untouched — still QUEUED_APPLY for next day.
    assert len(JobRepo(conn).list_by_state(JobState.QUEUED_APPLY)) == 1
    assert any("daily target reached" in n for n in summary.notes)


def test_dry_run_ignores_daily_target(settings, conn):
    """Dry runs produce no APPLIED rows, so the daily target never trips — a dev dry-run
    must drain the whole queue regardless of target."""
    for sid in ("dt-4", "dt-5", "dt-6"):
        _seed_queued_lever(conn, company=f"co-{sid}", source_job_id=sid)

    custom = settings.model_copy(update={
        "strategy": StrategyConfig(profile=StrategyProfile.CUSTOM),
        "pacing": PacingConfig(min_delay_s=0.0, max_delay_s=0.0, daily_target=1,
                               max_per_company_per_day=2, risk_bias=RiskBias.BALANCED),
    })
    worker = _build_worker(custom, conn,
                           driver=_fake_driver(_make_outcome(status=None)), dry_run=True)

    summary = asyncio.run(worker.run_once())

    assert summary.attempted == 3
    assert summary.deferred_daily_target == 0


def test_aggressive_profile_widens_per_company_cap(settings, conn):
    """Aggressive's per-company cap (3) lets a 3rd same-company apply through where the
    Balanced cap (2) would skip it."""
    _seed_applied_lever(conn, company="acmeco", source_job_id="agg-past-1")
    _seed_applied_lever(conn, company="acmeco", source_job_id="agg-past-2")
    _seed_queued_lever(conn, company="acmeco", source_job_id="agg-3")

    worker = _build_worker(
        _with_profile(settings, StrategyProfile.AGGRESSIVE), conn,
        driver=_fake_driver(_make_outcome(status=ApplicationStatus.APPLIED)),
    )

    summary = asyncio.run(worker.run_once())

    assert summary.applied == 1  # 3rd apply allowed under Aggressive (cap 3)
    assert summary.skipped == 0


# --------------------------------------------------------------- session rotation (Phase 6 8/M)

def _counting_clock(step: float):
    """A monotonic clock that advances by ``step`` seconds on every read. Lets a single
    run_once() cross the rotation budget deterministically without real sleeps."""
    state = {"n": -1}

    def clock() -> float:
        state["n"] += 1
        return state["n"] * step

    return clock


def test_session_rotation_defers_remaining_jobs(settings, conn):
    """Custom profile with a session-rotation budget: once the budget on the current
    source elapses mid-run, the worker softly defers the remaining jobs (summary.rotated)
    and stops INITIATING new applies — the same shape as the daily-target break. Works in
    dry-run because rotation paces sources, not real submits."""
    for sid in ("rot-1", "rot-2", "rot-3"):
        _seed_queued_lever(conn, company=f"co-{sid}", source_job_id=sid)

    custom = settings.model_copy(update={
        "strategy": StrategyConfig(profile=StrategyProfile.CUSTOM),
        "pacing": PacingConfig(
            min_delay_s=0.0, max_delay_s=0.0, daily_target=100,
            max_per_company_per_day=100, risk_bias=RiskBias.BALANCED,
            session_rotation_min=10.0,  # 600s budget
        ),
    })
    # step=400s/read: job1 should_rotate sees 400 (<600, processes), job2 sees 800 (>=600,
    # rotates) — so exactly one job runs, the other two defer.
    worker = ApplyWorker(
        settings=custom, conn=conn, fact_bank=_bank(), resume_path="/tmp/r.pdf",
        new_page=_new_page, applicant=Applicant("Pat", "Doe", "pat@example.com", "555"),
        dry_run=True, sleep=_noop_sleep,
        drivers={"lever": _fake_driver(_make_outcome(status=None))},
        rotation_clock=_counting_clock(400.0),
    )

    summary = asyncio.run(worker.run_once())

    assert summary.rotated >= 1
    assert summary.attempted < 3
    # Every queued job is accounted for: either attempted or deferred by rotation.
    assert summary.attempted + summary.rotated == 3
    assert any("session rotation" in n for n in summary.notes)


def test_no_rotation_when_disabled(settings, conn):
    """Default profile (Balanced → session_rotation_min=0.0) never rotates, even with a
    clock that would trip an *enabled* policy instantly — proving it's the disabled flag,
    not a slow clock, keeping rotation off. The whole queue drains."""
    for sid in ("rot-d1", "rot-d2", "rot-d3"):
        _seed_queued_lever(conn, company=f"co-{sid}", source_job_id=sid)

    worker = ApplyWorker(
        settings=settings,  # default = Balanced = rotation disabled
        conn=conn, fact_bank=_bank(), resume_path="/tmp/r.pdf", new_page=_new_page,
        applicant=Applicant("Pat", "Doe", "pat@example.com", "555"),
        dry_run=True, sleep=_noop_sleep,
        drivers={"lever": _fake_driver(_make_outcome(status=None))},
        rotation_clock=_counting_clock(10_000.0),  # would rotate instantly IF enabled
    )

    summary = asyncio.run(worker.run_once())

    assert summary.rotated == 0
    assert summary.attempted == 3


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


def test_default_drivers_registers_all_three_atses():
    drivers = default_drivers()
    assert set(drivers.keys()) == {"lever", "greenhouse", "ashby"}


# --------------------------------------------------------------- crash-sweep (spec §5)

def _seed_stuck_applying_lever(
    conn: sqlite3.Connection, *, company: str, source_job_id: str
):
    """Walk a job all the way through to APPLYING and STOP there — simulates a crash
    between ``set_state(APPLYING)`` and the next transition."""
    repo = JobRepo(conn)
    job = Job(
        source="lever", source_job_id=source_job_id, title="Stuck Job", company=company,
        url=f"https://jobs.lever.co/{company}/{source_job_id}",
    )
    repo.add(job)
    for nxt in (
        JobState.DESCRIBED, JobState.SCORED, JobState.DECIDED,
        JobState.QUEUED_APPLY, JobState.APPLYING,
    ):
        repo.set_state(job.id, nxt)
    return repo.get(job.id)


def test_recover_crashed_requeues_applying_leftovers(settings, conn):
    """Spec §5: jobs left in APPLYING from a crashed prior run must be re-queued."""
    job_a = _seed_stuck_applying_lever(conn, company="acmeco", source_job_id="stuck-1")
    job_b = _seed_stuck_applying_lever(conn, company="otherco", source_job_id="stuck-2")
    worker = _build_worker(
        settings, conn,
        driver=_fake_driver(_make_outcome(status=ApplicationStatus.APPLIED)),
    )

    recovered = worker.recover_crashed()

    assert recovered == 2
    assert JobRepo(conn).get(job_a.id).state is JobState.QUEUED_APPLY
    assert JobRepo(conn).get(job_b.id).state is JobState.QUEUED_APPLY


def test_recover_crashed_is_noop_when_no_applying_leftovers(settings, conn):
    """Idempotency: no APPLYING rows -> zero work, no errors."""
    _seed_queued_lever(conn, company="acmeco", source_job_id="queued-only")
    worker = _build_worker(
        settings, conn,
        driver=_fake_driver(_make_outcome(status=ApplicationStatus.APPLIED)),
    )

    assert worker.recover_crashed() == 0
    # The QUEUED_APPLY job is untouched (recovery only walks APPLYING).
    queued = JobRepo(conn).list_by_state(JobState.QUEUED_APPLY)
    assert len(queued) == 1


def test_run_once_auto_runs_recovery_and_drains_recovered_job(settings, conn):
    """run_once must crash-sweep BEFORE pulling QUEUED_APPLY so the recovered job
    flows through the same cycle (otherwise it sits out another full cycle)."""
    stuck = _seed_stuck_applying_lever(conn, company="acmeco", source_job_id="stuck-3")
    worker = _build_worker(
        settings, conn,
        driver=_fake_driver(_make_outcome(status=ApplicationStatus.APPLIED)),
    )

    summary = asyncio.run(worker.run_once())

    assert summary.recovered == 1
    assert summary.applied == 1
    assert summary.attempted == 1
    assert JobRepo(conn).get(stuck.id).state is JobState.APPLIED
    # The crash-sweep recovery is surfaced as a note for the dashboard / CLI.
    assert any("crash-sweep" in n for n in summary.notes)


def test_run_once_summary_recovered_is_zero_when_nothing_stuck(settings, conn):
    """The summary.recovered field stays at 0 on a clean queue (no spurious notes)."""
    _seed_queued_lever(conn, company="acmeco", source_job_id="clean-1")
    worker = _build_worker(
        settings, conn,
        driver=_fake_driver(_make_outcome(status=ApplicationStatus.APPLIED)),
    )

    summary = asyncio.run(worker.run_once())

    assert summary.recovered == 0
    assert not any("crash-sweep" in n for n in summary.notes)


def test_recovery_can_be_called_without_browser_session(settings, conn):
    """recover_crashed is sync and never touches the driver or browser — operational
    tools (a doctor check, an 'av3 recover' command) must be able to use it without
    booting Chrome."""
    _seed_stuck_applying_lever(conn, company="acmeco", source_job_id="stuck-4")

    # Build the worker with a driver whose `prepare` would EXPLODE if called — proving
    # recovery doesn't touch the driver path.
    def _bomb(*_a, **_kw):
        raise AssertionError("driver must not run during recovery")

    bomb_driver = DriverEntry(
        listing_from_job=lambda j: _job_to_lever_listing(j), prepare=_bomb,
    )
    worker = ApplyWorker(
        settings=settings,
        conn=conn,
        fact_bank=_bank(),
        resume_path="/tmp/resume.pdf",
        new_page=_new_page,
        applicant=Applicant("Pat", "Doe", "pat@example.com", "555-0100"),
        drivers={"lever": bomb_driver},
    )

    assert worker.recover_crashed() == 1
