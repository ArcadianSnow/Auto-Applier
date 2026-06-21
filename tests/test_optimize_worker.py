"""Optimize worker (spec section 7 #6) - contract tests for the DECIDED drain loop.

Same fake-the-client pattern as test_filter_worker.py / test_score_worker.py: we
inject a deterministic CompletionClient stub and an injectable PDF renderer so
these tests focus on the worker's contract, not the LLM wire format or Playwright.

Coverage:
  * all gates clean -> DECIDED -> QUEUED_APPLY (``summary.queued``);
  * missing LLM client -> every DECIDED job fail-closes to REVIEW
    (``failed_closed`` bookkept separately from content-driven ``guard_rejected``);
  * per-job LLM exception on resume gen -> isolated, that job to REVIEW;
  * per-job LLM exception on cover gen -> isolated, that job to REVIEW;
  * fabrication guard returns HARD_FAIL -> that job to REVIEW (``guard_rejected``);
  * fabrication guard returns REVIEW    -> that job to REVIEW (``guard_rejected``);
  * PDF renderer returns False          -> that job to REVIEW (``render_failed``);
  * malformed LLM payload (non-dict)    -> that job to REVIEW (``failed_closed``);
  * empty cover letter body             -> that job to REVIEW (``failed_closed``);
  * ``limit`` honored, oldest-first;
  * artifacts written to the canonical paths derived from job.id;
  * telemetry: routed-to-review path raises StageSkip so the event spine records
    'skip' with the reason, not 'ok'.
"""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

from auto_applier.config.settings import Settings
from auto_applier.db.repositories import JobRepo
from auto_applier.domain.models import Job
from auto_applier.domain.state import JobState
from auto_applier.llm.prompts import GENERATE_COVER_LETTER, GENERATE_RESUME
from auto_applier.pipeline.optimize_worker import (
    OptimizeRunSummary,
    OptimizeWorker,
)
from auto_applier.resume.factbank import Contact, FactBank, WorkEntry
from auto_applier.resume.generate import (
    generated_cover_letter_path,
    generated_resume_path,
)


# --------------------------------------------------------------- fakes

class _FakeLLM:
    """Returns prompts-routed payloads. Dispatches by which prompt's system field
    the call carries (resume vs cover), so a single fake serves both generators.

    ``resume_payload`` and ``cover_payload`` default to a valid happy-path shape so
    most tests can construct ``_FakeLLM()`` and only override one side.
    """

    _HAPPY_RESUME: dict = {
        "summary": "Pythonic data engineer with ETL ownership at Acme.",
        "skills": ["python", "sql", "etl"],
        "work": [
            {
                "company": "Acme",
                "title": "Data Engineer",
                "start": "2020",
                "end": "2023",
                "bullets": ["Built pipelines", "Owned ETL"],
            }
        ],
        "education": [],
    }
    _HAPPY_COVER: dict = {
        "body": (
            "I am a data engineer with three years at Acme building production "
            "ETL.\n\nMy work on the bank's ingestion stack maps directly to your "
            "JD's Python and SQL focus.\n\nHappy to discuss further."
        )
    }

    def __init__(
        self,
        *,
        resume_payload: dict | None = None,
        cover_payload: dict | None = None,
    ):
        self._resume_payload = (
            dict(resume_payload) if resume_payload is not None else dict(self._HAPPY_RESUME)
        )
        self._cover_payload = (
            dict(cover_payload) if cover_payload is not None else dict(self._HAPPY_COVER)
        )
        self.calls: list[tuple[str, str]] = []  # (kind, prompt)
        self.raise_resume: list[Exception] = []
        self.raise_cover: list[Exception] = []

    async def complete_json(self, prompt: str, *, system: str = "") -> dict:
        # Route by which template's system field was passed. The score worker
        # makes the same kind of routing decision - this is just one fake serving
        # two prompts.
        if system == GENERATE_RESUME.system:
            self.calls.append(("resume", prompt))
            if self.raise_resume:
                raise self.raise_resume.pop(0)
            return dict(self._resume_payload)
        if system == GENERATE_COVER_LETTER.system:
            self.calls.append(("cover", prompt))
            if self.raise_cover:
                raise self.raise_cover.pop(0)
            return dict(self._cover_payload)
        raise AssertionError(f"unexpected system prompt: {system[:80]!r}")


class _StubPdfRenderer:
    """Writes a single-byte marker file at the requested path (so the test can
    later assert the path exists) and returns ``True``. Tests that need a render
    failure pass ``returns=False`` and observe the empty path.
    """

    def __init__(self, returns: bool = True):
        self._returns = returns
        self.calls: list[tuple[str, Path]] = []

    async def __call__(self, html: str, out_path: Path) -> bool:
        self.calls.append((html, out_path))
        if self._returns:
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_bytes(b"\x00")  # marker
        return self._returns


# --------------------------------------------------------------- helpers

def _bank() -> FactBank:
    """Bank that aligns with the happy-path resume payload so the guard passes
    out of the box. Every claim in ``_HAPPY_RESUME`` traces back to this bank.
    """
    return FactBank(
        contact=Contact(name="Pat Doe", email="pat@example.com",
                        location="Remote", links={"LinkedIn": "https://example.com/pat"}),
        skills=["python", "sql", "etl"],
        work_history=[
            WorkEntry(
                company="Acme",
                title="Data Engineer",
                start="2020",
                end="2023",
                bullets=["Built pipelines", "Owned ETL"],
            )
        ],
        allowed_metrics=[],  # happy resume has no $/% metrics; guard passes
    )


def _seed_decided(
    conn: sqlite3.Connection,
    *,
    source_job_id: str,
    title: str = "Senior Data Engineer",
    company: str = "BetaCo",
    description: str = "We need a Python + SQL data engineer to own ETL pipelines.",
) -> Job:
    """Insert a job at DECIDED (post-score) via the canonical state walk."""
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
    repo.set_state(job.id, JobState.SCORED)
    repo.set_state(job.id, JobState.DECIDED)
    return repo.get(job.id)  # type: ignore[return-value]


def _build(
    settings: Settings,
    conn: sqlite3.Connection,
    *,
    llm: _FakeLLM | None = None,
    pdf_renderer: _StubPdfRenderer | None = None,
    fact_bank: FactBank | None = None,
) -> OptimizeWorker:
    return OptimizeWorker(
        settings=settings,
        conn=conn,
        fact_bank=fact_bank if fact_bank is not None else _bank(),
        llm_client=llm,
        pdf_renderer=pdf_renderer or _StubPdfRenderer(),
    )


# --------------------------------------------------------------- happy path

def test_all_gates_clean_transitions_to_queued_apply(settings, conn):
    job = _seed_decided(conn, source_job_id="hp-1")
    llm = _FakeLLM()
    renderer = _StubPdfRenderer()
    worker = _build(settings, conn, llm=llm, pdf_renderer=renderer)

    summary = asyncio.run(worker.run_once())

    assert summary.queued == 1
    assert summary.routed_to_review == 0
    assert summary.guard_rejected == 0
    assert summary.render_failed == 0
    assert summary.failed_closed == 0
    assert summary.attempted == 1
    assert JobRepo(conn).get(job.id).state is JobState.QUEUED_APPLY

    # Artifacts written to the canonical paths.
    pdf_path = generated_resume_path(settings, job.id)
    cover_path = generated_cover_letter_path(settings, job.id)
    assert pdf_path.exists()
    assert cover_path.exists()
    assert cover_path.read_text(encoding="utf-8").startswith("I am a data engineer")

    # Renderer received the canonical PDF path - guards against a wiring regression.
    assert len(renderer.calls) == 1
    assert renderer.calls[0][1] == pdf_path


def test_prompt_threads_jd_and_bank_facts(settings, conn):
    """The résumé prompt must contain both the bank facts AND the JD; the cover
    prompt must contain the JD + company + title. Guards against a wiring drift."""
    _seed_decided(conn, source_job_id="pt-1",
                  description="JD-MARKER-XYZ ETL pipelines please")
    llm = _FakeLLM()
    worker = _build(settings, conn, llm=llm)

    asyncio.run(worker.run_once())

    resume_calls = [c for c in llm.calls if c[0] == "resume"]
    cover_calls = [c for c in llm.calls if c[0] == "cover"]
    assert len(resume_calls) == 1
    assert len(cover_calls) == 1

    # Bank facts and JD are both in the resume prompt.
    assert "JD-MARKER-XYZ" in resume_calls[0][1]
    assert "Acme" in resume_calls[0][1]  # bank company
    assert "python" in resume_calls[0][1]  # bank skill

    # JD + company + title in the cover prompt.
    assert "JD-MARKER-XYZ" in cover_calls[0][1]
    assert "BetaCo" in cover_calls[0][1]
    assert "Senior Data Engineer" in cover_calls[0][1]


# --------------------------------------------------------------- fail-closed: no LLM

def test_missing_llm_client_fails_closed_to_review(settings, conn):
    """No LLM client -> every DECIDED job routes to REVIEW with failed_closed
    bookkept. The opposite posture of the filter worker (which fail-OPENs on
    missing embed) - here, fail-open would advance unverified résumés to
    QUEUED_APPLY, which is the safety hole the Strict gate exists to close."""
    jobs = [_seed_decided(conn, source_job_id=f"nl-{i}") for i in range(3)]
    worker = _build(settings, conn, llm=None)

    summary = asyncio.run(worker.run_once())

    assert summary.failed_closed == 3
    assert summary.routed_to_review == 3
    assert summary.queued == 0
    assert summary.guard_rejected == 0
    assert summary.attempted == 3
    for job in jobs:
        assert JobRepo(conn).get(job.id).state is JobState.REVIEW
    assert any("no LLM client" in n for n in summary.notes)


# --------------------------------------------------------------- fail-closed: per-job LLM exceptions

def test_resume_llm_exception_isolates_one_job(settings, conn):
    """An LLM exception on job A's resume gen must NOT prevent job B from being
    optimized. A routes to REVIEW (failed_closed + errors); B sails through."""
    job_a = _seed_decided(conn, source_job_id="iso-a", title="A",
                          description="alpha alpha")
    job_b = _seed_decided(conn, source_job_id="iso-b", title="B",
                          description="beta beta")

    llm = _FakeLLM()
    # Job A is processed first (oldest-by-discovered_at); its resume gen raises.
    llm.raise_resume.append(RuntimeError("simulated resume LLM outage"))

    worker = _build(settings, conn, llm=llm)
    summary = asyncio.run(worker.run_once())

    assert summary.errors == 1
    assert summary.failed_closed == 1
    assert summary.routed_to_review == 1
    assert summary.queued == 1
    assert JobRepo(conn).get(job_a.id).state is JobState.REVIEW
    assert JobRepo(conn).get(job_b.id).state is JobState.QUEUED_APPLY


def test_cover_llm_exception_isolates_one_job(settings, conn):
    """Same as above but the exception fires on the cover-letter call. Resume
    succeeded - but the Strict gate requires both, so the job still routes to
    REVIEW (and no PDF should be written for it)."""
    job_a = _seed_decided(conn, source_job_id="cov-a")
    job_b = _seed_decided(conn, source_job_id="cov-b")

    llm = _FakeLLM()
    llm.raise_cover.append(RuntimeError("simulated cover LLM outage"))

    renderer = _StubPdfRenderer()
    worker = _build(settings, conn, llm=llm, pdf_renderer=renderer)
    summary = asyncio.run(worker.run_once())

    assert summary.errors == 1
    assert summary.failed_closed == 1
    assert summary.queued == 1
    assert JobRepo(conn).get(job_a.id).state is JobState.REVIEW
    assert JobRepo(conn).get(job_b.id).state is JobState.QUEUED_APPLY

    # Critical: no PDF for the failed job (the cover-letter failure happens
    # BEFORE rendering, so the renderer is never called for A).
    assert not generated_resume_path(settings, job_a.id).exists()
    assert generated_resume_path(settings, job_b.id).exists()


def test_malformed_resume_payload_routes_to_failed_closed(settings, conn):
    """Non-dict resume reply -> parse raises ValueError -> caught upstream -> REVIEW."""
    job = _seed_decided(conn, source_job_id="mp-1")

    class _BadShapeLLM:
        async def complete_json(self, prompt: str, *, system: str = "") -> object:
            return ["not", "a", "dict"]  # type: ignore[return-value]

    worker = _build(settings, conn, llm=_BadShapeLLM())  # type: ignore[arg-type]
    summary = asyncio.run(worker.run_once())

    assert summary.errors == 1
    assert summary.failed_closed == 1
    assert JobRepo(conn).get(job.id).state is JobState.REVIEW


def test_empty_cover_body_routes_to_failed_closed(settings, conn):
    """A {"body": ""} reply is structurally indistinguishable from a generation
    failure - parse raises so the gate fails closed."""
    job = _seed_decided(conn, source_job_id="ec-1")
    llm = _FakeLLM(cover_payload={"body": ""})
    worker = _build(settings, conn, llm=llm)

    summary = asyncio.run(worker.run_once())

    assert summary.errors == 1
    assert summary.failed_closed == 1
    assert JobRepo(conn).get(job.id).state is JobState.REVIEW


# --------------------------------------------------------------- fail-closed: empty JD

def test_empty_description_fails_closed_per_job(settings, conn):
    """A DECIDED row with no JD text would have nothing to optimize against - the
    score worker doesn't normally produce these (it fail-closes empty JDs at
    SCORED), but defend the gate anyway."""
    repo = JobRepo(conn)
    job = Job(source="greenhouse", source_job_id="ed-1", title="Eng", company="X",
              description="", url="https://job-boards.greenhouse.io/x/jobs/ed-1")
    repo.add(job)
    repo.set_state(job.id, JobState.DESCRIBED)
    repo.set_state(job.id, JobState.SCORED)
    repo.set_state(job.id, JobState.DECIDED)

    llm = _FakeLLM()  # should never be called
    worker = _build(settings, conn, llm=llm)

    summary = asyncio.run(worker.run_once())

    assert summary.failed_closed == 1
    assert summary.routed_to_review == 1
    assert summary.queued == 0
    assert JobRepo(conn).get(job.id).state is JobState.REVIEW
    # Never called the LLM - the empty-JD guard is before the call.
    assert llm.calls == []


# --------------------------------------------------------------- guard-driven REVIEW

def test_guard_hard_fail_routes_to_review(settings, conn):
    """The résumé invents a company not in the bank. Guard returns HARD_FAIL ->
    job to REVIEW with ``guard_rejected``. The ``failed_closed`` counter stays
    zero - a guard rejection is content-driven, not operational."""
    job = _seed_decided(conn, source_job_id="gh-1")
    llm = _FakeLLM(resume_payload={
        "summary": "Made-up summary",
        "skills": ["python"],
        "work": [
            {
                "company": "Fabricated Co",  # NOT in the bank
                "title": "Data Engineer",
                "start": "2020",
                "end": "2023",
                "bullets": ["Invented work"],
            }
        ],
        "education": [],
    })
    worker = _build(settings, conn, llm=llm)

    summary = asyncio.run(worker.run_once())

    assert summary.guard_rejected == 1
    assert summary.routed_to_review == 1
    assert summary.failed_closed == 0  # operational counter stays clean
    assert summary.queued == 0
    assert summary.errors == 0
    assert JobRepo(conn).get(job.id).state is JobState.REVIEW
    # No PDF written - we gate render behind the guard pass.
    assert not generated_resume_path(settings, job.id).exists()


def test_guard_review_verdict_also_routes_to_review(settings, conn):
    """A REVIEW-only verdict (no HARD_FAIL findings, but at least one REVIEW
    finding) must also block QUEUED_APPLY - the Strict gate is fail-CLOSED on
    anything less than PASS."""
    job = _seed_decided(conn, source_job_id="gr-1")
    # Title that's near-miss to the bank ("Data Engineer") triggers REVIEW, not
    # HARD_FAIL, on the guard's title check.
    llm = _FakeLLM(resume_payload={
        "summary": "Summary",
        "skills": ["python"],
        "work": [
            {
                "company": "Acme",
                "title": "Quantum Sorcerer",  # very different from bank title -> REVIEW
                "start": "2020",
                "end": "2023",
                "bullets": ["did work"],
            }
        ],
        "education": [],
    })
    worker = _build(settings, conn, llm=llm)

    summary = asyncio.run(worker.run_once())

    assert summary.guard_rejected == 1
    assert summary.routed_to_review == 1
    assert summary.queued == 0
    assert JobRepo(conn).get(job.id).state is JobState.REVIEW


def test_fabricated_cover_letter_routes_to_review(settings, conn):
    """The résumé passes the guard but the COVER LETTER claims a stack the bank
    never mentions (the live 2026-06-11 failure: a Kubernetes/Terraform letter
    for a SQL Server DBA). The cover-letter prose check must block QUEUED_APPLY
    and leave no artifacts behind."""
    job = _seed_decided(conn, source_job_id="cl-1")
    llm = _FakeLLM(cover_payload={
        "body": (
            "I led zero-downtime Kubernetes migrations and designed Terraform "
            "modules across multi-cloud environments."
        )
    })
    worker = _build(settings, conn, llm=llm)

    summary = asyncio.run(worker.run_once())

    assert summary.guard_rejected == 1
    assert summary.routed_to_review == 1
    assert summary.queued == 0
    assert JobRepo(conn).get(job.id).state is JobState.REVIEW
    # Neither artifact lands: render is gated behind BOTH guards.
    assert not generated_resume_path(settings, job.id).exists()
    from auto_applier.resume.generate import generated_cover_letter_path
    assert not generated_cover_letter_path(settings, job.id).exists()


# --------------------------------------------------------------- render failure

def test_render_failure_routes_to_review(settings, conn):
    """The PDF renderer returns False (e.g. Playwright not installed) -> that job
    routes to REVIEW with ``render_failed``. Guard and generation succeeded; the
    failure is at the materialization step."""
    job = _seed_decided(conn, source_job_id="rf-1")
    llm = _FakeLLM()
    renderer = _StubPdfRenderer(returns=False)
    worker = _build(settings, conn, llm=llm, pdf_renderer=renderer)

    summary = asyncio.run(worker.run_once())

    assert summary.render_failed == 1
    assert summary.routed_to_review == 1
    assert summary.failed_closed == 0
    assert summary.queued == 0
    assert JobRepo(conn).get(job.id).state is JobState.REVIEW
    # Renderer was called once (the failure path) but produced no file.
    assert len(renderer.calls) == 1
    assert not generated_resume_path(settings, job.id).exists()
    # And critically: no orphan cover letter either (we gate the cover-letter
    # write BEHIND the PDF render so a render failure leaves no half-state).
    assert not generated_cover_letter_path(settings, job.id).exists()


# --------------------------------------------------------------- bookkeeping

def test_limit_caps_jobs_processed(settings, conn):
    seeded = [_seed_decided(conn, source_job_id=f"lim-{i}") for i in range(5)]
    llm = _FakeLLM()
    worker = _build(settings, conn, llm=llm)

    summary = asyncio.run(worker.run_once(limit=2))

    assert summary.attempted == 2
    assert summary.queued == 2
    # The other three stay DECIDED.
    remaining = JobRepo(conn).list_by_state(JobState.DECIDED)
    assert len(remaining) == 3
    queued = [j.id for j in JobRepo(conn).list_by_state(JobState.QUEUED_APPLY)]
    assert set(queued) == {seeded[0].id, seeded[1].id}


def test_empty_queue_returns_zero_summary(settings, conn):
    """No DECIDED jobs -> no-op summary, no LLM call."""
    llm = _FakeLLM()
    worker = _build(settings, conn, llm=llm)

    summary = asyncio.run(worker.run_once())

    assert isinstance(summary, OptimizeRunSummary)
    assert summary.attempted == 0
    assert summary.queued == summary.routed_to_review == 0
    assert summary.failed_closed == summary.errors == 0
    assert llm.calls == []


# --------------------------------------------------------------- telemetry

def test_telemetry_records_skip_for_routed_to_review(settings, conn, sink):
    """Routed-to-REVIEW jobs surface as 'skip' events with the reason, not 'ok'.
    Same pattern as filter/score - keeps the event spine honest about 'this
    stage decided NOT to advance the job'."""
    import json as _json

    job = _seed_decided(conn, source_job_id="tel-1")
    # Hard-fail the guard so the routed-to-review path fires.
    llm = _FakeLLM(resume_payload={
        "summary": "summary",
        "skills": ["python"],
        "work": [{
            "company": "Fabricated Co",  # not in bank
            "title": "Data Engineer",
            "start": "2020",
            "end": "2023",
            "bullets": ["bullet"],
        }],
        "education": [],
    })
    worker = _build(settings, conn, llm=llm)

    asyncio.run(worker.run_once())

    rows = [r for r in sink.recent(limit=20) if r["stage"] == "optimize"]
    statuses = [r["status"] for r in rows]
    assert "start" in statuses
    assert "skip" in statuses
    skip_row = next(r for r in rows if r["status"] == "skip")
    ctx = _json.loads(skip_row["context_json"] or "{}")
    assert "guard" in ctx.get("reason", "")
    assert JobRepo(conn).get(job.id).state is JobState.REVIEW


def test_telemetry_records_ok_for_queued(settings, conn, sink):
    """Successful queue path surfaces as 'ok' on the event spine, not 'skip'."""
    _seed_decided(conn, source_job_id="ok-1")
    llm = _FakeLLM()
    worker = _build(settings, conn, llm=llm)

    asyncio.run(worker.run_once())

    rows = [r for r in sink.recent(limit=20) if r["stage"] == "optimize"]
    statuses = [r["status"] for r in rows]
    assert "start" in statuses
    assert "ok" in statuses
    assert "skip" not in statuses


# --------------------------------------------------------------- path derivation

def test_canonical_paths_derive_from_job_id(settings):
    """Path helpers - both workers (optimize writes, apply reads) derive the same
    canonical paths from job.id. No DB column added: the file's existence is the
    durable 'this job has been optimized' contract."""
    jid = "abc123"
    pdf = generated_resume_path(settings, jid)
    cover = generated_cover_letter_path(settings, jid)
    assert pdf.name == "abc123.pdf"
    assert cover.name == "abc123_cover.txt"
    assert pdf.parent == cover.parent  # both in the same per-job subdir-less folder
    assert pdf.parent == settings.artifacts_dir / "generated"


def test_artifact_names_are_human_readable_with_job_and_bank(settings, conn):
    """With a seeded job + a fact-bank name, the on-disk filename is readable, not a bare UUID,
    while staying deterministic from job.id (both workers derive the same path)."""
    from auto_applier.web.onboarding import save_fact_bank

    job = _seed_decided(conn, source_job_id="rd-1", company="Acme", title="Data Engineer")
    conn.commit()
    save_fact_bank(settings.data_dir, FactBank(contact=Contact(name="Jane Doe", email="j@x")))

    pdf = generated_resume_path(settings, job.id)
    cover = generated_cover_letter_path(settings, job.id)
    assert pdf.name == f"Jane_Doe_Resume_Acme_Data_Engineer_{job.id[:8]}.pdf"
    assert cover.name == f"Jane_Doe_Cover_Acme_Data_Engineer_{job.id[:8]}.txt"
    # Deterministic: a second derivation (what the apply worker does) matches.
    assert generated_resume_path(settings, job.id) == pdf
