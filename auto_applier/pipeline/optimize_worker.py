"""Optimize+Strict gate worker - drains ``DECIDED`` (spec §7 #6 + §6b).

Where this sits in the pipeline (spec §7):

  (#5 score) -> DECIDED   (above review_min — score worker walks below-bar to SKIPPED)
                  │
                  ▼
        ┌─────────────────────┐
        │   optimize worker   │ ← THIS MODULE
        │   (#6 in spec §7)   │
        └──────────┬──────────┘
                   │
       generate résumé from fact bank   ──┐
       generate cover letter from bank  ──┤── ALL must succeed
       fabrication guard PASSES         ──┤
       PDF render + cover letter write  ──┘
                   │
       ┌───────────┴───────────┐
       ▼                       ▼
   QUEUED_APPLY            REVIEW
   (apply worker            (any gate failed —
    consumes blind)          human triage required)

> "Optimize (Strict gate): for auto-bound decisions: generate the per-job résumé
>  from the fact bank (§6b) + cover letter + run the fabrication guard. Pass →
>  QUEUED_APPLY; fail → drop to REVIEW (never auto-submit un-optimized)."

The **Strict gate is THE safety mechanism that justifies BROWSER_AUTO** (handoff §8).
Without it, the apply worker would ship unverified résumés that fabricate facts on
real applications. So every failure mode here drops the job to REVIEW — including
configuration failures the user can fix. The cost of a false REVIEW is a manual
click; the cost of a false QUEUED_APPLY is a fabricated submission. Asymmetric.

**Fail-CLOSED** (matches the score worker's posture, opposite of the filter worker):

  * No LLM client at construction → every DECIDED job walks to REVIEW (``routed_to_review``,
    ``failed_closed`` bookkept separately).
  * Per-job résumé/cover generation exception → that one job to REVIEW.
  * Fabrication guard returns REVIEW or HARD_FAIL → that one job to REVIEW.
  * PDF render returns False → that one job to REVIEW.
  * Cover letter file write exception → that one job to REVIEW.

The state machine (``auto_applier.domain.state``) already lists ``DECIDED → {QUEUED_APPLY,
REVIEW, SKIPPED}`` as allowed, so no new edges. SKIPPED is reserved for the score
worker's below-bar walk — the optimize worker never routes to SKIPPED (a job that
reached DECIDED was above the bar; if optimize fails, it's a human-triage problem,
not a "skip forever" decision).

**Canonical artifact paths.** The worker writes:

  * résumé PDF      → ``settings.artifacts_dir / "generated" / "{job_id}.pdf"``
  * cover letter txt → ``settings.artifacts_dir / "generated" / "{job_id}_cover.txt"``

Both are derived from ``job.id`` via :func:`auto_applier.resume.generate.generated_resume_path`
and :func:`auto_applier.resume.generate.generated_cover_letter_path`, so the apply worker
reads them by the same derivation — no DB column added to ``jobs`` for the paths
(file existence is the durable contract). The apply worker writes those paths into
its ``Application`` row at submit time, where the schema (spec §4) already has
``cover_letter_path`` + ``generated_resume_path``.

State machine recap (spec §5):
  ``DESCRIBED → SCORED → DECIDED → {QUEUED_APPLY, REVIEW, SKIPPED}``
  Below-bar SKIPPED is owned by the score worker. This worker owns the gate
  between DECIDED and {QUEUED_APPLY, REVIEW}.
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass, field

from auto_applier.config.settings import Settings
from auto_applier.db.repositories import JobRepo
from auto_applier.domain.models import Job
from auto_applier.domain.state import JobState
from auto_applier.llm.complete import CompletionClient
from auto_applier.llm.prompts import GENERATE_COVER_LETTER, GENERATE_RESUME
from auto_applier.pipeline.stage import StageSkip, new_run_id, stage
from auto_applier.resume.factbank import FactBank
from auto_applier.resume.generate import (
    DEFAULT_COVER_TARGET_WORDS,
    CoverLetterGenerator,
    ResumeGenerator,
    generated_cover_letter_path,
    generated_resume_path,
)
from auto_applier.resume.guard import GuardResult, Verdict, guard_l1
from auto_applier.resume.render import PdfRenderer, build_resume_html, render_resume_pdf

__all__ = ["OptimizeRunSummary", "OptimizeWorker"]


# --------------------------------------------------------------- run summary

@dataclass
class OptimizeRunSummary:
    """One ``run_once()`` invocation's outcome — observable, not side-effect-only.

    ``queued`` = DECIDED → QUEUED_APPLY (all gates passed). ``routed_to_review`` =
    DECIDED → REVIEW (any gate failed — covers both the deliberate-fail cases like
    "guard caught a fabrication" AND the configuration-fail cases like "LLM client
    not configured"). ``failed_closed`` = the subset of ``routed_to_review`` that
    failed for *operational* reasons (no LLM / per-job exception) rather than
    *content* reasons (guard rejection / render failure) — bookkept separately so
    the CLI / dashboard can distinguish "fix your Ollama" from "review this
    fabrication". ``errors`` counts the exception path so a misconfigured Ollama
    still trips the CLI to exit 1 for monitoring.
    """

    run_id: str
    attempted: int = 0
    queued: int = 0           # DECIDED -> QUEUED_APPLY (gates clean)
    routed_to_review: int = 0  # DECIDED -> REVIEW (any gate failed)
    guard_rejected: int = 0   # subset of routed_to_review: fabrication guard caught it
    render_failed: int = 0    # subset of routed_to_review: PDF render returned False
    failed_closed: int = 0    # subset of routed_to_review: no LLM / per-job exception
    errors: int = 0           # per-job exceptions (rolled into failed_closed)
    elapsed_s: float = 0.0
    notes: list[str] = field(default_factory=list)


# --------------------------------------------------------------- the worker

class OptimizeWorker:
    """The drain-side of the DECIDED queue + Strict gate.

    Construct once per run, call :meth:`run_once`. Stateless across runs aside
    from the DB and the artifacts dir; a long-lived service keeps one worker
    alive and calls ``run_once`` on a cadence.

    The PDF renderer is injectable so tests don't need Playwright/Chromium; in
    production the default :func:`auto_applier.resume.render.render_resume_pdf` wires the
    real headless Chromium path. Both résumé generator and cover-letter generator
    take the same :class:`CompletionClient` — they're orchestration shells over
    versioned prompts that live in :mod:`auto_applier.llm.prompts`.
    """

    def __init__(
        self,
        *,
        settings: Settings,
        conn: sqlite3.Connection,
        fact_bank: FactBank,
        llm_client: CompletionClient | None = None,
        pdf_renderer: PdfRenderer | None = None,
        cover_target_words: int = DEFAULT_COVER_TARGET_WORDS,
    ):
        self._settings = settings
        self._conn = conn
        self._fact_bank = fact_bank
        self._llm = llm_client
        self._pdf_renderer: PdfRenderer = pdf_renderer or render_resume_pdf
        self._cover_target_words = cover_target_words

        self._job_repo = JobRepo(conn)

        # Generators are stateless wrappers over the LLM client; one each is enough.
        # Constructed lazily so a ``--no-llm`` run doesn't build dead objects.
        self._resume_gen: ResumeGenerator | None = (
            ResumeGenerator(llm_client) if llm_client is not None else None
        )
        self._cover_gen: CoverLetterGenerator | None = (
            CoverLetterGenerator(llm_client, target_words=cover_target_words)
            if llm_client is not None
            else None
        )

        # Tag stamped into notes so the eval harness ((7/M)) and a future audit
        # trail can see which prompt versions produced which artifacts. The same
        # tag pattern as the score worker.
        self._resume_tag = (
            f"{GENERATE_RESUME.version}|{settings.llm.ollama_model}"
            if llm_client is not None
            else f"{GENERATE_RESUME.version}|no-llm"
        )
        self._cover_tag = (
            f"{GENERATE_COVER_LETTER.version}|{settings.llm.ollama_model}"
            if llm_client is not None
            else f"{GENERATE_COVER_LETTER.version}|no-llm"
        )

    # -- public ------------------------------------------------------------

    async def run_once(self, limit: int | None = None) -> OptimizeRunSummary:
        """Process up to ``limit`` DECIDED jobs. Returns a structured summary so
        the CLI / dashboard can show what just happened without re-querying the DB.
        """
        run_id = new_run_id()
        summary = OptimizeRunSummary(run_id=run_id)
        t0 = time.perf_counter()

        if self._llm is None:
            summary.notes.append(
                "no LLM client; every DECIDED job will route to REVIEW (fail-closed)"
            )

        queued = self._job_repo.list_by_state(JobState.DECIDED, limit=limit)

        for job in queued:
            try:
                await self._process_one(job=job, run_id=run_id, summary=summary)
            except StageSkip:
                # Routed-to-REVIEW path raises StageSkip after the durable transition
                # so the event spine records 'skip', not 'ok'. State is already written
                # here; the exception only shapes the telemetry row.
                pass
            except Exception as exc:  # noqa: BLE001 — isolation is the point
                # @stage already emitted the error event. Walk the job to REVIEW so an
                # LLM/render outage doesn't strand DECIDED forever. We don't raise
                # StageSkip from here: we're already past the @stage frame, so it would
                # bubble out of run_once and break the run (the score worker hit the
                # same shape — see handoff §8 "StageSkip raise discipline").
                summary.errors += 1
                self._route_to_review(
                    job, summary, reason=f"optimize error: {exc}", failed_closed=True
                )

        summary.elapsed_s = time.perf_counter() - t0
        return summary

    # -- per-job (the @stage spine emits start/ok/skip/error around this) --

    @stage("optimize")
    async def _process_one(
        self,
        *,
        job: Job,
        run_id: str,
        summary: OptimizeRunSummary,
    ) -> None:
        summary.attempted += 1

        # Fail-closed if no LLM. Walk DECIDED -> REVIEW with a clear note; raise
        # StageSkip after the durable write so the event spine records 'skip'.
        if self._llm is None or self._resume_gen is None or self._cover_gen is None:
            self._route_to_review(
                job, summary, reason="no LLM client", failed_closed=True
            )
            raise StageSkip("fail-closed: no LLM available")

        description = (job.description or "").strip()
        if not description:
            # A DECIDED row with no JD is a programming error upstream (the score
            # worker rejects empty JDs before they reach DECIDED), but handle it
            # defensively rather than crashing the run.
            self._route_to_review(
                job, summary, reason="empty job description", failed_closed=True
            )
            raise StageSkip("fail-closed: empty job description")

        # 1. Generate the structured résumé.
        resume = await self._resume_gen.generate(
            bank=self._fact_bank, job_description=description
        )

        # 2. Generate the cover letter body.
        cover_body = await self._cover_gen.generate(
            bank=self._fact_bank,
            job_description=description,
            company=job.company,
            title=job.title,
        )

        # 3. Fabrication guard — the load-bearing fact check.
        verdict: GuardResult = guard_l1(resume, self._fact_bank)
        if not verdict.ok:
            findings_summary = self._summarize_findings(verdict)
            summary.guard_rejected += 1
            self._route_to_review(
                job, summary, reason=f"guard {verdict.verdict.value}: {findings_summary}"
            )
            raise StageSkip(f"guard {verdict.verdict.value}")

        # 4. Render the PDF. We render BEFORE writing the cover letter so a render
        #    failure doesn't leave an orphan .txt on disk (the apply worker keys
        #    off the PDF's existence; an orphan cover would be confusing).
        html_content = build_resume_html(resume, self._fact_bank.contact)
        pdf_path = generated_resume_path(self._settings, job.id)
        ok = await self._pdf_renderer(html_content, pdf_path)
        if not ok:
            summary.render_failed += 1
            self._route_to_review(
                job, summary, reason="PDF render failed"
            )
            raise StageSkip("fail-closed: PDF render failed")

        # 5. Write the cover letter. Caught as a per-job exception by run_once if
        #    the disk is full / permissions wrong (same fail-closed walk).
        cover_path = generated_cover_letter_path(self._settings, job.id)
        cover_path.parent.mkdir(parents=True, exist_ok=True)
        cover_path.write_text(cover_body, encoding="utf-8")

        # 6. All gates clean — DECIDED -> QUEUED_APPLY.
        self._job_repo.set_state(job.id, JobState.QUEUED_APPLY)
        summary.queued += 1
        summary.notes.append(
            f"queued job {job.id}: resume={self._resume_tag} cover={self._cover_tag}"
        )

    # -- helpers -----------------------------------------------------------

    def _route_to_review(
        self,
        job: Job,
        summary: OptimizeRunSummary,
        *,
        reason: str,
        failed_closed: bool = False,
    ) -> None:
        """Walk a DECIDED job to REVIEW + bookkeep.

        **Durable writes only — does NOT raise.** Callers decide whether to raise
        :class:`StageSkip` (when inside a ``@stage`` frame, to record 'skip' instead
        of 'ok') or to return normally (when called from ``run_once``'s exception
        handler, which is already past the ``@stage`` frame). Same discipline as
        the score worker's :meth:`_write_fail_closed` (see handoff §8).

        Idempotent only on the summary counter (caller is the only path); the state
        transition itself raises :class:`InvalidTransition` if called twice (the
        state machine refuses ``REVIEW → REVIEW``), which is the correct behavior —
        it catches a worker-internal bug rather than silently double-counting.
        """
        self._job_repo.set_state(job.id, JobState.REVIEW)
        summary.routed_to_review += 1
        if failed_closed:
            summary.failed_closed += 1
        summary.notes.append(f"routed job {job.id} to REVIEW: {reason}")

    @staticmethod
    def _summarize_findings(verdict: GuardResult) -> str:
        """Render a guard verdict's findings into one short string for the notes.

        Caps at three findings so a noisy résumé doesn't flood the summary;
        the full findings list lives on the verdict object and could be persisted
        as a sidecar artifact in the future (out of scope for v3.0)."""
        if not verdict.findings:
            return verdict.verdict.value
        parts = [
            f"{f.severity.value} {f.category} '{f.claim}'"
            for f in verdict.findings[:3]
        ]
        suffix = (
            f" (+{len(verdict.findings) - 3} more)" if len(verdict.findings) > 3 else ""
        )
        return "; ".join(parts) + suffix
