"""Score worker — drains ``DESCRIBED`` (spec §7 #5, §10).

Where this sits in the pipeline (spec §7):

  (#4 describe) -> DESCRIBED
                      │
                      ▼
            ┌───────────────────┐
            │   score worker    │ ← THIS MODULE
            │  (#5 in spec §7)  │
            └─────────┬─────────┘
                      │
       ┌──────────────┴──────────────┐
       ▼                             ▼
  total >= review_min          total <  review_min
       │                             │
   DESCRIBED → SCORED →          DESCRIBED → SCORED →
       DECIDED                       DECIDED → SKIPPED
   (await optimize)              (didn't meet bar — terminal)

Why the score worker owns the "below-bar SKIPPED" walk instead of leaving it to the
optimize worker: spec §10 says *"Auto >= auto_apply_min (default 7), REVIEW >=
review_min (default 4), else SKIP."* Running the optimize stage (which builds a
tailored résumé + cover letter + runs the fabrication guard — all LLM-heavy) on a
job that already scored below the REVIEW band is exactly the work the threshold is
supposed to prevent. So below-threshold jobs walk through DECIDED → SKIPPED here,
and the optimize worker can assume *every DECIDED job it reads is worth optimizing*.

**Fail-CLOSED** (opposite of the filter worker's fail-open posture):
  * No LLM client at construction → every job gets ``total=0.0`` + dimensions={} +
    walks DESCRIBED → SCORED → DECIDED → SKIPPED.
  * Per-job LLM exception → same fail-closed walk for that one job; other jobs in
    the run continue.

Why the inversion: the filter worker's failure mode (silently FILTER everything)
would lose user jobs forever. The score worker's failure mode (silently let
everything through to the optimize stage with score 0.0) would auto-apply unscored
garbage. The fail-open default is wrong here; "skip on unknown" is the only safe
posture. The CLI exits 1 on ``errors > 0`` so a misconfigured Ollama still trips
monitoring.

Profile = fact bank summary (same shape as the filter worker's bank anchor), so the
two workers reuse one definition of "who the user is." The summary is built once
per run and threaded into every prompt.

State machine recap (spec §5):
  ``DESCRIBED → SCORED → DECIDED → {QUEUED_APPLY, REVIEW, SKIPPED}``
  This worker walks the first three steps for every job, plus DECIDED → SKIPPED
  for below-bar jobs. Above-bar jobs stop at DECIDED for the optimize worker
  (Phase 3 (3/M)) to pick up.
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass, field

from auto_applier.config.settings import ScoringWeights, Settings
from auto_applier.db.repositories import JobRepo, ScoreRepo
from auto_applier.domain.models import Job, JobScore
from auto_applier.domain.state import JobState
from auto_applier.llm.complete import CompletionClient
from auto_applier.llm.prompts import SCORE_JD
from auto_applier.pipeline.filter_worker import build_bank_summary
from auto_applier.pipeline.stage import StageSkip, new_run_id, stage
from auto_applier.resume.factbank import FactBank
from auto_applier.resume.salary import is_below_floor, parse_posted_range

__all__ = ["AXIS_NAMES", "ScoreRunSummary", "ScoreWorker", "parse_dimensions"]


#: The canonical axis order. Used both for prompt schema (in :mod:`auto_applier.llm.prompts`)
#: and for the weighted-sum compute. Any axis the LLM omits defaults to 5.0 (neutral)
#: per the prompt's own contract — keeps a partial reply from cratering the total.
AXIS_NAMES: tuple[str, ...] = (
    "skills", "experience", "seniority", "location",
    "culture", "growth", "compensation",
)

_NEUTRAL = 5.0  # missing-axis default, mirrors the prompt's "unstated → 5.0" rule


# --------------------------------------------------------------- JSON -> dimensions

def parse_dimensions(payload: dict) -> dict[str, float]:
    """Validate + clamp the LLM's JSON reply into a ``{axis: float}`` dict.

    Strict at the wire (the prompt demands an exact shape), lenient at the merge —
    a missing axis defaults to 5.0, a non-numeric value coerces or defaults, and
    every value clamps to ``[0.0, 10.0]``. Raises :class:`ValueError` only when the
    payload isn't a dict at all (anything else is recoverable mid-merge).

    A defensive parser is non-negotiable here because the prompt's correctness is
    what the eval harness ((7/M)) will pin against — a regression in the model has
    to surface as "scores drifted," not as "every axis crashed on parse." This is
    the layer that lets the harness catch the former.
    """
    if not isinstance(payload, dict):
        raise ValueError(f"score reply must be a JSON object (got {type(payload).__name__})")

    out: dict[str, float] = {}
    for axis in AXIS_NAMES:
        raw = payload.get(axis, _NEUTRAL)
        try:
            value = float(raw)
        except (TypeError, ValueError):
            value = _NEUTRAL
        # NaN / inf -> neutral too. Comparison to self is the standard NaN check.
        if value != value or value in (float("inf"), float("-inf")):
            value = _NEUTRAL
        out[axis] = max(0.0, min(10.0, value))
    return out


def weighted_total(dimensions: dict[str, float], weights: ScoringWeights) -> float:
    """Compute the single ``[0, 10]`` total as Σ(axis_i × weight_i).

    The weight model validator guarantees Σweights ≈ 1.0, so the total is in the
    same units as the per-axis scores (no rescaling). Missing dimensions fall back
    to neutral 5.0 — matches :func:`parse_dimensions` so reconstructing a total
    from a raw payload is a one-liner if needed."""
    w = weights.as_dict()
    total = 0.0
    for axis in AXIS_NAMES:
        total += dimensions.get(axis, _NEUTRAL) * w[axis]
    return round(total, 3)


# --------------------------------------------------------------- run summary

@dataclass
class ScoreRunSummary:
    """One ``run_once()`` invocation's outcome — observable, not side-effect-only.

    ``decided`` = above-bar (DESCRIBED → SCORED → DECIDED). ``below_bar`` = jobs
    that walked all the way to terminal SKIPPED because they didn't meet
    ``review_min``. ``failed_closed`` = jobs the worker couldn't score (no LLM
    client, LLM exception, empty description) — also walked to SKIPPED but bucketed
    separately so the CLI / dashboard can tell "we tried and rejected" from "we
    couldn't try at all." ``errors`` counts the latter's exception path so a misconfigured
    Ollama still tips the CLI to exit 1 for monitoring.
    """

    run_id: str
    attempted: int = 0
    decided: int = 0          # DESCRIBED -> SCORED -> DECIDED on real LLM pass
    below_bar: int = 0        # DESCRIBED -> SCORED -> DECIDED -> SKIPPED on real score
    comp_skipped: int = 0     # DESCRIBED -> SCORED -> DECIDED -> SKIPPED (posted pay < floor, §8d)
    failed_closed: int = 0    # same walk, score=0 because LLM unavailable/failed
    errors: int = 0           # per-job LLM exceptions (rolled into failed_closed)
    elapsed_s: float = 0.0
    notes: list[str] = field(default_factory=list)


# --------------------------------------------------------------- the worker

class ScoreWorker:
    """The drain-side of the DESCRIBED queue.

    Construct once per run, call :meth:`run_once`. Stateless across runs aside from
    the DB; a long-lived service keeps one worker alive and calls ``run_once`` on a
    cadence.
    """

    def __init__(
        self,
        *,
        settings: Settings,
        conn: sqlite3.Connection,
        fact_bank: FactBank,
        llm_client: CompletionClient | None = None,
    ):
        self._settings = settings
        self._conn = conn
        self._fact_bank = fact_bank
        self._llm = llm_client

        self._job_repo = JobRepo(conn)
        self._score_repo = ScoreRepo(conn)

        # Bank summary is per-user, stable within a run — build once on construction
        # so the per-job prompt format is a cheap string interpolation.
        self._profile = build_bank_summary(fact_bank)

        # The score record's ``model`` field stamps prompt-version + LLM model so a
        # row is self-describing for the eval harness ((7/M)). Falls back to
        # 'no-llm' so failed_closed rows aren't mis-attributed to a working model.
        self._model_tag = (
            f"{SCORE_JD.version}|{settings.llm.ollama_model}"
            if self._llm is not None
            else f"{SCORE_JD.version}|no-llm"
        )

    # -- public ------------------------------------------------------------

    async def run_once(self, limit: int | None = None) -> ScoreRunSummary:
        """Process up to ``limit`` DESCRIBED jobs. Returns a structured summary so
        the CLI / dashboard can show what just happened without re-querying the DB.
        """
        run_id = new_run_id()
        summary = ScoreRunSummary(run_id=run_id)
        t0 = time.perf_counter()

        if self._llm is None:
            summary.notes.append("no LLM client; every job will SKIP (fail-closed)")

        queued = self._job_repo.list_by_state(JobState.DESCRIBED, limit=limit)

        for job in queued:
            try:
                await self._process_one(job=job, run_id=run_id, summary=summary)
            except StageSkip:
                # Below-bar / fail-closed paths raise StageSkip after the durable
                # transition so the event spine records "skip", not "ok". State is
                # already written here; the exception only shapes the telemetry row.
                pass
            except Exception as exc:  # noqa: BLE001 — isolation is the point
                # @stage already emitted the error event. Walk the job through the
                # fail-closed path so an LLM outage doesn't strand DESCRIBED forever.
                # Note we don't raise StageSkip here: we're already past the @stage
                # frame, so it would just bubble up and break the run.
                summary.errors += 1
                self._write_fail_closed(job, summary, reason=f"score error: {exc}")

        summary.elapsed_s = time.perf_counter() - t0
        return summary

    # -- per-job (the @stage spine emits start/ok/skip/error around this) --

    @stage("score")
    async def _process_one(
        self,
        *,
        job: Job,
        run_id: str,
        summary: ScoreRunSummary,
    ) -> None:
        summary.attempted += 1

        # Fail-closed if we can't score: no LLM client OR empty JD text. Either way
        # we still write a 0.0 score row so the audit trail records "we tried."
        # Raise StageSkip after the durable write so the event spine records 'skip'
        # (with reason) rather than 'ok'.
        if self._llm is None:
            self._write_fail_closed(job, summary, reason="no LLM available")
            raise StageSkip("fail-closed: no LLM available")
        if not (job.description or "").strip():
            self._write_fail_closed(job, summary, reason="empty job description")
            raise StageSkip("fail-closed: empty job description")

        # Compensation comp-filter (spec §8d) — BEFORE the LLM call so a below-floor job
        # costs no scoring work. Only fires when BOTH a floor is configured AND the job
        # posted a range whose top is below it. No posted range → can't filter → proceed.
        posted = parse_posted_range(job.compensation)
        if is_below_floor(posted, self._settings.salary.floor):
            self._skip_below_comp(
                job, summary,
                reason=(
                    f"posted {posted.low:,}-{posted.high:,} < floor "
                    f"{self._settings.salary.floor:,}"
                ),
            )
            raise StageSkip("comp-filter: posted pay below floor")

        # The LLM call — JSON-mode by contract (Ollama format=json /
        # Gemini responseMimeType). A backend exception propagates to run_once,
        # which fail-closes this one job and continues.
        prompt = SCORE_JD.format(profile=self._profile, job_description=job.description)
        payload = await self._llm.complete_json(prompt, system=SCORE_JD.system)

        dimensions = parse_dimensions(payload)
        total = weighted_total(dimensions, self._settings.scoring.weights)

        # Write the score row + walk the state machine. Order matters: score row
        # first, then state. A crash between the two leaves a SCORED row with no
        # JobScore (recoverable: re-score next cycle), not the other way around
        # (an "approved for apply" job with no merit evidence).
        self._score_repo.upsert(
            JobScore(job_id=job.id, total=total, dimensions=dimensions, model=self._model_tag)
        )
        self._job_repo.set_state(job.id, JobState.SCORED)
        self._job_repo.set_state(job.id, JobState.DECIDED)

        if total < self._settings.scoring.review_min:
            # Below the REVIEW band -> terminal SKIPPED. The optimize worker
            # ((3/M)) will trust that every DECIDED job it sees is worth optimizing.
            self._job_repo.set_state(job.id, JobState.SKIPPED)
            summary.below_bar += 1
            raise StageSkip(
                f"below review_min ({total:.2f} < {self._settings.scoring.review_min:.2f})"
            )

        summary.decided += 1

    # -- helpers -----------------------------------------------------------

    def _skip_below_comp(
        self, job: Job, summary: ScoreRunSummary, *, reason: str
    ) -> None:
        """Walk a below-comp-floor job DESCRIBED → SCORED → DECIDED → SKIPPED (spec §8d).

        Durable writes only — does NOT raise (the caller raises StageSkip inside the
        ``@stage`` frame). Writes a 0.0 score row tagged so the dashboard can tell a
        comp-filtered skip from a low-merit skip and from a fail-closed skip — same
        four-transition walk (no DESCRIBED → SKIPPED edge, spec §5) the other skip paths
        use, so history renders uniformly."""
        self._score_repo.upsert(
            JobScore(job_id=job.id, total=0.0, dimensions={}, model=self._model_tag)
        )
        self._job_repo.set_state(job.id, JobState.SCORED)
        self._job_repo.set_state(job.id, JobState.DECIDED)
        self._job_repo.set_state(job.id, JobState.SKIPPED)
        summary.comp_skipped += 1
        summary.notes.append(f"comp-filter skip job {job.id}: {reason}")

    def _write_fail_closed(
        self, job: Job, summary: ScoreRunSummary, *, reason: str
    ) -> None:
        """Walk a job through the full fail-closed path:
        ``DESCRIBED → SCORED → DECIDED → SKIPPED`` with ``total=0.0``.

        **Durable writes only — does NOT raise.** Callers decide whether to raise
        :class:`StageSkip` (when inside a ``@stage`` frame, to record 'skip' instead
        of 'ok') or to return normally (when called from ``run_once``'s exception
        handler, which is already past the ``@stage`` frame and would just bubble
        the StageSkip up and break the run).

        Why all four transitions instead of a shortcut: the state machine has no
        ``DESCRIBED → SKIPPED`` edge (spec §5 — the table is the only source of
        truth). The path here matches what a real-but-below-bar score does, so
        the dashboard renders both uniformly: "this job was attempted, scored
        ``X``, didn't meet the bar." Fail-closed just has ``X=0``."""
        self._score_repo.upsert(
            JobScore(
                job_id=job.id, total=0.0, dimensions={},
                model=self._model_tag,
            )
        )
        self._job_repo.set_state(job.id, JobState.SCORED)
        self._job_repo.set_state(job.id, JobState.DECIDED)
        self._job_repo.set_state(job.id, JobState.SKIPPED)
        summary.failed_closed += 1
        summary.notes.append(f"fail-closed job {job.id}: {reason}")
