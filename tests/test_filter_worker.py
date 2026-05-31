"""Filter worker (spec §7 #3) — contract tests for the DISCOVERED drain loop.

Mirrors the shape of ``test_apply_worker.py``: we fake the *embedding client*
(deterministic vectors keyed off the embed text) instead of the live Ollama HTTP
path, so these tests stay focused on the worker's contract:

  * cosine >= threshold -> DISCOVERED -> DESCRIBED (``summary.passed``);
  * cosine <  threshold -> DISCOVERED -> FILTERED (``summary.filtered``, StageSkip);
  * missing embed client -> fail-open: every job -> DESCRIBED (``summary.failed_open``);
  * empty bank summary  -> same fail-open posture;
  * empty listing text  -> per-job fail-open;
  * per-job embed error -> isolated, that job alone fail-open, run continues;
  * ``limit`` honored;
  * bank summary is embedded ONCE per run (not per-job), so a 10-job run shouldn't
    rack up 11 bank embeds.

Live Ollama HTTP is covered separately by ``test_apply_worker.py``'s LLM client
constructs and (eventually) a smoke test. Re-testing it here would just couple this
file to the embedder's wire format.
"""

from __future__ import annotations

import asyncio
import sqlite3

from auto_applier.config.settings import Settings
from auto_applier.db.repositories import JobRepo
from auto_applier.domain.models import Job
from auto_applier.domain.state import JobState
from auto_applier.pipeline.filter_worker import (
    FilterRunSummary,
    FilterWorker,
    build_bank_summary,
)
from auto_applier.resume.factbank import Contact, FactBank, WorkEntry


# --------------------------------------------------------------- fakes

class _FakeEmbed:
    """Deterministic embed stub.

    ``vectors`` is a {text: [floats]} table. ``embed`` records every call so tests
    can assert "the bank was embedded exactly once" and "this job query was passed
    through verbatim." Unknown texts return a length-2 zero vector — which cosines to
    0.0 against anything, so an unhandled key always FAILS the threshold (useful for
    "this text wasn't covered by the test" assertions).
    """

    def __init__(self, vectors: dict[str, list[float]]):
        self._vectors = vectors
        self.calls: list[str] = []
        self.raise_for: set[str] = set()

    async def embed(self, text: str) -> list[float]:
        self.calls.append(text)
        if text in self.raise_for:
            raise RuntimeError(f"simulated embed outage for {text!r}")
        return self._vectors.get(text, [0.0, 0.0])


class _BlowupEmbed:
    """An embed client whose bank embed itself raises — drives the bank-failure
    fail-open branch."""

    def __init__(self):
        self.calls: list[str] = []

    async def embed(self, text: str) -> list[float]:
        self.calls.append(text)
        raise RuntimeError("ollama unreachable")


def _bank() -> FactBank:
    """Minimal but non-empty bank so ``build_bank_summary`` returns a non-empty string."""
    return FactBank(
        contact=Contact(name="Pat Doe", email="pat@example.com"),
        skills=["python", "sql", "etl"],
        work_history=[
            WorkEntry(company="Acme", title="Data Engineer", start="2020", end="2023",
                      bullets=["built pipelines"]),
        ],
    )


def _seed_discovered(conn: sqlite3.Connection, *, source_job_id: str,
                     title: str = "Data Engineer", company: str = "BetaCo",
                     description: str = "Python and SQL pipelines.") -> Job:
    """Insert a fresh DISCOVERED row (the worker's input state)."""
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
    return repo.get(job.id)  # type: ignore[return-value]


def _build(
    settings: Settings,
    conn: sqlite3.Connection,
    *,
    embed_client=None,
    threshold: float = 0.6,
    fact_bank: FactBank | None = None,
) -> FilterWorker:
    return FilterWorker(
        settings=settings,
        conn=conn,
        fact_bank=fact_bank if fact_bank is not None else _bank(),
        embed_client=embed_client,
        threshold=threshold,
    )


# --------------------------------------------------------------- bank summary

def test_build_bank_summary_includes_skills_titles_bullets():
    bank = FactBank(
        skills=["python", "sql"],
        work_history=[
            WorkEntry(company="Acme", title="Data Engineer", start="", end="",
                      bullets=["owned ETL"]),
        ],
        certifications=["AWS SAA"],
    )
    summary = build_bank_summary(bank)
    assert "python sql" in summary
    assert "Data Engineer" in summary
    assert "owned ETL" in summary
    assert "AWS SAA" in summary


def test_build_bank_summary_empty_bank_returns_empty_string():
    """Empty bank must surface as an empty string so the worker can route it to the
    fail-open branch (zero-norm cosine would FILTER everything)."""
    assert build_bank_summary(FactBank()) == ""


# --------------------------------------------------------------- happy path

def test_above_threshold_transitions_to_described(settings, conn):
    bank = _bank()
    bank_text = build_bank_summary(bank)
    query = "Data Engineer BetaCo Python and SQL pipelines."
    embed = _FakeEmbed({bank_text: [1.0, 0.0], query: [1.0, 0.0]})  # cosine 1.0
    job = _seed_discovered(conn, source_job_id="hi-1")
    worker = _build(settings, conn, embed_client=embed, fact_bank=bank, threshold=0.6)

    summary = asyncio.run(worker.run_once())

    assert summary.passed == 1
    assert summary.filtered == 0
    assert summary.failed_open == 0
    assert summary.attempted == 1
    assert JobRepo(conn).get(job.id).state is JobState.DESCRIBED


def test_below_threshold_transitions_to_filtered_terminal(settings, conn):
    bank = _bank()
    bank_text = build_bank_summary(bank)
    query = "Data Engineer BetaCo Python and SQL pipelines."
    # Orthogonal vectors -> cosine 0.0 -> below default threshold (0.6) -> FILTERED.
    embed = _FakeEmbed({bank_text: [1.0, 0.0], query: [0.0, 1.0]})
    job = _seed_discovered(conn, source_job_id="lo-1")
    worker = _build(settings, conn, embed_client=embed, fact_bank=bank, threshold=0.6)

    summary = asyncio.run(worker.run_once())

    assert summary.filtered == 1
    assert summary.passed == 0
    assert summary.failed_open == 0
    assert JobRepo(conn).get(job.id).state is JobState.FILTERED


def test_threshold_boundary_inclusive(settings, conn):
    """``sim >= threshold`` is the contract — equality passes."""
    bank = _bank()
    bank_text = build_bank_summary(bank)
    query = "Data Engineer BetaCo Python and SQL pipelines."
    # cosine([1,1], [1,0]) = 1/sqrt(2) ≈ 0.7071 — comfortably above 0.7071-epsilon
    embed = _FakeEmbed({bank_text: [1.0, 1.0], query: [1.0, 0.0]})
    job = _seed_discovered(conn, source_job_id="eq-1")
    worker = _build(settings, conn, embed_client=embed, fact_bank=bank, threshold=0.70)

    summary = asyncio.run(worker.run_once())

    assert summary.passed == 1
    assert JobRepo(conn).get(job.id).state is JobState.DESCRIBED


# --------------------------------------------------------------- fail-open paths

def test_missing_embed_client_routes_all_to_described(settings, conn):
    """No embed client configured -> every DISCOVERED job fail-opens to DESCRIBED,
    bookkept in ``failed_open`` (not ``passed``) so the dashboard can tell."""
    jobs = [_seed_discovered(conn, source_job_id=f"f-{i}") for i in range(3)]
    worker = _build(settings, conn, embed_client=None)

    summary = asyncio.run(worker.run_once())

    assert summary.failed_open == 3
    assert summary.passed == 0
    assert summary.filtered == 0
    assert summary.attempted == 3
    for job in jobs:
        assert JobRepo(conn).get(job.id).state is JobState.DESCRIBED
    assert any("no embed client" in n for n in summary.notes)


def test_empty_bank_routes_all_to_described(settings, conn):
    """Empty fact bank -> cosine against an empty anchor would FILTER everything.
    The fail-open path catches it before any embed call fires."""
    bank = FactBank(contact=Contact(name="Pat"))  # no skills, no work, no edu
    embed = _FakeEmbed({})  # never called, but mustn't be None
    job = _seed_discovered(conn, source_job_id="emp-1")
    worker = _build(settings, conn, embed_client=embed, fact_bank=bank)

    summary = asyncio.run(worker.run_once())

    assert summary.failed_open == 1
    assert summary.passed == 0
    assert JobRepo(conn).get(job.id).state is JobState.DESCRIBED
    assert any("empty fact-bank summary" in n for n in summary.notes)
    # And critically: no embed calls at all on the empty-bank fail-open path.
    assert embed.calls == []


def test_empty_listing_text_per_job_fail_opens(settings, conn):
    """A DISCOVERED row with no title/company/description has nothing to embed;
    fail-open to DESCRIBED so the describe stage can scrape the JD properly."""
    bank = _bank()
    bank_text = build_bank_summary(bank)
    embed = _FakeEmbed({bank_text: [1.0, 0.0]})

    repo = JobRepo(conn)
    job = Job(source="greenhouse", source_job_id="blank-1", title="", company="",
              description="", url="https://job-boards.greenhouse.io/x/jobs/blank-1")
    repo.add(job)

    worker = _build(settings, conn, embed_client=embed, fact_bank=bank)
    summary = asyncio.run(worker.run_once())

    assert summary.failed_open == 1
    assert summary.passed == 0
    assert summary.filtered == 0
    assert JobRepo(conn).get(job.id).state is JobState.DESCRIBED
    # Bank embed fired once; the empty job was skipped before the per-job embed call.
    assert embed.calls == [bank_text]


def test_bank_embed_exception_routes_all_to_described(settings, conn):
    """If embedding the bank itself raises (Ollama down at start), every job in this
    run fail-opens — the alternative would be silently FILTER-ing the whole DB."""
    bank = _bank()
    embed = _BlowupEmbed()
    jobs = [_seed_discovered(conn, source_job_id=f"bb-{i}") for i in range(2)]
    worker = _build(settings, conn, embed_client=embed, fact_bank=bank)

    summary = asyncio.run(worker.run_once())

    assert summary.failed_open == 2
    assert summary.passed == 0
    assert summary.filtered == 0
    for job in jobs:
        assert JobRepo(conn).get(job.id).state is JobState.DESCRIBED
    assert any("bank embed failed" in n for n in summary.notes)


def test_per_job_embed_error_isolates_one_job(settings, conn):
    """A per-job embed exception fail-opens that job (failed_open + errors) but the
    rest of the run keeps going. Mirrors apply_worker's per-job isolation contract."""
    bank = _bank()
    bank_text = build_bank_summary(bank)

    job_a = _seed_discovered(conn, source_job_id="iso-a", title="A", company="ACo",
                             description="alpha")
    job_b = _seed_discovered(conn, source_job_id="iso-b", title="B", company="BCo",
                             description="beta")
    query_a = "A ACo alpha"
    query_b = "B BCo beta"

    embed = _FakeEmbed({bank_text: [1.0, 0.0], query_b: [1.0, 0.0]})
    embed.raise_for.add(query_a)  # job_a's query raises; job_b sails through

    worker = _build(settings, conn, embed_client=embed, fact_bank=bank, threshold=0.5)
    summary = asyncio.run(worker.run_once())

    assert summary.errors == 1
    assert summary.failed_open == 1
    assert summary.passed == 1
    assert summary.filtered == 0
    assert JobRepo(conn).get(job_a.id).state is JobState.DESCRIBED  # fail-open
    assert JobRepo(conn).get(job_b.id).state is JobState.DESCRIBED  # cosine pass


# --------------------------------------------------------------- bookkeeping

def test_limit_caps_jobs_processed(settings, conn):
    bank = _bank()
    bank_text = build_bank_summary(bank)
    jobs = [
        _seed_discovered(conn, source_job_id=f"lim-{i}", title=f"T{i}",
                         description="match")
        for i in range(5)
    ]
    # Map every job's query text to a high-cosine vector so they'd all pass without
    # the limit; the assertion is that limit=2 stops after 2.
    vectors = {bank_text: [1.0, 0.0]}
    for i in range(5):
        vectors[f"T{i} BetaCo match"] = [1.0, 0.0]
    embed = _FakeEmbed(vectors)
    worker = _build(settings, conn, embed_client=embed, fact_bank=bank, threshold=0.5)

    summary = asyncio.run(worker.run_once(limit=2))

    assert summary.attempted == 2
    assert summary.passed == 2
    # The other three stay DISCOVERED.
    discovered_after = JobRepo(conn).list_by_state(JobState.DISCOVERED)
    assert len(discovered_after) == 3
    # Sanity: only the first two jobs (oldest by discovered_at) transitioned.
    described = [j.id for j in JobRepo(conn).list_by_state(JobState.DESCRIBED)]
    assert set(described) == {jobs[0].id, jobs[1].id}


def test_bank_summary_embedded_once_per_run(settings, conn):
    """Bank text is per-user, not per-job — embedding it inside the loop would just
    heat embed caches without changing the result. The bank embed must fire ONCE per
    run regardless of how many DISCOVERED jobs are processed."""
    bank = _bank()
    bank_text = build_bank_summary(bank)
    job_count = 4
    vectors = {bank_text: [1.0, 0.0]}
    queries = []
    for i in range(job_count):
        title = f"Role{i}"
        company = "BetaCo"
        desc = f"snippet {i}"
        _seed_discovered(conn, source_job_id=f"once-{i}", title=title,
                         company=company, description=desc)
        q = f"{title} {company} {desc}"
        queries.append(q)
        vectors[q] = [1.0, 0.0]

    embed = _FakeEmbed(vectors)
    worker = _build(settings, conn, embed_client=embed, fact_bank=bank, threshold=0.5)
    summary = asyncio.run(worker.run_once())

    assert summary.passed == job_count
    # bank_text appears exactly once in the call log; each per-job query appears once.
    assert embed.calls.count(bank_text) == 1
    for q in queries:
        assert embed.calls.count(q) == 1


def test_empty_queue_returns_zero_summary(settings, conn):
    """No DISCOVERED jobs -> the worker still constructs cleanly, returns an empty
    summary, and never even embeds the bank (lazy-init guard)."""
    bank = _bank()
    embed = _FakeEmbed({build_bank_summary(bank): [1.0, 0.0]})
    worker = _build(settings, conn, embed_client=embed, fact_bank=bank)

    summary = asyncio.run(worker.run_once())

    assert isinstance(summary, FilterRunSummary)
    assert summary.attempted == 0
    assert summary.passed == summary.filtered == summary.failed_open == 0
    # No queue = no need to embed the bank either; the lazy-init shouldn't fire.
    assert embed.calls == []


def test_telemetry_skip_event_emitted_on_below_threshold(settings, conn, sink):
    """The below-threshold path raises StageSkip so the event spine records a 'skip'
    row (with the reason), not an 'ok'. This is the canonical 'legitimate early-exit'
    shape called out in stage.py's StageSkip docstring."""
    import json as _json

    bank = _bank()
    bank_text = build_bank_summary(bank)
    query = "Data Engineer BetaCo Python and SQL pipelines."
    embed = _FakeEmbed({bank_text: [1.0, 0.0], query: [0.0, 1.0]})
    _seed_discovered(conn, source_job_id="tel-1")
    worker = _build(settings, conn, embed_client=embed, fact_bank=bank, threshold=0.6)

    asyncio.run(worker.run_once())

    rows = [r for r in sink.recent(limit=20) if r["stage"] == "filter"]
    statuses = [r["status"] for r in rows]
    assert "start" in statuses
    assert "skip" in statuses
    skip_row = next(r for r in rows if r["status"] == "skip")
    ctx = _json.loads(skip_row["context_json"] or "{}")
    assert "below threshold" in ctx.get("reason", "")
