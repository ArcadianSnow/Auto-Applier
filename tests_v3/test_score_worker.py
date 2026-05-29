"""Score worker (spec §7 #5, §10) — contract tests for the DESCRIBED drain loop.

Same fake-the-client pattern as ``test_filter_worker.py`` / ``test_apply_worker.py``:
we inject a deterministic :class:`CompletionClient` stub so these tests focus on the
worker's contract, not the LLM wire format.

Coverage:
  * above review_min -> DESCRIBED -> SCORED -> DECIDED (``summary.decided``);
  * below review_min -> DESCRIBED -> SCORED -> DECIDED -> SKIPPED (``below_bar``);
  * boundary: total == review_min is the BELOW-bar side (strict < passes) — guards
    against off-by-one in the threshold;
  * missing LLM client at construction -> every job fail-closes to SKIPPED
    (``failed_closed`` bookkept separately from ``below_bar``);
  * per-job LLM exception -> isolated, that job alone fail-closes, run continues;
  * empty job description -> per-job fail-closed (the prompt has nothing to score);
  * malformed LLM payload (e.g. non-dict) -> exception -> isolated fail-closed;
  * defensive parser: missing axis -> 5.0; out-of-range -> clamps; non-numeric -> 5.0;
  * weighted-sum math matches Settings.scoring.weights;
  * ``limit`` honored;
  * model tag stamps prompt version on every score row (so the eval harness can pin).
"""

from __future__ import annotations

import asyncio
import sqlite3
from typing import Any

from av3.config.settings import Settings
from av3.db.repositories import JobRepo, ScoreRepo
from av3.domain.models import Job
from av3.domain.state import JobState
from av3.llm.prompts import SCORE_JD
from av3.pipeline.score_worker import (
    AXIS_NAMES,
    ScoreRunSummary,
    ScoreWorker,
    parse_dimensions,
    weighted_total,
)
from av3.resume.factbank import Contact, FactBank, WorkEntry


# --------------------------------------------------------------- fakes

class _FakeLLM:
    """Returns ``payloads`` in order (one per call). Records prompts for assertion.

    A single dict in ``payloads`` is reused for every call (lets a happy-path test
    say "every job gets the same score" in one line).
    """

    def __init__(self, payloads: list[dict] | dict | None = None):
        if isinstance(payloads, dict) or payloads is None:
            self._single = payloads or {axis: 8.0 for axis in AXIS_NAMES}
            self._queue: list[dict] | None = None
        else:
            self._single = None
            self._queue = list(payloads)
        self.prompts: list[str] = []
        self.systems: list[str] = []
        self.raise_for: set[str] = set()
        self.raise_next: list[Exception] = []

    async def complete_json(self, prompt: str, *, system: str = "") -> dict:
        self.prompts.append(prompt)
        self.systems.append(system)
        if self.raise_next:
            raise self.raise_next.pop(0)
        for marker in self.raise_for:
            if marker in prompt:
                raise RuntimeError(f"simulated LLM failure for {marker!r}")
        if self._queue is not None:
            if not self._queue:
                raise AssertionError("fake LLM payload queue exhausted")
            return self._queue.pop(0)
        return dict(self._single) if self._single is not None else {}


# --------------------------------------------------------------- helpers

def _bank() -> FactBank:
    return FactBank(
        contact=Contact(name="Pat Doe", email="pat@example.com"),
        skills=["python", "sql", "etl"],
        work_history=[
            WorkEntry(company="Acme", title="Data Engineer", start="2020", end="2023",
                      bullets=["built pipelines"]),
        ],
    )


def _seed_described(
    conn: sqlite3.Connection, *, source_job_id: str,
    title: str = "Senior Data Engineer", company: str = "BetaCo",
    description: str = "We need a Python + SQL data engineer to own ETL pipelines.",
) -> Job:
    """Insert a job at DESCRIBED (post-filter) via the canonical state walk."""
    repo = JobRepo(conn)
    job = Job(
        source="greenhouse",
        source_job_id=source_job_id,
        title=title,
        company=company,
        description=description,
        url=f"https://job-boards.greenhouse.io/test/jobs/{source_job_id}",
    )
    repo.add(job)
    repo.set_state(job.id, JobState.DESCRIBED)
    return repo.get(job.id)  # type: ignore[return-value]


def _build(
    settings: Settings,
    conn: sqlite3.Connection,
    *,
    llm_client=None,
    fact_bank: FactBank | None = None,
) -> ScoreWorker:
    return ScoreWorker(
        settings=settings,
        conn=conn,
        fact_bank=fact_bank if fact_bank is not None else _bank(),
        llm_client=llm_client,
    )


# --------------------------------------------------------------- parser

def test_parse_dimensions_defaults_missing_to_neutral():
    out = parse_dimensions({"skills": 8.0})  # all other axes missing
    assert out["skills"] == 8.0
    for axis in AXIS_NAMES:
        if axis != "skills":
            assert out[axis] == 5.0


def test_parse_dimensions_clamps_out_of_range():
    out = parse_dimensions({axis: 99.0 for axis in AXIS_NAMES})
    for axis in AXIS_NAMES:
        assert out[axis] == 10.0

    out = parse_dimensions({axis: -3.0 for axis in AXIS_NAMES})
    for axis in AXIS_NAMES:
        assert out[axis] == 0.0


def test_parse_dimensions_handles_non_numeric_and_nan():
    out = parse_dimensions({
        "skills": "high",                    # non-numeric string
        "experience": None,                  # null
        "seniority": float("nan"),           # NaN
        "location": float("inf"),            # inf
        "culture": "7.5",                    # numeric string is OK
        "growth": True,                      # bool coerces to 1.0
        "compensation": 3,                   # int coerces to 3.0
    })
    assert out["skills"] == 5.0  # fell back to neutral
    assert out["experience"] == 5.0
    assert out["seniority"] == 5.0
    assert out["location"] == 5.0
    assert out["culture"] == 7.5
    assert out["growth"] == 1.0
    assert out["compensation"] == 3.0


def test_parse_dimensions_rejects_non_dict():
    import pytest
    with pytest.raises(ValueError):
        parse_dimensions("not a dict")  # type: ignore[arg-type]


def test_weighted_total_matches_settings_weights(settings):
    # All-7s -> total should be ~7.0 (since weights sum to 1.0).
    dims = {axis: 7.0 for axis in AXIS_NAMES}
    assert weighted_total(dims, settings.scoring.weights) == 7.0

    # Mixed: only skills matters (weight 0.35) at 10, rest at 0 -> 3.5.
    dims = {axis: 0.0 for axis in AXIS_NAMES}
    dims["skills"] = 10.0
    assert weighted_total(dims, settings.scoring.weights) == 3.5


# --------------------------------------------------------------- happy path

def test_above_threshold_transitions_to_decided_and_writes_score(settings, conn):
    job = _seed_described(conn, source_job_id="hi-1")
    llm = _FakeLLM({axis: 8.0 for axis in AXIS_NAMES})  # total = 8.0 >= review_min (4)
    worker = _build(settings, conn, llm_client=llm)

    summary = asyncio.run(worker.run_once())

    assert summary.decided == 1
    assert summary.below_bar == 0
    assert summary.failed_closed == 0
    assert summary.attempted == 1
    assert JobRepo(conn).get(job.id).state is JobState.DECIDED

    score = ScoreRepo(conn).get(job.id)
    assert score is not None
    assert score.total == 8.0
    assert set(score.dimensions.keys()) == set(AXIS_NAMES)
    # Model tag stamps prompt version + LLM model so the eval harness can pin.
    assert SCORE_JD.version in score.model


def test_below_threshold_walks_to_skipped_terminal(settings, conn):
    job = _seed_described(conn, source_job_id="lo-1")
    llm = _FakeLLM({axis: 2.0 for axis in AXIS_NAMES})  # total = 2.0 < review_min (4)
    worker = _build(settings, conn, llm_client=llm)

    summary = asyncio.run(worker.run_once())

    assert summary.below_bar == 1
    assert summary.decided == 0
    assert summary.failed_closed == 0
    assert JobRepo(conn).get(job.id).state is JobState.SKIPPED

    # The score row is still written (the audit trail records "we tried + this is
    # what we got"), just with the low total.
    score = ScoreRepo(conn).get(job.id)
    assert score is not None
    assert score.total == 2.0


def test_threshold_boundary_strictly_less_than(settings, conn):
    """``total < review_min`` is the below-bar side. Equality stays at DECIDED."""
    assert settings.scoring.review_min == 4.0
    job = _seed_described(conn, source_job_id="eq-1")
    llm = _FakeLLM({axis: 4.0 for axis in AXIS_NAMES})  # total = 4.0 == review_min
    worker = _build(settings, conn, llm_client=llm)

    summary = asyncio.run(worker.run_once())

    assert summary.decided == 1
    assert summary.below_bar == 0
    assert JobRepo(conn).get(job.id).state is JobState.DECIDED


# --------------------------------------------------------------- fail-closed paths

def test_missing_llm_client_fails_closed_to_skipped(settings, conn):
    """No LLM client -> every job walks DESCRIBED -> SCORED -> DECIDED -> SKIPPED
    with total=0.0. The opposite posture of the filter worker (where missing embed
    fail-opens) — here, fail-OPEN would auto-apply unscored jobs."""
    jobs = [_seed_described(conn, source_job_id=f"nl-{i}") for i in range(3)]
    worker = _build(settings, conn, llm_client=None)

    summary = asyncio.run(worker.run_once())

    assert summary.failed_closed == 3
    assert summary.decided == 0
    assert summary.below_bar == 0
    assert summary.attempted == 3
    for job in jobs:
        assert JobRepo(conn).get(job.id).state is JobState.SKIPPED
        # Audit trail records the attempt with total=0.0.
        score = ScoreRepo(conn).get(job.id)
        assert score is not None and score.total == 0.0
    assert any("no LLM client" in n for n in summary.notes)
    # Model tag stamps "no-llm" so failed-closed rows aren't misattributed.
    score = ScoreRepo(conn).get(jobs[0].id)
    assert score is not None and "no-llm" in score.model


def test_empty_description_fails_closed_per_job(settings, conn):
    """A DESCRIBED row with no JD text has nothing to score; fail-closed so the
    pipeline doesn't surface garbage as a real decision."""
    repo = JobRepo(conn)
    job = Job(source="greenhouse", source_job_id="ed-1", title="Eng", company="X",
              description="", url="https://job-boards.greenhouse.io/x/jobs/ed-1")
    repo.add(job)
    repo.set_state(job.id, JobState.DESCRIBED)

    llm = _FakeLLM({})  # would return all-neutral, but should never be called
    worker = _build(settings, conn, llm_client=llm)

    summary = asyncio.run(worker.run_once())

    assert summary.failed_closed == 1
    assert summary.decided == 0
    assert summary.below_bar == 0
    assert JobRepo(conn).get(job.id).state is JobState.SKIPPED
    # Never called the LLM — the empty-JD guard is before the call.
    assert llm.prompts == []


def test_per_job_llm_exception_isolates_one_job(settings, conn):
    """An LLM exception on job A must NOT prevent job B from being scored. A walks
    DESCRIBED -> ... -> SKIPPED (failed_closed + errors); B sails through normally."""
    job_a = _seed_described(conn, source_job_id="iso-a", title="A", description="alpha alpha")
    job_b = _seed_described(conn, source_job_id="iso-b", title="B", description="beta beta")

    llm = _FakeLLM({axis: 8.0 for axis in AXIS_NAMES})
    llm.raise_for.add("alpha alpha")  # job_a's JD text appears in the prompt

    worker = _build(settings, conn, llm_client=llm)
    summary = asyncio.run(worker.run_once())

    assert summary.errors == 1
    assert summary.failed_closed == 1
    assert summary.decided == 1
    assert summary.below_bar == 0
    assert JobRepo(conn).get(job_a.id).state is JobState.SKIPPED
    assert JobRepo(conn).get(job_b.id).state is JobState.DECIDED


def test_malformed_llm_payload_routes_to_failed_closed(settings, conn):
    """Non-dict reply -> parse_dimensions raises ValueError -> caught upstream as
    a per-job exception -> fail-closed walk."""
    job = _seed_described(conn, source_job_id="mp-1")

    class _BadShapeLLM:
        async def complete_json(self, prompt: str, *, system: str = "") -> Any:
            return ["not", "a", "dict"]  # type: ignore[return-value]

    worker = _build(settings, conn, llm_client=_BadShapeLLM())
    summary = asyncio.run(worker.run_once())

    assert summary.errors == 1
    assert summary.failed_closed == 1
    assert JobRepo(conn).get(job.id).state is JobState.SKIPPED


def test_partial_llm_payload_defaults_missing_to_neutral(settings, conn):
    """Defensive parser: a partial reply doesn't crater the run — missing axes
    default to 5.0 so the weighted total is still computable."""
    job = _seed_described(conn, source_job_id="pp-1")
    # Only 'skills' returned. Total = 0.35 * 10 + 0.65 * 5 = 6.75 (above review_min).
    llm = _FakeLLM({"skills": 10.0})
    worker = _build(settings, conn, llm_client=llm)

    summary = asyncio.run(worker.run_once())

    assert summary.decided == 1
    assert summary.below_bar == 0
    score = ScoreRepo(conn).get(job.id)
    assert score is not None
    assert score.total == 6.75


# --------------------------------------------------------------- bookkeeping

def test_limit_caps_jobs_processed(settings, conn):
    seeded = [_seed_described(conn, source_job_id=f"lim-{i}") for i in range(5)]
    llm = _FakeLLM({axis: 8.0 for axis in AXIS_NAMES})
    worker = _build(settings, conn, llm_client=llm)

    summary = asyncio.run(worker.run_once(limit=2))

    assert summary.attempted == 2
    assert summary.decided == 2
    # The other three stay DESCRIBED.
    remaining = JobRepo(conn).list_by_state(JobState.DESCRIBED)
    assert len(remaining) == 3
    decided = [j.id for j in JobRepo(conn).list_by_state(JobState.DECIDED)]
    assert set(decided) == {seeded[0].id, seeded[1].id}


def test_empty_queue_returns_zero_summary(settings, conn):
    """No DESCRIBED jobs -> no-op summary, no LLM call."""
    llm = _FakeLLM({axis: 8.0 for axis in AXIS_NAMES})
    worker = _build(settings, conn, llm_client=llm)

    summary = asyncio.run(worker.run_once())

    assert isinstance(summary, ScoreRunSummary)
    assert summary.attempted == 0
    assert summary.decided == summary.below_bar == summary.failed_closed == 0
    assert llm.prompts == []


def test_prompt_includes_profile_and_jd(settings, conn):
    """The prompt threads through both the fact-bank summary AND the JD — without
    either, the LLM has nothing to compare. Guards against a wiring regression."""
    _seed_described(conn, source_job_id="pr-1",
                    description="JD-MARKER-XYZ data engineer role")
    llm = _FakeLLM({axis: 8.0 for axis in AXIS_NAMES})
    worker = _build(settings, conn, llm_client=llm)

    asyncio.run(worker.run_once())

    assert len(llm.prompts) == 1
    prompt = llm.prompts[0]
    assert "JD-MARKER-XYZ" in prompt   # JD threaded in
    assert "python" in prompt          # bank-summary skill threaded in
    # System message is the prompt template's system field.
    assert llm.systems[0] == SCORE_JD.system


def test_telemetry_records_skip_for_below_bar(settings, conn, sink):
    """Below-bar jobs surface as 'skip' events with the threshold reason, not 'ok'.
    Same pattern as the filter worker — keeps the event spine honest about
    "this stage decided NOT to advance the job."""
    import json as _json

    job = _seed_described(conn, source_job_id="tel-1")
    llm = _FakeLLM({axis: 2.0 for axis in AXIS_NAMES})  # below review_min
    worker = _build(settings, conn, llm_client=llm)

    asyncio.run(worker.run_once())

    rows = [r for r in sink.recent(limit=20) if r["stage"] == "score"]
    statuses = [r["status"] for r in rows]
    assert "start" in statuses
    assert "skip" in statuses
    skip_row = next(r for r in rows if r["status"] == "skip")
    ctx = _json.loads(skip_row["context_json"] or "{}")
    assert "below review_min" in ctx.get("reason", "")
    # Job is in terminal SKIPPED, not just SCORED (the worker walks the full path).
    assert JobRepo(conn).get(job.id).state is JobState.SKIPPED
