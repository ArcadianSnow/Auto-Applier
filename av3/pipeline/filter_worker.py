"""Embedding pre-filter worker — drains ``DISCOVERED`` (spec §7 #3).

Where this sits in the pipeline (spec §7):

  (#2 dedup/ghost) -> DISCOVERED
                          │
                          ▼
                 ┌───────────────────┐
                 │  filter worker    │ ← THIS MODULE
                 │  (#3 in spec §7)  │
                 └─────────┬─────────┘
                           │
            ┌──────────────┼──────────────┐
            ▼              ▼              ▼
        cosine >=      cosine <        embed error /
        threshold      threshold       no embed client
            │              │              │
        DESCRIBED      FILTERED        DESCRIBED (fail-open)

Why "fail-open" routes a per-job embed error to DESCRIBED, not FILTERED:
  * FILTERED is **terminal** (spec §5). An inference-layer outage must never silently
    drop jobs into a terminal state — the user would never know what was lost.
  * The state machine only allows ``DISCOVERED → {SKIPPED, FILTERED, DESCRIBED}``;
    REVIEW isn't reachable from DISCOVERED, so fail-open to DESCRIBED is the
    consistent "we couldn't filter, let it proceed" choice. The describe / score
    stages downstream will still decide on merits.

What it embeds:
  * **Query side (job):** ``(title + company + snippet)`` — whatever text exists at
    DISCOVERED. Full JD doesn't arrive until DESCRIBED (the *next* stage), so the
    pre-filter intentionally operates on the cheap-to-fetch listing-page text. That's
    the throughput/cost win the spec calls out: don't pay JD-scrape + LLM-score on
    obvious non-matches.
  * **Anchor side (you):** a single concatenated summary of the fact bank — skills,
    work titles, top bullets, degrees, certifications. **Embedded ONCE per run** and
    re-used across all jobs. The bank is per-user and stable within a run, so embedding
    it inside the per-job loop would burn calls without changing the result.

Threshold:
  * Default ``0.6`` — conservative (favors *recall*: false-positive an obvious-bad job
    into DESCRIBED rather than false-negative a borderline match into FILTERED). Do
    NOT tune the default until the scoring eval harness ((7/M)) provides a calibration
    set; tuning blind would just lock in noise.
  * Pass via constructor / ``--threshold`` CLI flag for ad-hoc experimentation.

Spec §7 also offers a "top-N rather than threshold" variant. Threshold is what v3.0
ships because the pre-filter runs per cycle (not per batch) — there's no obvious "N"
to pick across cycle boundaries. The cycle/batch knob is a v3.1 strategy-profile
concern (spec §8a).
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass, field

from av3.config.settings import Settings
from av3.db.repositories import JobRepo
from av3.domain.models import Job
from av3.domain.state import JobState
from av3.llm.embed import EmbeddingClient, cosine
from av3.pipeline.stage import StageSkip, new_run_id, stage
from av3.resume.factbank import FactBank

__all__ = ["FilterRunSummary", "FilterWorker", "build_bank_summary"]


# --------------------------------------------------------------- bank summary

def build_bank_summary(fact_bank: FactBank) -> str:
    """Flatten the fact bank into the single string we embed once per run.

    Captures the *shape* of the user — skills first (the highest-signal field for a
    job-fit cosine), then work-history titles, then a sampling of bullets, then
    degrees + certifications. Quantities and dates aren't useful for semantic
    similarity against a listing snippet, so they're omitted.

    Returns ``""`` if the bank is empty — callers must treat empty-summary as a
    fail-open signal (cosine against zero-norm is 0.0, which would FILTER everything).
    """
    parts: list[str] = []
    if fact_bank.skills:
        parts.append(" ".join(fact_bank.skills))
    parts.extend(t for t in fact_bank.titles() if t)
    for entry in fact_bank.work_history:
        parts.extend(b for b in entry.bullets if b)
    parts.extend(d for d in fact_bank.degrees() if d)
    parts.extend(c for c in fact_bank.certifications if c)
    return " ".join(p.strip() for p in parts if p and p.strip())


def _job_query_text(job: Job) -> str:
    """Listing-page text we embed per job. Title + company + whatever description
    is present at DISCOVERED (often a snippet, sometimes empty)."""
    chunks = [job.title, job.company, job.description]
    return " ".join(c.strip() for c in chunks if c and c.strip())


# --------------------------------------------------------------- run summary

@dataclass
class FilterRunSummary:
    """One ``run_once()`` invocation's outcome — observable, not side-effect-only.

    ``passed`` = transitions to DESCRIBED on a real cosine pass. ``filtered`` =
    transitions to FILTERED (terminal). ``failed_open`` = transitions to DESCRIBED
    because the embed layer was unavailable or raised for that job — distinct from
    ``passed`` so a CLI/dashboard can tell "the filter didn't actually filter" from
    "the filter did filter and the job passed."
    """

    run_id: str
    attempted: int = 0
    passed: int = 0           # DISCOVERED -> DESCRIBED on cosine pass
    filtered: int = 0         # DISCOVERED -> FILTERED on cosine fail
    failed_open: int = 0      # DISCOVERED -> DESCRIBED because embed unavailable
    errors: int = 0           # per-job embed exceptions (rolled into failed_open)
    elapsed_s: float = 0.0
    notes: list[str] = field(default_factory=list)


# --------------------------------------------------------------- the worker

class FilterWorker:
    """The drain-side of the DISCOVERED queue.

    Construct once per run, call :meth:`run_once`. Like :class:`ApplyWorker`, stateless
    across runs aside from the DB — a long-lived service can keep one worker alive and
    just call ``run_once`` on a cadence.
    """

    def __init__(
        self,
        *,
        settings: Settings,
        conn: sqlite3.Connection,
        fact_bank: FactBank,
        embed_client: EmbeddingClient | None = None,
        threshold: float = 0.6,
    ):
        self._settings = settings
        self._conn = conn
        self._fact_bank = fact_bank
        self._embed_client = embed_client
        self._threshold = threshold

        self._job_repo = JobRepo(conn)

        # Bank summary stays stable within a run; embed it lazily on first need so
        # tests that never call run_once don't pay the cost. Cached after first call.
        self._bank_text = build_bank_summary(fact_bank)
        self._bank_vec: list[float] | None = None

    # -- public ------------------------------------------------------------

    async def run_once(self, limit: int | None = None) -> FilterRunSummary:
        """Process up to ``limit`` DISCOVERED jobs. Returns a structured summary so
        the CLI / dashboard can show what just happened without re-querying the DB.

        Fail-open invariants:
          * No embed client -> every DISCOVERED job routes to DESCRIBED (``failed_open``).
            Logged once in ``notes`` so the cause is visible.
          * Empty bank summary -> same fail-open: cosine against an empty anchor is 0.0,
            which would FILTER everything indiscriminately. Logged in ``notes``.
          * Per-job embed exception -> that job alone routes to DESCRIBED
            (``failed_open`` + ``errors``); the run continues.
        """
        run_id = new_run_id()
        summary = FilterRunSummary(run_id=run_id)
        t0 = time.perf_counter()

        queued = self._job_repo.list_by_state(JobState.DISCOVERED, limit=limit)
        if not queued:
            summary.elapsed_s = time.perf_counter() - t0
            return summary

        # Decide fail-open posture once, before the per-job loop, so the notes are
        # written in run order rather than tacked on at the end. Guarded on a
        # non-empty queue so an idle scheduler tick doesn't burn an embed call.
        bank_vec = await self._ensure_bank_vec(summary)
        force_pass = bank_vec is None  # missing embed OR empty bank

        for job in queued:
            try:
                await self._process_one(
                    job=job, run_id=run_id, summary=summary,
                    bank_vec=bank_vec, force_pass=force_pass,
                )
            except StageSkip:
                # Below-threshold path raises StageSkip *after* writing FILTERED so the
                # event spine records "skip", not "ok". State is already durable here;
                # the exception just shapes the telemetry row.
                pass
            except Exception:  # noqa: BLE001 — isolation is the point
                # The @stage wrapper already emitted the error row. Route the job to
                # fail-open DESCRIBED so an inference outage doesn't strand jobs in
                # DISCOVERED forever; counter it in failed_open + errors.
                summary.errors += 1
                self._fail_open_to_described(job, summary, reason="embed error")

        summary.elapsed_s = time.perf_counter() - t0
        return summary

    # -- per-job (the @stage spine emits start/ok/skip/error around this) --

    @stage("filter")
    async def _process_one(
        self,
        *,
        job: Job,
        run_id: str,
        summary: FilterRunSummary,
        bank_vec: list[float] | None,
        force_pass: bool,
    ) -> None:
        summary.attempted += 1

        # Fail-open posture: route to DESCRIBED without spending an embed call.
        if force_pass or bank_vec is None:
            self._fail_open_to_described(job, summary, reason="no embed/bank")
            return

        query = _job_query_text(job)
        if not query:
            # No listing text to compare against — fail-open so a thin discovery row
            # gets the full describe + score downstream rather than dying silently.
            self._fail_open_to_described(job, summary, reason="empty listing text")
            return

        # Embed the query side only — bank side is cached. One HTTP call per job.
        assert self._embed_client is not None  # mypy/runtime: force_pass guards None
        job_vec = await self._embed_client.embed(query)
        sim = cosine(job_vec, bank_vec)

        if sim >= self._threshold:
            self._job_repo.set_state(job.id, JobState.DESCRIBED)
            summary.passed += 1
            return

        # Below threshold -> terminal FILTERED. Write the transition BEFORE raising
        # StageSkip so the state is durable even if telemetry blew up.
        self._job_repo.set_state(job.id, JobState.FILTERED)
        summary.filtered += 1
        raise StageSkip(f"below threshold (sim={sim:.3f} < {self._threshold:.3f})")

    # -- helpers -----------------------------------------------------------

    async def _ensure_bank_vec(self, summary: FilterRunSummary) -> list[float] | None:
        """Embed the bank summary once per run. Returns None to signal "fail-open
        every job this run" — either because no embed client was configured, the
        bank summary is empty, or embedding the bank itself raised."""
        if self._embed_client is None:
            summary.notes.append("no embed client; routing all to DESCRIBED")
            return None
        if not self._bank_text:
            summary.notes.append("empty fact-bank summary; routing all to DESCRIBED")
            return None
        if self._bank_vec is not None:
            return self._bank_vec
        try:
            self._bank_vec = await self._embed_client.embed(self._bank_text)
        except Exception as exc:  # noqa: BLE001 — fail-open is the contract
            summary.notes.append(f"bank embed failed ({exc}); routing all to DESCRIBED")
            return None
        return self._bank_vec

    def _fail_open_to_described(
        self, job: Job, summary: FilterRunSummary, *, reason: str
    ) -> None:
        """Transition DISCOVERED → DESCRIBED + bookkeep ``failed_open``.

        Idempotent on the summary: never double-counts (caller is the only path)."""
        self._job_repo.set_state(job.id, JobState.DESCRIBED)
        summary.failed_open += 1
        summary.notes.append(f"fail-open job {job.id}: {reason}")

