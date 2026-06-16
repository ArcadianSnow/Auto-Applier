"""Inbox outcome worker — drives the EXISTING outcome ladder from email (email-outcome-loop
Phase B, research/future-directions.md Direction 4).

Mirrors :class:`auto_applier.pipeline.discover_worker.DiscoverWorker`: ``source_name``,
``__init__(*, settings, conn, ...)`` with injectable deps, ``async run_once() -> InboxRunSummary``,
and a per-message unit wrapped in ``@stage("inbox")`` so the observability spine emits
start/ok/error/skip for free.

The whole point of Phase B is to prove the end-to-end write into the EXISTING outcome
path with **zero new analytics** and **no IMAP**: a confident classification + match calls
exactly ``OutcomeRepo(conn).add(Outcome(job_id, kind, note))`` — the same one line
``av3 outcome`` uses — and ``compute_conversion_report(applied_with_outcomes())`` picks it
up unchanged.

  source (uid, raw_bytes)  ──►  parse_message  ──►  is_processed? ─yes─► already_processed
        │                                                    │ no
        │  (STUB in tests / eml files in CLI / IMAP later)   ▼
        │                                          classify(email, llm)
        │                                                    │
        │                kind None ───────────────────────► ignored (+ security_code_flag)
        │                                                    │ else
        │                                  match_email(cls, email, APPLIED jobs)
        │                                                    │
        │       conf >= floors & job_id ──► OutcomeRepo.add ─► outcomes_recorded
        │                                                    │ else
        └──────────────────────────────────────────────────► review (no DB outcome write)

Honesty invariants (NEVER compromise):
  * email ONLY appends an :class:`Outcome` — it NEVER calls ``set_state(APPLIED)``. An
    outcome for a non-APPLIED job is recorded but won't pollute conversion stats (the
    analytics counts only APPLIED jobs).
  * a low-confidence or unmatched email writes NO outcome → it routes to review.
  * ``--dry-run`` (``record=False``) makes the run genuinely side-effect-free: it
    classifies + matches + reports, but writes NOTHING (not even inbox_messages).
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Iterator

from auto_applier.config.settings import Settings
from auto_applier.db.repositories import JobRepo, OutcomeRepo
from auto_applier.domain.models import Outcome
from auto_applier.domain.state import JobState
from auto_applier.inbox.classify import classify
from auto_applier.inbox.match import match_email
from auto_applier.inbox.parse import parse_message
from auto_applier.inbox.repo import InboxMessageRepo
from auto_applier.pipeline.stage import new_run_id, stage

__all__ = ["InboxRunSummary", "InboxWorker", "eml_file_source", "MATCH_MIN", "CLASS_MIN"]


#: Confidence gates. A recorded outcome requires BOTH a confident classification AND a
#: confident match — anything below routes to review, never a guessed outcome (honesty).
MATCH_MIN = 0.6
CLASS_MIN = 0.6


@dataclass
class InboxRunSummary:
    """One ``run_once()`` outcome — observable, not side-effect-only.

    Mutable (like :class:`~auto_applier.pipeline.discover_worker.DiscoverRunSummary`): the
    worker accumulates counters in place over the run, then returns it. The plan sketched
    it ``frozen`` but the discover-mirrored accumulation pattern needs in-place mutation.

    ``fetched`` = messages the source yielded; ``already_processed`` = skipped by
    message_id idempotency; ``classified`` = reached the classifier; ``outcomes_recorded``
    = confident match+class that produced an :class:`Outcome` row; ``routed_to_review`` =
    ambiguous/no-match (no outcome written); ``unmatched`` = the subset of review whose
    matcher returned no job; ``ignored_non_job`` = ``kind is None`` (newsletter / pure
    security-code); ``security_code_flags`` = messages flagged as verification/OTP mail
    (the Greenhouse security-code gate, Direction 3); ``errors`` = per-message failures
    (the @stage spine logged them); ``elapsed_s`` = wall time.
    """

    run_id: str
    fetched: int = 0
    already_processed: int = 0
    classified: int = 0
    outcomes_recorded: int = 0
    routed_to_review: int = 0
    unmatched: int = 0
    ignored_non_job: int = 0
    security_code_flags: int = 0
    errors: int = 0
    elapsed_s: float = 0.0
    notes: list[str] = field(default_factory=list)


class InboxWorker:
    """Ingest emails → record outcomes / route to review. Construct once, call
    :meth:`run_once`.

    ``source`` is an injectable iterable of ``(uid, raw_bytes)`` tuples — the STUB
    fetcher in tests, :func:`eml_file_source` from the CLI offline path, and (Phase C)
    the real IMAP fetcher all conform to it. ``source=None`` makes the worker **inert**
    (returns an empty summary), so the scheduler can keep one alive before the live
    fetcher is wired.

    ``record=False`` is the ``--dry-run`` mode: classify + match + report, write nothing
    (no outcomes, no inbox_messages) — genuinely side-effect-free.
    """

    source_name = "inbox"

    def __init__(
        self,
        *,
        settings: Settings,
        conn: sqlite3.Connection,
        llm=None,
        source: Iterable[tuple[str, bytes]] | None = None,
        record: bool = True,
    ):
        self._settings = settings
        self._conn = conn
        self._llm = llm
        self._source = source
        self._record = record
        self._job_repo = JobRepo(conn)
        self._outcome_repo = OutcomeRepo(conn)
        self._inbox_repo = InboxMessageRepo(conn)

    # -- public ------------------------------------------------------------

    async def run_once(self) -> InboxRunSummary:
        """Ingest every message the source yields, recording outcomes / review rows.

        Inert when no source is configured (Phase C wires the live IMAP fetcher). The
        APPLIED-jobs match candidate list is fetched ONCE per run (not per message).
        """
        run_id = new_run_id()
        summary = InboxRunSummary(run_id=run_id)
        t0 = time.perf_counter()

        if self._source is None:
            summary.notes.append(
                "no source configured — inert (use --eml for offline test, "
                "or enable inbox in onboarding for live fetch)"
            )
            summary.elapsed_s = time.perf_counter() - t0
            return summary

        applied_jobs = self._job_repo.list_by_state(JobState.APPLIED)

        for uid, raw_bytes in self._source:
            summary.fetched += 1
            try:
                await self._handle_one(
                    uid=uid, raw_bytes=raw_bytes,
                    applied_jobs=applied_jobs, summary=summary,
                )
            except Exception:  # noqa: BLE001 — per-message isolation (@stage logged it)
                summary.errors += 1
                summary.notes.append(f"uid={uid} failed")

        summary.elapsed_s = time.perf_counter() - t0
        return summary

    # -- per-message (the @stage spine emits start/ok/error around this) ---

    @stage("inbox")
    async def _handle_one(
        self,
        *,
        uid: str,
        raw_bytes: bytes,
        applied_jobs: list,
        summary: InboxRunSummary,
        platform: str | None = "inbox",  # picked up by @stage for the event row
    ) -> None:
        email = parse_message(raw_bytes, uid=str(uid))

        # Idempotency: a message we already handled never records a second outcome.
        if self._inbox_repo.is_processed(email.message_id):
            summary.already_processed += 1
            return

        cls = await classify(email, llm=self._llm)
        summary.classified += 1
        if cls.security_code_flag:
            summary.security_code_flags += 1

        # (1) Not a job-status email (newsletter / pure security-code) → ignore.
        if cls.kind is None:
            summary.ignored_non_job += 1
            self._mark(email.message_id, action="ignored", kind=None, job_id=None)
            return

        match = match_email(cls, email, applied_jobs)

        # (2) Confident classification AND confident match → record the outcome.
        if (
            match.job_id is not None
            and match.confidence >= MATCH_MIN
            and cls.confidence >= CLASS_MIN
        ):
            note = (
                f"email:{cls.method} conf={cls.confidence:.2f} match={match.reason}"
            )
            if self._record:
                # The single existing outcome path — analytics consumes it unchanged.
                # NEVER set_state(APPLIED): email only appends an Outcome.
                self._outcome_repo.add(
                    Outcome(job_id=match.job_id, kind=cls.kind, note=note)
                )
            summary.outcomes_recorded += 1
            self._mark(
                email.message_id, action="outcome",
                kind=cls.kind.value, job_id=match.job_id,
            )
            return

        # (3) Low confidence or no confident match → review (no DB outcome write).
        summary.routed_to_review += 1
        if match.job_id is None:
            summary.unmatched += 1
        self._mark(email.message_id, action="review", kind=None, job_id=match.job_id)

    # -- helpers -----------------------------------------------------------

    def _mark(
        self, message_id: str, *, action: str, kind: str | None, job_id: str | None
    ) -> None:
        """Record the routing decision — UNLESS this is a dry-run (record=False), in
        which case the run is genuinely side-effect-free (no inbox_messages write either)."""
        if not self._record:
            return
        self._inbox_repo.mark_processed(
            message_id, matched_job_id=job_id, kind=kind, action=action
        )


def eml_file_source(paths: list[str | Path]) -> Iterator[tuple[str, bytes]]:
    """Yield ``(uid, raw_bytes)`` from ``.eml`` files — the CLI's no-creds offline path.

    ``uid`` is the file stem (stable per file). Lets ``av3 inbox --eml saved.eml`` dogfood
    the whole worker (parse → classify → match → record) on a saved email with no IMAP.
    """
    for p in paths:
        path = Path(p)
        yield (path.stem, path.read_bytes())
