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
6. **Reads the per-job optimize-generated artifacts** (spec §6b / §7 #6): the tailored
   résumé PDF + cover letter the optimize worker wrote, keyed by ``job.id`` via the same
   ``auto_applier.resume.generate`` path helpers (file existence is the durable contract — no DB
   hand-off). Falls back to the single global ``resume.pdf`` only when no per-job PDF
   exists (a job that reached QUEUED_APPLY before optimize ran, or a manual re-queue).
   Writes an :class:`Application` row with mode + status + the resolved résumé/cover
   paths + submitted_at, so the dashboard can show "what attempt produced what outcome"
   even when the job stays in REVIEW.
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

Pacing & strategy (spec §8a — Pareto profiles, Phase 6 / v3.1):
  * The active :class:`auto_applier.config.strategy.EffectivePacing` is resolved ONCE at
    construction via ``resolve_strategy(settings)`` — the profile→knobs mapping lives in
    ``auto_applier.config.strategy``, not here. The worker reads ``self._pacing`` for every knob.
  * Inter-apply random delay from ``pacing.{min,max}_delay_s`` — applied between
    *successful* submits only. Rate-limited skips don't burn a delay slot.
  * Per-company/day cap from ``pacing.max_per_company_per_day`` (spec §7 anti-spam).
  * **Soft daily target** from ``pacing.daily_target`` — once the day's APPLIED count
    reaches it, the worker stops INITIATING new applies this run and defers the rest
    (left in QUEUED_APPLY); a *goal*, never a hard wall, and dry-runs never trip it.
  * **Risk-router bias** from ``pacing.risk_bias`` — a ``leans_assisted`` profile starts
    every job assisted (the *starting* posture; the driver's detection-signal downgrade,
    the safety floor, still fires on top).
  * **Session rotation** from ``pacing.session_rotation_min`` (spec §8a 8/M) — a
    :class:`auto_applier.config.strategy.SessionRotationPolicy` time-boxes how long the run keeps
    applying to one source before rotating off it. Consulted at the top of the per-job
    loop; when the budget on the current source elapses the worker *softly* defers the
    remaining jobs (left in QUEUED_APPLY), the same shape as the daily-target break. The
    clock is injectable (``rotation_clock``) so tests drive it deterministically; default
    profile (Balanced, ``session_rotation_min=0.0``) disables it = v3.0 behaviour.
  * **Concurrency** (``pacing.concurrency``) is a declared parallel ceiling the profile
    carries for a future parallel drainer / the dashboard; this worker still drains
    sequentially, so it reads but doesn't act on it yet — see ``strategy.py``.
"""

from __future__ import annotations

import asyncio
import random
import sqlite3
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from auto_applier.config.settings import Settings
from auto_applier.config.strategy import (
    EffectivePacing,
    RiskBias,
    SessionRotationPolicy,
    resolve_strategy,
)
from auto_applier.db.repositories import AnswerRepo, ApplicationRepo, JobRepo
from auto_applier.domain.models import Application, Job, utcnow_iso
from auto_applier.domain.state import ApplicationStatus, ApplyMode, JobState
from auto_applier.llm.complete import CompletionClient
from auto_applier.llm.embed import EmbeddingClient
from auto_applier.pipeline.stage import new_run_id, stage
from auto_applier.resume.answer_resolver import (
    AnswerResolver,
    ResolutionSource,
    SensitiveClass,
)
from auto_applier.resume.factbank import FactBank
from auto_applier.resume.generate import (
    archive_cover_letter,
    archive_resume,
    existing_job_cover,
    existing_job_resume,
    resolve_generated_cover_letter,
    resolve_generated_resume,
)
from auto_applier.resume.salary import (
    build_market_source,
    format_ask,
    parse_posted_range,
    recommend_ask,
)
from auto_applier.sources.ashby import AshbyListing
from auto_applier.sources.browser import ashby_apply, greenhouse_apply, lever_apply
from auto_applier.sources.browser.apply_base import Applicant, ApplyOutcome
from auto_applier.sources.greenhouse import JobListing as GreenhouseListing
from auto_applier.sources.health import is_paused
from auto_applier.sources.lever import LeverListing
from auto_applier.telemetry import get_sink

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


def _job_to_ashby_listing(job: Job) -> AshbyListing:
    """Ashby apply URL is ``{jobUrl}/application`` (research §Ashby). When ``Job.url``
    already points at the apply endpoint (e.g. the discovery row populated ``applyUrl``
    into ``url`` directly), keep it as-is."""
    apply_url = job.url
    if apply_url and not apply_url.rstrip("/").endswith("/application"):
        apply_url = f"{apply_url}/application"
    return AshbyListing(
        source_job_id=job.source_job_id,
        title=job.title,
        company=job.company,
        location=job.location,
        url=job.url,
        apply_url=apply_url,
        description=job.description,
        posted_at=job.posted_at,
    )


def default_drivers() -> dict[str, DriverEntry]:
    """The production dispatch table. Tests inject their own to fake the drivers."""
    return {
        "lever": DriverEntry(_job_to_lever_listing, lever_apply.prepare_application),
        "greenhouse": DriverEntry(
            _job_to_greenhouse_listing, greenhouse_apply.prepare_application
        ),
        "ashby": DriverEntry(_job_to_ashby_listing, ashby_apply.prepare_application),
    }


# --------------------------------------------------------------- run summary

@dataclass
class ApplyRunSummary:
    """One ``run_once()`` invocation's outcome — observable, not side-effect-only.

    ``review`` covers ASSISTED_PENDING + UNCONFIRMED + driver-FAILED (everything that
    transitioned to ``JobState.REVIEW``). ``skipped`` covers rate-limit drops and
    unknown-source rows; ``errors`` covers unhandled exceptions during ``_process_one``.
    ``recovered`` counts jobs swept from a crashed prior run's APPLYING state back to
    QUEUED_APPLY (spec §5 crash-sweep mandate; runs at the top of every ``run_once``).
    """

    run_id: str
    attempted: int = 0    # process_one calls that ran the driver (incl. dry-runs)
    applied: int = 0      # outcome.status == APPLIED
    review: int = 0       # any non-APPLIED real status (ASSISTED_PENDING/UNCONFIRMED/FAILED)
    skipped: int = 0      # rate-limit / unknown source / paused source (spec §8b)
    paused: int = 0       # subset of skipped: AUTH_REQUIRED source (spec §8b)
    errors: int = 0       # exception during _process_one
    recovered: int = 0    # crash-sweep: APPLYING leftovers re-queued (spec §5)
    deferred_daily_target: int = 0  # jobs left in QUEUED_APPLY because the soft daily target was hit (§8a)
    rotated: int = 0      # jobs left in QUEUED_APPLY because the session-rotation budget elapsed (§8a 8/M)
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
        rotation_clock: Callable[[], float] | None = None,
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
        # Session-rotation clock (spec §8a 8/M) — injectable so tests advance it
        # deterministically; None → SessionRotationPolicy falls back to time.monotonic.
        self._rotation_clock = rotation_clock

        # Strategy profile (spec §8a, Phase 6) — resolve the active pacing knobs ONCE.
        # The profile→knobs mapping lives in auto_applier.config.strategy; the worker reads the
        # resolved EffectivePacing instead of settings.pacing directly so a non-default
        # profile (cautious/aggressive) takes effect without touching this loop. Default
        # profile (balanced) resolves to the historical PacingConfig defaults, so v3.0
        # behaviour is unchanged.
        self._pacing: EffectivePacing = resolve_strategy(settings)

        # Salary intelligence (spec §8d, Phase 6) — the market source is built ONCE
        # (default NoMarketData = local-first, zero egress). The per-job recommended ask
        # is computed inside _process_one from {config floor/ceiling, the job's posted
        # comp, market}. The explicit ``salary_expectation`` constructor arg still seeds
        # the resolver (back-compat); when salary config is present the per-job compute
        # overrides it before each job's questions resolve.
        self._market = build_market_source(settings.salary.market_source)

        self._job_repo = JobRepo(conn)
        self._app_repo = ApplicationRepo(conn)
        self._answer_repo = AnswerRepo(conn)

        # Applicant: prefer explicit override; otherwise build from fact-bank contact.
        # The fact bank is the single source of truth (spec §6b) so this is just an
        # ATS-shape adapter, not a second model. Keep the override so a mid-run fact-bank
        # reload (below) rebuilds the applicant from the new contact ONLY when not overridden.
        self._applicant_override = applicant
        self._applicant = applicant or Applicant.from_contact(fact_bank.contact)

        # Resolver constructed once per run — fact bank is per-user, not per-job, so
        # rebuilding it per job would just heat embed caches and re-load answer rows.
        self._resolver = AnswerResolver(
            fact_bank=fact_bank,
            answer_repo=self._answer_repo,
            embed_client=embed_client,
            llm_client=llm_client,
            salary_expectation=salary_expectation,
            attest_human=settings.attest_human,
            draft_freeform=settings.draft_freeform_answers,
        )

        # The worker + its resolver hold the fact bank in memory for the whole serve session.
        # Track master.json's mtime so ``_refresh_fact_bank`` can hot-reload a profile edit
        # (e.g. the "More details" wizard step adding nationality/notice/gender) WITHOUT a
        # restart — otherwise the live apply fills from a stale bank and those fields go blank
        # (live bug 2026-06-21: saved More-details extras never filled).
        self._fact_bank_mtime: float | None = self._master_mtime()

        # Dry-run leaves jobs in QUEUED_APPLY (no APPLYING transition), so without this the
        # scheduler re-picks the same jobs every cycle and re-runs the fill forever. The worker
        # is built once per serve session and reused across cycles, so an in-memory "already
        # dry-run-tested" set stops the loop (resets on restart — fine for testing).
        self._dry_run_tested_job_ids: set[str] = set()

    # -- public ------------------------------------------------------------

    def _effective_mode(self) -> ApplyMode:
        """The apply mode after the strategy's risk-router bias (spec §8a).

        A ``LEANS_ASSISTED`` profile (Cautious) starts every job in assisted regardless
        of the requested mode — the low-risk, human-submits posture. Other biases honour
        the constructor's ``mode``. This is the *starting* posture only; the driver's
        downgrade-to-assisted on a real detection signal (the safety floor) is unaffected
        and still fires on top of this.
        """
        if self._pacing.risk_bias is RiskBias.LEANS_ASSISTED:
            return ApplyMode.BROWSER_ASSISTED
        return self._mode

    def recover_crashed(self) -> int:
        """Re-queue jobs left in ``APPLYING`` from a crashed prior run (spec §5 mandate).

        A previous run that died between ``set_state(APPLYING)`` and the next state
        transition leaves the job stuck in ``APPLYING`` — out of the queue, but the
        attempt never reached a terminal state. The strict state machine allows
        ``APPLYING → QUEUED_APPLY`` exactly for this case; this method walks the leftover
        rows and re-queues them so the next ``run_once`` picks them up. Idempotent — if
        nothing is stuck, this is a single read and a no-op.

        Returns the count of jobs re-queued. ``run_once`` calls this automatically; the
        method is public so a doctor command or stand-alone "av3 recover" can use it
        without booting a browser session.
        """
        stuck = self._job_repo.list_by_state(JobState.APPLYING)
        for job in stuck:
            self._job_repo.set_state(job.id, JobState.QUEUED_APPLY)
        return len(stuck)

    def _master_mtime(self) -> float | None:
        """Modification time of the fact-bank file, or None if it's missing/unreadable."""
        try:
            return (self._settings.data_dir / "profile" / "master.json").stat().st_mtime
        except OSError:
            return None

    def _refresh_fact_bank(self) -> None:
        """Hot-reload master.json into the worker + resolver when it changed since we last
        read it, so a profile edit (e.g. the 'More details' wizard step adding nationality /
        notice period / gender) takes effect on the NEXT cycle without restarting the worker.

        Cheap: a stat() each cycle, a load only when the mtime moved. Fail-safe: any read/parse
        error keeps the current bank (a half-written file must never break the apply loop)."""
        mtime = self._master_mtime()
        if mtime is None or mtime == self._fact_bank_mtime:
            return
        try:
            bank = FactBank.load(self._settings.data_dir / "profile" / "master.json")
        except (OSError, ValueError):
            return  # keep the in-memory bank; try again next cycle
        self._fact_bank = bank
        self._resolver.fact_bank = bank
        # Rebuild the ATS applicant from the fresh contact UNLESS an explicit override was
        # injected at construction (tests / callers that pin a specific applicant).
        if self._applicant_override is None:
            self._applicant = Applicant.from_contact(bank.contact)
        self._fact_bank_mtime = mtime

    async def run_once(self, limit: int | None = None) -> ApplyRunSummary:
        """Process up to ``limit`` QUEUED_APPLY jobs. Returns a structured summary so
        the CLI / dashboard can show what just happened without re-querying the DB.

        Auto-runs the spec §5 crash-sweep first so a previous run's APPLYING leftovers
        rejoin the queue *before* we read it (otherwise they'd sit out yet another
        cycle).
        """
        # Pick up any profile edit (e.g. "More details") saved since the worker started —
        # the bank is held in memory for the session, so without this the live apply fills
        # from a stale copy and newly-saved screener fields go blank.
        self._refresh_fact_bank()
        run_id = new_run_id()
        summary = ApplyRunSummary(run_id=run_id)
        t0 = time.perf_counter()

        # Spec §5: re-queue APPLYING leftovers from a crashed prior run BEFORE pulling
        # the queue, so the same run picks them up. The recovered count is recorded for
        # observability; the swept jobs flow through the normal pipeline below.
        summary.recovered = self.recover_crashed()
        if summary.recovered:
            summary.notes.append(
                f"crash-sweep: re-queued {summary.recovered} APPLYING leftover(s)"
            )

        queued = self._job_repo.list_by_state(JobState.QUEUED_APPLY, limit=limit)
        prior_was_apply = False

        # Session-rotation policy (spec §8a 8/M) — time-box the run on one source, then
        # rotate. Disabled (no-op) when the active profile's session_rotation_min is 0
        # (Balanced / v3.0). Clock is injectable for deterministic tests.
        rotation = SessionRotationPolicy(
            self._pacing.session_rotation_min, now=self._rotation_clock
        )

        for job in queued:
            # Session rotation (spec §8a 8/M) — consult BEFORE any per-job work so the
            # budget is measured against the source we're about to apply to. on_source
            # (re)starts the timer when the source changes; should_rotate fires once the
            # budget on the current source elapses. A *soft* stop like the daily target:
            # the remaining jobs stay in QUEUED_APPLY for the next cycle, no error, no
            # state change. Applies in dry-run too (it paces sources, not real submits).
            rotation.on_source(job.source)
            if rotation.should_rotate():
                remaining = len(queued) - queued.index(job)
                summary.rotated = remaining
                summary.notes.append(
                    f"session rotation: {self._pacing.session_rotation_min:g} min on "
                    f"{job.source!r} elapsed (profile={self._pacing.profile.value}); "
                    f"deferring {remaining} job(s)"
                )
                break

            # Soft daily target (spec §8a) — a *goal*, never a hard wall. In a real
            # (non-dry) run, once the day's APPLIED count reaches the profile's target we
            # stop INITIATING new applies this run and leave the rest in QUEUED_APPLY for
            # the next day. It never errors and never touches gather stages, so the
            # pipeline isn't blocked; it just paces volume. Dry runs never count toward
            # (or trip) the target — they don't produce APPLIED rows. company_applied_count
            # already re-queries the DB so this naturally includes applies from this run.
            if not self._dry_run:
                applied_today = self._job_repo.applied_count_on_day()
                if applied_today >= self._pacing.daily_target:
                    remaining = len(queued) - (queued.index(job))
                    summary.deferred_daily_target = remaining
                    summary.notes.append(
                        f"daily target reached ({applied_today}/"
                        f"{self._pacing.daily_target}, profile="
                        f"{self._pacing.profile.value}); deferring {remaining} job(s)"
                    )
                    break

            # Pace between successive *real* applies (spec §8a). Skips don't burn a delay
            # slot; the rate-limit branch sets prior_was_apply=False.
            if prior_was_apply:
                delay = self._rng.uniform(
                    self._pacing.min_delay_s,
                    self._pacing.max_delay_s,
                )
                await self._sleep(delay)

            # Unknown source: skip with a note. Don't change state — a future driver
            # rollout might pick it up next cycle.
            if job.source not in self._drivers:
                summary.skipped += 1
                summary.notes.append(f"unknown source {job.source!r} for job {job.id}")
                prior_was_apply = False
                continue

            # Source paused (spec §8b session expiry): silently skip, leave the job
            # in QUEUED_APPLY for next cycle. Other sources keep running; the user
            # re-logs in when convenient and ``auto_applier.sources.health.mark_healthy()``
            # clears the pause. No state change, no rate-limit slot burned.
            if is_paused(job.source):
                summary.skipped += 1
                summary.paused += 1
                summary.notes.append(
                    f"source-paused skip: {job.source} auth_required (job {job.id})"
                )
                prior_was_apply = False
                continue

            # Per-company/day rate limit (spec §7 re-apply policy; cap from the active
            # strategy profile, §8a).
            count = self._job_repo.company_applied_count(job.company)
            if count >= self._pacing.max_per_company_per_day:
                summary.skipped += 1
                summary.notes.append(
                    f"rate-limit skip: {job.company} ({count}/"
                    f"{self._pacing.max_per_company_per_day})"
                )
                prior_was_apply = False
                continue

            # Dry-run idempotency: a dry-run job stays in QUEUED_APPLY, so the scheduler would
            # otherwise re-test it (and re-emit apply events) every cycle. Skip jobs already
            # dry-run-tested this session — checked HERE so the @stage("apply") wrapper doesn't
            # even fire for a skip. (Resets on restart — fine for testing.)
            if self._dry_run and job.id in self._dry_run_tested_job_ids:
                summary.skipped += 1
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

        # Per-job salary ask (spec §8d). Compute from config floor/ceiling + the job's
        # posted comp (if any) + the market source, and set it on the shared resolver
        # before this job's questions resolve. Safe because the worker processes jobs
        # sequentially (one driver fully awaited before the next). When no salary config
        # and no posted comp exist, recommend_ask returns None → "" → resolver bails any
        # salary question to REVIEW (unchanged v3.0 behaviour).
        self._apply_salary_ask(job)
        # Per-job context for assisted freeform drafting (BUILD 6 Phase B). Set on the shared
        # resolver before this job's questions resolve (same per-job pattern as the salary ask,
        # safe because jobs process sequentially) so a "why this company?" essay can draft
        # against the real company/JD. No-op unless draft_freeform is enabled.
        self._resolver.current_job = job

        listing = driver.listing_from_job(job)
        page = await self._new_page()

        # Per-job optimize-generated artifacts (spec §6b / §7 #6). The optimize
        # worker wrote a tailored résumé PDF + cover letter keyed by job.id; read
        # them by the same derivation (file existence is the durable contract).
        # The global resume.pdf the worker was built with is only a fallback.
        resume_used, cover_used = self._artifacts_for(job)

        outcome = await driver.prepare(
            page,
            listing,
            self._applicant,
            resume_used,
            cover_letter_path=cover_used,
            dry_run=self._dry_run,
            mode=self._effective_mode(),
            resolver=self._resolver,
        )

        # Mirror INFERRED resolutions to the event spine (spec §8b iteration loop / §9
        # metadata-only mirror). Skip EEO entirely — those answer rows never mirror.
        self._mirror_inferred_resolutions(run_id, job, outcome)
        # Local-only per-question observability — what resolved/filled vs bailed. NOT
        # mirrored (sink._maybe_mirror forwards only error/resolver_inferred). Without it a
        # dry-run left no record of fills, which made the 2026-06-13 "nothing filled" report
        # un-diagnosable. Metadata only — never the answer value.
        self._log_resolutions(run_id, job, outcome)

        if self._dry_run:
            # No state transition (we never went to APPLYING) — job stays in QUEUED_APPLY.
            # Remember it so the next cycle doesn't re-run the fill on the same job.
            self._dry_run_tested_job_ids.add(job.id)
            return None

        # On a confirmed APPLIED, archive the (generic-named) manually-assigned files — move
        # them to uploads/_archive with the job id appended, and record the ARCHIVE path on the
        # row so it points at the kept file (BUILD 1.1/1.2). The live upload already happened in
        # driver.prepare; archiving is post-confirmation bookkeeping and never fatal. A
        # non-APPLIED outcome (assisted/review) leaves files in the job folder — not yet
        # "confirmed used", and assisted still needs them. archive_* no-ops (returns None) when
        # the file used was the optimize PDF / global résumé (not in the uploads folder).
        cover_for_row, resume_for_row = cover_used, resume_used
        if outcome.status is ApplicationStatus.APPLIED:
            archived_cover = archive_cover_letter(self._settings, job.id)
            if archived_cover is not None:
                cover_for_row = str(archived_cover)
            archived_resume = archive_resume(self._settings, job.id)
            if archived_resume is not None:
                resume_for_row = str(archived_resume)

        # Write the Application row first so a follow-up state transition crash still
        # leaves a record of *what was attempted* (useful for the dashboard's
        # "what happened to this job?" view).
        attempted_status = outcome.status or ApplicationStatus.FAILED
        self._app_repo.add(
            Application(
                job_id=job.id,
                mode=outcome.mode,
                status=attempted_status,
                generated_resume_path=resume_for_row,
                cover_letter_path=cover_for_row,
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

    # -- salary ------------------------------------------------------------

    def _apply_salary_ask(self, job: Job) -> None:
        """Compute this job's recommended salary ask (spec §8d) and set it on the resolver.

        Inputs: the user's configured floor/ceiling (``settings.salary``), the job's posted
        comp string (``job.compensation``, parsed into a range when present), and the market
        source. ``recommend_ask`` prioritises posted-range → market → user range; the floor
        is a hard lower bound. Result is formatted (``"$140,000"``) and assigned to the
        shared resolver's ``salary_expectation`` — the resolver's SALARY branch fills it.
        A ``None`` recommendation (no inputs at all) clears it to ``""`` so the resolver
        bails salary questions to REVIEW rather than inventing a number.
        """
        cfg = self._settings.salary
        posted = parse_posted_range(job.compensation)
        market = None
        if cfg.market_source and cfg.market_source.lower() not in ("", "none", "off"):
            market = self._market.estimate(title=job.title, location=job.location)
        # Fall back to the targeting salary floor: onboarding writes targeting.salary_floor (the
        # discovery filter), not salary.floor, so without this the user's stated floor never
        # reaches the salary answer and it bails to REVIEW.
        user_floor = cfg.floor if cfg.floor else self._settings.targeting.salary_floor
        rec = recommend_ask(
            user_floor=user_floor,
            user_ceiling=cfg.ceiling,
            posted=posted,
            market=market,
        )
        self._resolver.salary_expectation = format_ask(rec)

    # -- artifacts ---------------------------------------------------------

    def _artifacts_for(self, job: Job) -> tuple[str, str]:
        """Resolve the per-job résumé PDF + cover letter the optimize worker generated.

        The optimize+Strict gate (spec §7 #6) writes a tailored résumé PDF and a cover
        letter keyed by ``job.id`` via the same ``auto_applier.resume.generate`` path helpers we
        read here — file existence is the durable "this job was optimized" contract, so
        no DB column carries the path (see the ``optimize_worker`` docstring). Returns
        ``(resume_path, cover_letter_path)`` as strings for the driver upload + the
        :class:`Application` row:

          * **résumé**: a per-job manually-assigned résumé
            (``artifacts/uploads/<job_id>/Resume.*`` via ``av3 resume``, generic basename) when
            present; else the optimize-generated per-job PDF; else the single global
            ``resume.pdf`` the worker was constructed with (a job that reached QUEUED_APPLY
            before optimize ran, or a manual re-queue).
          * **cover letter**: the per-job manually-assigned letter
            (``artifacts/uploads/<job_id>/Cover Letter.*`` via ``av3 cover``, generic basename
            for upload) when present; else the optimize-generated ``.txt``; else ``""`` (no
            cover to attach/record). **No per-company fallback** — files are per posting (see
            ``auto_applier.resume.generate`` / BUILD 1.1, 1.2).
        """
        manual_resume = existing_job_resume(self._settings, job.id)
        if manual_resume is not None:
            resume_used = str(manual_resume)
        else:
            # ``resolve_generated_resume`` tolerates the on-disk name-scheme drift (legacy
            # bare ``{job_id}.pdf`` vs the readable name, + a name-change between optimize-write
            # and apply-read) — a plain ``generated_resume_path(...).exists()`` would orphan any
            # pre-rename artifact and fall through to the (often-absent) global résumé.
            pdf = resolve_generated_resume(self._settings, job.id)
            resume_used = str(pdf) if pdf is not None else self._resume_path

        manual_cover = existing_job_cover(self._settings, job.id)
        if manual_cover is not None:
            cover_used = str(manual_cover)
        else:
            optimize_cover = resolve_generated_cover_letter(self._settings, job.id)
            cover_used = str(optimize_cover) if optimize_cover is not None else ""
        return resume_used, cover_used

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
        # Use the same per-job artifact resolution as the success path so the dashboard
        # shows which résumé the failed attempt would have used.
        resume_used, cover_used = self._artifacts_for(job)
        self._app_repo.add(
            Application(
                job_id=job.id,
                mode=self._effective_mode(),
                status=ApplicationStatus.FAILED,
                generated_resume_path=resume_used,
                cover_letter_path=cover_used,
            )
        )
        self._job_repo.set_state(job.id, JobState.FAILED)
        self._job_repo.set_state(job.id, JobState.REVIEW)

    def _log_resolutions(self, run_id: str, job: Job, outcome: ApplyOutcome) -> None:
        """Emit a local ``resolution`` event per discovered question (metadata only).

        Records ``{label, kind, required, source, fills, filled_on_page}`` — enough to see,
        from ``events.db`` alone, exactly what each form field resolved to and whether it
        landed, WITHOUT the answer value ever being written. Not mirrored (see
        ``sink._maybe_mirror`` — only error/resolver_inferred categories forward)."""
        sink = get_sink()
        if sink is None or not outcome.resolutions:
            return
        for q, r in zip(outcome.custom_questions, outcome.resolutions):
            sink.emit(
                stage="resolution",
                status="ok",
                run_id=run_id,
                platform=job.source,
                job_id=job.id,
                context={
                    "label": (q.label or "")[:120],
                    "kind": q.kind,
                    "required": q.required,
                    "source": r.source.value,
                    "fills": bool(getattr(r, "fills", False)),
                    "filled_on_page": outcome.filled.get(f"q:{q.field_id}"),
                },
            )

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
