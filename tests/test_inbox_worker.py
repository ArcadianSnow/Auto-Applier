"""InboxWorker end-to-end (email-outcome-loop Phase B) — the key proof.

100% offline: a STUB source yields fixture ``.eml`` bytes (no IMAP), the worker classifies
+ matches + records, and we assert the write lands in the EXISTING outcome path —
``OutcomeRepo.list_by_job`` AND ``compute_conversion_report(applied_with_outcomes())``. No
analytics changes, no IMAP, no creds.

Harness mirrors ``tests/test_discovery.py``: ``settings`` + ``conn`` fixtures only, so
``@stage("inbox")`` is a silent no-op (``get_sink() is None``) — except one test that
installs the real ``sink`` fixture to prove the events spine doesn't blow up.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from click.testing import CliRunner

from auto_applier.analytics import compute_conversion_report
from auto_applier.cli.main import cli
from auto_applier.config.settings import Settings
from auto_applier.db import init_app_db
from auto_applier.db.repositories import JobRepo, OutcomeRepo
from auto_applier.domain.models import Job
from auto_applier.domain.state import JobState, OutcomeKind
from auto_applier.inbox.repo import InboxMessageRepo
from auto_applier.inbox.worker import InboxWorker

_FIXTURES = Path(__file__).parent / "fixtures" / "inbox"


# --------------------------------------------------------------- helpers

def _eml(name: str) -> bytes:
    return (_FIXTURES / name).read_bytes()


def _stub_source(*names: str):
    """A STUB fetcher: yields (uid, raw_bytes) from fixture .eml files. The uid is the
    fixture stem (the same shape eml_file_source / the future IMAP fetcher produce)."""
    for n in names:
        yield (Path(n).stem, _eml(n))


def _seed_applied_acme(conn) -> Job:
    """Seed one APPLIED job that the confirmation.eml fixture matches by URL.

    confirmation.eml's body contains https://boards.greenhouse.io/acme/jobs/12345, so the
    matcher's URL-substring path fires deterministically (no LLM needed)."""
    job = Job(
        source="greenhouse",
        source_job_id="12345",
        title="Senior Data Engineer",
        company="Acme",
        url="https://boards.greenhouse.io/acme/jobs/12345",
        state=JobState.APPLIED,
    )
    JobRepo(conn).add(job)
    return job


class _RaisingLLM:
    """A stub LLM that always raises — proves classify() fail-safes for the fixtures that
    DON'T need it (all deterministic-keyword fixtures classify before the LLM is reached).
    If the worker ever leaned on the LLM for these, this would surface it."""

    async def complete_json(self, *a, **k):
        raise RuntimeError("LLM must not be called for deterministic fixtures")


# --------------------------------------------------------------- (a) confident → outcome

def test_confirmation_records_outcome_and_shows_in_analytics(settings: Settings, conn):
    job = _seed_applied_acme(conn)

    worker = InboxWorker(
        settings=settings, conn=conn, llm=_RaisingLLM(),
        source=_stub_source("confirmation.eml"),
    )
    summary = asyncio.run(worker.run_once())

    assert summary.fetched == 1
    assert summary.outcomes_recorded == 1
    assert summary.routed_to_review == 0
    assert summary.ignored_non_job == 0

    # The real outcome path: an Outcome row exists for the matched job.
    outcomes = OutcomeRepo(conn).list_by_job(job.id)
    assert len(outcomes) == 1
    assert outcomes[0].kind is OutcomeKind.RESPONSE  # "application received" → RESPONSE
    assert "email:deterministic" in outcomes[0].note
    assert "match=url" in outcomes[0].note

    # The end-to-end wiring proof: the EXISTING analytics picks it up unchanged.
    report = compute_conversion_report(OutcomeRepo(conn).applied_with_outcomes())
    assert report.total_applied == 1
    assert report.total_converted == 1            # RESPONSE is a positive conversion
    assert report.overall_rate == 1.0
    assert report.outcome_counts == {"response": 1}

    # The inbox message was recorded as an outcome route.
    msg = conn.execute(
        "SELECT action, matched_job_id, kind FROM inbox_messages"
    ).fetchone()
    assert msg["action"] == "outcome"
    assert msg["matched_job_id"] == job.id
    assert msg["kind"] == "response"


# --------------------------------------------------------------- (b) no match → review

def test_unmatched_rejection_routes_to_review_no_outcome(settings: Settings, conn):
    # Seed ONLY the Acme job; the rejection.eml is from Globex → confident class, no match.
    job = _seed_applied_acme(conn)

    worker = InboxWorker(
        settings=settings, conn=conn, llm=_RaisingLLM(),
        source=_stub_source("rejection.eml"),
    )
    summary = asyncio.run(worker.run_once())

    assert summary.classified == 1
    assert summary.outcomes_recorded == 0
    assert summary.routed_to_review == 1
    assert summary.unmatched == 1

    # No outcome written for the (only) applied job.
    assert OutcomeRepo(conn).list_by_job(job.id) == []
    # ... and conversion analytics still shows the applied job as silent (implicit ghost).
    report = compute_conversion_report(OutcomeRepo(conn).applied_with_outcomes())
    assert report.total_applied == 1
    assert report.total_converted == 0

    review = InboxMessageRepo(conn).list_for_review()
    assert len(review) == 1
    assert review[0].action == "review"
    assert review[0].matched_job_id is None


# --------------------------------------------------------------- (c) newsletter → ignored

def test_newsletter_is_ignored_no_outcome(settings: Settings, conn):
    job = _seed_applied_acme(conn)

    worker = InboxWorker(
        settings=settings, conn=conn, llm=_RaisingLLM(),
        source=_stub_source("newsletter.eml"),
    )
    summary = asyncio.run(worker.run_once())

    assert summary.ignored_non_job == 1
    assert summary.outcomes_recorded == 0
    assert summary.routed_to_review == 0
    assert OutcomeRepo(conn).list_by_job(job.id) == []

    msg = conn.execute("SELECT action FROM inbox_messages").fetchone()
    assert msg["action"] == "ignored"


def test_security_code_is_flagged_and_ignored(settings: Settings, conn):
    """The Greenhouse security-code gate (Direction 3): not an outcome, but flagged so a
    later worker can route it to 'finish assisted'."""
    worker = InboxWorker(
        settings=settings, conn=conn, llm=_RaisingLLM(),
        source=_stub_source("security_code.eml"),
    )
    summary = asyncio.run(worker.run_once())

    assert summary.ignored_non_job == 1
    assert summary.security_code_flags == 1
    assert summary.outcomes_recorded == 0


# --------------------------------------------------------------- (d) idempotency

def test_rerun_is_idempotent_no_duplicate_outcome(settings: Settings, conn):
    job = _seed_applied_acme(conn)

    first = asyncio.run(InboxWorker(
        settings=settings, conn=conn, llm=_RaisingLLM(),
        source=_stub_source("confirmation.eml"),
    ).run_once())
    second = asyncio.run(InboxWorker(
        settings=settings, conn=conn, llm=_RaisingLLM(),
        source=_stub_source("confirmation.eml"),
    ).run_once())

    assert first.outcomes_recorded == 1
    assert second.outcomes_recorded == 0
    assert second.already_processed == 1
    # Exactly ONE outcome despite two runs (message_id dedup).
    assert len(OutcomeRepo(conn).list_by_job(job.id)) == 1


# --------------------------------------------------------------- (e) dry-run writes nothing

def test_dry_run_writes_nothing(settings: Settings, conn):
    job = _seed_applied_acme(conn)

    worker = InboxWorker(
        settings=settings, conn=conn, llm=_RaisingLLM(),
        source=_stub_source("confirmation.eml"),
        record=False,
    )
    summary = asyncio.run(worker.run_once())

    # The summary still reports what WOULD happen...
    assert summary.outcomes_recorded == 1
    # ... but NOTHING was written: no outcome, no inbox_messages row.
    assert OutcomeRepo(conn).list_by_job(job.id) == []
    n = conn.execute("SELECT COUNT(*) AS n FROM inbox_messages").fetchone()["n"]
    assert n == 0


# --------------------------------------------------------------- inert + multi + events

def test_no_source_is_inert(settings: Settings, conn):
    worker = InboxWorker(settings=settings, conn=conn, llm=_RaisingLLM(), source=None)
    summary = asyncio.run(worker.run_once())
    assert summary.fetched == 0
    assert summary.outcomes_recorded == 0
    assert summary.notes and "inert" in summary.notes[0]


def test_mixed_batch_in_one_run(settings: Settings, conn):
    """One run over a confirmation (→outcome), a rejection (→review), and a newsletter
    (→ignored) tallies each bucket correctly."""
    job = _seed_applied_acme(conn)
    worker = InboxWorker(
        settings=settings, conn=conn, llm=_RaisingLLM(),
        source=_stub_source("confirmation.eml", "rejection.eml", "newsletter.eml"),
    )
    summary = asyncio.run(worker.run_once())

    assert summary.fetched == 3
    assert summary.outcomes_recorded == 1
    assert summary.routed_to_review == 1
    assert summary.ignored_non_job == 1
    assert summary.errors == 0
    assert len(OutcomeRepo(conn).list_by_job(job.id)) == 1


def test_events_sink_does_not_blow_up(settings: Settings, conn, sink):
    """With a real EventSink installed (the @stage spine writes), the run still works —
    exercises the start/ok event path like the discover-worker harness does."""
    job = _seed_applied_acme(conn)
    worker = InboxWorker(
        settings=settings, conn=conn, llm=_RaisingLLM(),
        source=_stub_source("confirmation.eml"),
    )
    summary = asyncio.run(worker.run_once())
    assert summary.outcomes_recorded == 1
    assert len(OutcomeRepo(conn).list_by_job(job.id)) == 1


# --------------------------------------------------------------- CLI (offline)

def test_cli_status_exits_zero_without_connecting(settings):
    """`av3 inbox --status` reports config + cursor and never touches IMAP."""
    res = CliRunner().invoke(cli, ["inbox", "--status"])
    assert res.exit_code == 0, res.output
    assert "Inbox config:" in res.output
    assert "enabled   = False" in res.output
    assert "imap.gmail.com:993" in res.output


def test_cli_no_eml_friendly_exit(settings):
    """With no --eml and no live fetcher, print a friendly nudge and exit 0 (never crash)."""
    res = CliRunner().invoke(cli, ["inbox"])
    assert res.exit_code == 0, res.output
    assert "email ingestion not configured yet" in res.output


def test_cli_eml_dry_run_writes_nothing(settings):
    """`av3 inbox --eml confirmation.eml --dry-run` classifies + prints, writes nothing."""
    # Seed the matching APPLIED job so the dry-run would-record path is exercised.
    conn = init_app_db(settings.app_db_path)
    try:
        _seed_applied_acme(conn)
    finally:
        conn.close()

    eml = str(_FIXTURES / "confirmation.eml")
    res = CliRunner().invoke(cli, ["inbox", "--eml", eml, "--dry-run"])
    assert res.exit_code == 0, res.output
    assert "dry-run" in res.output
    assert "outcomes=1" in res.output

    # Nothing persisted.
    conn = init_app_db(settings.app_db_path)
    try:
        assert conn.execute("SELECT COUNT(*) AS n FROM outcomes").fetchone()["n"] == 0
        assert conn.execute("SELECT COUNT(*) AS n FROM inbox_messages").fetchone()["n"] == 0
    finally:
        conn.close()


def test_cli_eml_records_then_review_empty(settings):
    """A confident confirmation records an outcome (real path) and leaves the review
    queue empty; `--review` reports that."""
    conn = init_app_db(settings.app_db_path)
    try:
        job = _seed_applied_acme(conn)
    finally:
        conn.close()

    eml = str(_FIXTURES / "confirmation.eml")
    res = CliRunner().invoke(cli, ["inbox", "--eml", eml])
    assert res.exit_code == 0, res.output
    assert "outcomes=1" in res.output

    conn = init_app_db(settings.app_db_path)
    try:
        assert len(OutcomeRepo(conn).list_by_job(job.id)) == 1
    finally:
        conn.close()

    review = CliRunner().invoke(cli, ["inbox", "--review"])
    assert review.exit_code == 0, review.output
    assert "No messages routed to review." in review.output
