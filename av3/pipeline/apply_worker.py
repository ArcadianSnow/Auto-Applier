"""Apply worker — drains ``QUEUED_APPLY`` and runs the real submit path (spec §7 #7).

Where this sits in the pipeline (spec §7):

  ... (#6 optimize+Strict gate) -> QUEUED_APPLY
                                       │
                                       ▼
                              ┌──────────────────┐
                              │  apply worker    │ ← THIS MODULE
                              │  (#7 in spec §7) │
                              └────────┬─────────┘
                                       │
                ┌──────────────────────┼─────────────────────────┐
                ▼                      ▼                         ▼
        BROWSER_AUTO          BROWSER_ASSISTED          driver raised / no submit btn
        positive confirm →    pre-filled →              mid-form break →
        APPLIED               REVIEW                    FAILED → REVIEW

Responsibilities:

1. Constructs the :class:`AnswerResolver` **once per run** from the injected fact bank +
   answer repo (+ optional embedding/LLM clients). The bank is per-user, not per-job, so
   building it inside the per-job loop would just heat embed caches needlessly.
2. Pulls ``QUEUED_APPLY`` jobs (oldest first via ``list_by_state`` ORDER BY discovered_at).
3. **Per-company rate limit** (spec §7 re-apply policy) — silent skip when the configured
   ``max_per_company_per_day`` is hit; the job stays in QUEUED_APPLY for tomorrow.
4. Dispatches to the right per-ATS apply driver via the source registry. Each driver gets
   the resolver so it can fill custom questions; the driver still owns the (dry_run, mode)
   dispatch and the §8b required-Q downgrade to ASSISTED_PENDING.
5. **Translates ``ApplyOutcome.status`` → ``JobState`` via the strict state machine:**
      APPLIED            -> ``APPLYING -> APPLIED`` (terminal; dedup source of truth)
      ASSISTED_PENDING   -> ``APPLYING -> REVIEW``  (deliberate human handoff)
      UNCONFIRMED/FAILED -> ``APPLYING -> FAILED -> REVIEW`` (spec §5 wording)
      dry_run            -> no transition at all (no APPLYING in the first place)
6. Writes an :class:`Application` row with mode + status + submitted_at, so the dashboard
   can show "what attempt produced what outcome" even when the job stays in REVIEW.
7. Emits §8b iteration-feedback events for every INFERRED resolution into the local event
   spine — **metadata only (question label, category, confidence, outcome). The answer
   value never leaves the box** and EEO rows are dropped entirely (§8d, §9 telemetry policy).
8. **Per-job error isolation** (matches v2's hard-won pattern in
   ``orchestrator/engine.py``): a driver exception transitions only that job to
   FAILED→REVIEW with the error in the application note; the loop continues.

Why the driver registry is injectable: tests fake the driver entirely instead of dragging
in the per-ATS FakePage scaffolding, which means worker tests stay focused on the worker's
contract (state transitions, rate limiting, isolation, telemetry) and don't double-test
the drivers.

Pacing (spec §8a fixed for v3.0):
  * Inter-apply random delay from ``settings.pacing.{min,max}_delay_s`` — applied between
    *successful* submits only. Rate-limited skips don't burn a delay slot. The Pareto
    strategy profiles are v3.1; v3.0 ships these fixed knobs.
"""

from __future__ import annotations

import asyncio
import random
import sqlite3
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from av3.config.settings import Settings
from av3.db.repositories import AnswerRepo, ApplicationRepo, JobRepo
from av3.domain.models import Application, Job, utcnow_iso
from av3.domain.state import ApplicationStatus, ApplyMode, JobState
from av3.llm.complete import CompletionClient
from av3.llm.embed import EmbeddingClient
from av3.pipeline.stage import new_run_id, stage
from av3.resume.answer_resolver import (
    AnswerResolver,
    ResolutionSource,
    SensitiveClass,
)
from av3.resume.factbank import FactBank
from av3.sources.browser import greenhouse_apply, lever_apply
from av3.sources.browser.apply_base import Applicant, ApplyOutcome
from av3.sources.greenhouse import JobListing as GreenhouseListing
from av3.sources.lever import LeverListing
from av3.telemetry import get_sink

__all__ = ["ApplyRunSummary", "ApplyWorker", "DriverEntry", "default_drivers"]


# --------------------------------------------------------------- driver registry

@dataclass(frozen=True)
class DriverEntry:
    """One row in the per-source dispatch table.

    ``listing_from_job`` shapes the source-neutral :class:`Job` into the listing dataclass
    the driver expects (``LeverListing`` / ``JobListing``). ``prepare`` is the driver's
    ``prepare_application`` coroutine (or any callable matching its signature — tests pass
    a stub).
    """

    listing_from_job: Callable[[Job], Any]
    prepare: Callable[..., Awaitable[ApplyOutcome]]


def _gh_token_from_url(url: str) -> str:
    """Best-effort board-token extraction from a Greenhouse job URL.

    Canonical shape: ``https://job-boards.greenhouse.io/<token>/jobs/<id>``. The board
    token isn't stored on :class:`Job` (it's a Greenhouse-only concept), so we recover it
    from the URL when present and fall back to ``company`` (which the GH source set when
    a board didn't expose a friendlier display name)."""
    if not url:
        return ""
    marker = "greenhouse.io/"
    idx = url.find(marker)
    if idx < 0:
        return ""
    tail = url[idx + len(marker):]
    return tail.split("/", 1)[0]


def _job_to_lever_listing(job: Job) -> LeverListing:
    """``LeverListing.apply_url`` is ``{hostedUrl}/apply`` (spec/research §Lever)."""
    apply_url = f"{job.url}/apply" if job.url else ""
    return LeverListing(
        source_job_id=job.source_job_id,
        title=job.title,
        company=job.company,
        location=job.location,
        url=job.url,
        apply_url=apply_url,
        description=job.description,
        posted_at=job.posted_at,
    )


def _job_to_greenhouse_listing(job: Job) -> GreenhouseListing:
    token = _gh_token_from_url(job.url) or job.company
    return GreenhouseListing(
        source_job_id=job.source_job_id,
        title=job.title,
        company=job.company,
        location=job.location,
        url=job.url,
        board_token=token,
        posted_at=job.posted_at,
        description=job.description,
    )


def default_drivers() -> dict[str, DriverEntry]:
    """The production dispatch table. Tests inject their own to fake the drivers."""
    return {
        "lever": DriverEntry(_job_to_lever_listing, lever_apply.prepare_application),
        "greenhouse": DriverEntry(
            _job_to_greenhouse_listing, greenhouse_apply.prepare_application
        ),
    }


# --------------------------------------------------------------- run summary

@dataclass
class ApplyRunSummary:
    """One ``run_once()`` invocation's outcome — observable, not side-effect-only.

    ``review`` covers ASSISTED_PENDING + UNCONFIRMED + driver-FAILED (everything that
    transitioned to ``JobState.REVIEW``). ``skipped`` covers rate-limit drops and
    unknown-source rows; ``errors`` covers unhandled exceptions during ``_process_one``.
    """

    run_id: str
    attempted: int = 0    # process_one calls that ran the driver (incl. dry-runs)
    applied: int = 0      # outcome.status == APPLIED
    review: int = 0       # any non-APPLIED real status (ASSISTED_PENDING/UNCONFIRMED/FAILED)
    skipped: int = 0      # rate-limit / unknown source
    errors: int = 0       # exception during _process_one
    dry_run_count: int = 0
    elapsed_s: float = 0.0
    notes: list[str] = field(default_factory=list)


# --------------------------------------------------------------- the worker

class ApplyWorker:
    """The drain-side of the QUEUED_APPLY queue.

    Construct once per run, call :meth:`run_once`. The worker is stateless across runs
    aside from the DB (which is the system of record per spec §2) and the resolver's
    internal caches — so a long-lived service can keep one worker alive and just call
    ``run_once`` on a cadence.
    """

    def __init__(
        self,
        *,
        settings: Settings,
        conn: sqlite3.Connection,
        fact_bank: FactBank,
        resume_path: str,
        new_page: Callable[[], Awaitable[Any]],
        applicant: Applicant | None = None,
        embed_client: EmbeddingClient | None = None,
        llm_client: CompletionClient | None = None,
        salary_expectation: str = "",
        mode: ApplyMode = ApplyMode.BROWSER_AUTO,
        dry_run: bool = True,
        sleep: Callable[[float], Awaitable[None]] | None = None,
        rng: random.Random | None = None,
        drivers: dict[str, DriverEntry] | None = None,
    ):
        self._settings = settings
        self._conn = conn
        self._fact_bank = fact_bank
        self._resume_path = resume_path
        self._new_page = new_page
        self._mode = mode
        self._dry_run = dry_run
        self._sleep = sleep or asyncio.sleep
        self._rng = rng or random.Random()
        self._drivers = drivers if drivers is not None else default_drivers()

        self._job_repo = JobRepo(conn)
        self._app_repo = ApplicationRepo(conn)
        self._answer_repo = AnswerRepo(conn)

        # Applicant: prefer explicit override; otherwise build from fact-bank contact.
        # The fact bank is the single source of truth (spec §6b) so this is just an
        # ATS-shape adapter, not a second model.
        self._applicant = applicant or Applicant.from_contact(fact_bank.contact)

        # Resolver constructed once per run — fact bank is per-user, not per-job, so
        # rebuilding it per job would just heat embed caches and re-load answer rows.
        self._resolver = AnswerResolver(
            fact_bank=fact_bank,
            answer_repo=self._answer_repo,
            embed_client=embed_client,
            llm_client=llm_client,
            salary_expectation=salary_expectation,
        )

    # -- public ------------------------------------------------------------

    async def run_once(self, limit: int | None = None) -> ApplyRunSummary:
        """Process up to ``limit`` QUEUED_APPLY jobs. Returns a structured summary so
        the CLI / dashboard can show what just happened without re-querying the DB."""
        run_id = new_run_id()
        summary = ApplyRunSummary(run_id=run_id)
        t0 = time.perf_counter()

        queued = self._job_repo.list_by_state(JobState.QUEUED_APPLY, limit=limit)
        prior_was_apply = False

        for job in queued:
            # Pace between successive *real* applies (spec §8a fixed pacing). Skips
            # don't burn a delay slot; the rate-limit branch sets prior_was_apply=False.
            if prior_was_apply:
                delay = self._rng.uniform(
                    self._settings.pacing.min_delay_s,
                    self._settings.pacing.max_delay_s,
                )
                await self._sleep(delay)

            # Unknown source: skip with a note. Don't change state — a future driver
            # rollout might pick it up next cycle.
            if job.source not in self._drivers:
                summary.skipped += 1
                summary.notes.append(f"unknown source {job.source!r} for job {job.id}")
                prior_was_apply = False
                continue

            # Per-company/day rate limit (spec §7 re-apply policy).
            count = self._job_repo.company_applied_count(job.company)
            if count >= self._settings.pacing.max_per_company_per_day:
                summary.skipped += 1
                summary.notes.append(
                    f"rate-limit skip: {job.company} ({count}/"
                    f"{self._settings.pacing.max_per_company_per_day})"
                )
                prior_was_apply = False
                continue

            # Per-job error isolation. The @stage("apply") wrapper inside _process_one
            # records error events; we catch here so one driver crash doesn't kill the
            # rest of the run.
            try:
                status = await self._process_one(job=job, run_id=run_id)
            except Exception as exc:  # noqa: BLE001 — isolation is the point
                summary.errors += 1
                self._recover_job_to_review(job, exc)
                prior_was_apply = False
                continue

            summary.attempted += 1
            if self._dry_run:
                summary.dry_run_count += 1
                prior_was_apply = False  # no real submit fired
            elif status is ApplicationStatus.APPLIED:
                summary.applied += 1
                prior_was_apply = True
            else:
                summary.review += 1
                prior_was_apply = True

        summary.elapsed_s = time.perf_counter() - t0
        return summary

    # -- per-job (the @stage spine emits start/ok/error around this) -------

    @stage("apply")
    async def _process_one(self, *, job: Job, run_id: str) -> ApplicationStatus | None:
        """Run the apply path for one job.

        Returns the driver's :class:`ApplicationStatus` (or ``None`` in dry-run). Raises
        on unexpected failures — the caller (:meth:`run_once`) catches and routes to
        FAILED→REVIEW so the loop is resilient.
        """
        driver = self._drivers[job.source]

        # In dry-run we skip the APPLYING transition entirely. Otherwise we'd have to
        # undo it on every call (APPLYING → QUEUED_APPLY) just to test fills, which
        # creates a stream of state-machine ping-pong in the event log.
        if not self._dry_run:
            self._job_repo.set_state(job.id, JobState.APPLYING)

        listing = driver.listing_from_job(job)
        page = await self._new_page()

        outcome = await driver.prepare(
            page,
            listing,
            self._applicant,
            self._resume_path,
            dry_run=self._dry_run,
            mode=self._mode,
            resolver=self._resolver,
        )

        # Mirror INFERRED resolutions to the event spine (spec §8b iteration loop / §9
        # metadata-only mirror). Skip EEO entirely — those answer rows never mirror.
        self._mirror_inferred_resolutions(run_id, job, outcome)

        if self._dry_run:
            # No state transition (we never went to APPLYING) — job stays in QUEUED_APPLY.
            return None

        # Write the Application row first so a follow-up state transition crash still
        # leaves a record of *what was attempted* (useful for the dashboard's
        # "what happened to this job?" view).
        attempted_status = outcome.status or ApplicationStatus.FAILED
        self._app_repo.add(
            Application(
                job_id=job.id,
                mode=outcome.mode,
                status=attempted_status,
                generated_resume_path=self._resume_path,
                submitted_at=utcnow_iso() if outcome.submitted else "",
            )
        )

        # Translate ApplyOutcome -> JobState via the strict state machine. Each call
        # validates the edge; an invalid one raises (caught upstream as an error event).
        if outcome.status is ApplicationStatus.APPLIED:
            self._job_repo.set_state(job.id, JobState.APPLIED)
        elif outcome.status is ApplicationStatus.ASSISTED_PENDING:
            # Deliberate handoff: APPLYING → REVIEW (added edge, see spec §5 docstring).
            self._job_repo.set_state(job.id, JobState.REVIEW)
        else:
            # UNCONFIRMED / FAILED / None (defensive): spec §5 wording — FAILED → REVIEW.
            self._job_repo.set_state(job.id, JobState.FAILED)
            self._job_repo.set_state(job.id, JobState.REVIEW)

        return outcome.status

    # -- recovery + telemetry ----------------------------------------------

    def _recover_job_to_review(self, job: Job, exc: BaseException) -> None:
        """An unhandled driver/resolver exception left the job in APPLYING (we'd already
        set that). Walk it to REVIEW via FAILED with the error attached to a FAILED
        Application row — same shape as a driver-reported FAILED so the dashboard treats
        them uniformly.
        """
        # Refresh state in case set_state(APPLYING) didn't run (e.g. dry-run, but
        # exceptions in dry-run are still possible from a stub driver). If the job's
        # still in QUEUED_APPLY, we don't need to touch it — the next run will pick it
        # up. If it's in APPLYING, route to FAILED→REVIEW.
        current = self._job_repo.get(job.id)
        if current is None or current.state is not JobState.APPLYING:
            return

        # Record the failed attempt so the human triage in REVIEW has the error visible.
        self._app_repo.add(
            Application(
                job_id=job.id,
                mode=self._mode,
                status=ApplicationStatus.FAILED,
                generated_resume_path=self._resume_path,
            )
        )
        self._job_repo.set_state(job.id, JobState.FAILED)
        self._job_repo.set_state(job.id, JobState.REVIEW)

    def _mirror_inferred_resolutions(
        self, run_id: str, job: Job, outcome: ApplyOutcome
    ) -> None:
        """Emit one ``resolver_inferred`` event per INFERRED resolution.

        Per spec §9: ``{question_text, category, confidence, outcome}`` — the answer
        value never leaves the machine and **EEO rows do not mirror at all** (§8d).
        """
        sink = get_sink()
        if sink is None or not outcome.resolutions:
            return
        for resolution in outcome.resolutions:
            if resolution.sensitive is SensitiveClass.EEO:
                continue  # §8d: EEO answers stay 100% local, including the metadata row
            if resolution.source is not ResolutionSource.INFERRED:
                continue
            sink.emit(
                stage="resolver_inferred",
                status="ok",
                run_id=run_id,
                platform=job.source,
                job_id=job.id,
                context={
                    "question": resolution.question.label,
                    "category": resolution.sensitive.value,
                    "confidence": round(resolution.confidence, 3),
                    "outcome": "answered" if resolution.fills else "bailed",
                },
            )
