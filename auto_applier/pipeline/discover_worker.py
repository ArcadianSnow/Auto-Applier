"""Discovery producer — seeds ``DISCOVERED`` jobs into app.db (spec §6a, §7 #1).

This is the **head** of the pipeline: the stage every other worker drains behind.
Greenhouse / Lever / Ashby all expose unauthenticated public read APIs (no login, no
browser, no anti-detection — see ``research/ats-discovery-seeding.md``), so discovery
is cheap and ToS-clean. Submits never happen here; that's the browser apply path.

  board tokens (settings.targeting)             ┌──────────────────┐
        │  greenhouse: [...]                     │ discover worker  │  ← THIS MODULE
        │  lever:      [...]   ───────────────►  │  (spec §7 #1)    │
        │  ashby:      [...]                     └────────┬─────────┘
        ▼                                                 ▼
  source.discover(token)  →  title pre-filter  →  (describe GH JD)  →  Job(DISCOVERED)
                                                                          │
                                                                  upsert_discovered
                                                                  (idempotent on
                                                                   source+source_job_id)

Pipeline ordering of the work (the throughput win):
  1. ``discover(token)`` lists lightweight snippets (one GET/board, rate-limited ~1 req/s).
  2. **Title pre-filter first** (free — uses the snippet title) so we never pay a JD
     fetch on an obviously-off-target role.
  3. **Greenhouse only:** ``describe()`` the *surviving* listings to fill the full JD
     (Lever/Ashby already return ``descriptionPlain`` at discovery, so no extra call).
     The score worker fail-closes on an empty JD, so populating it here is what lets a
     freshly discovered job flow all the way through a single scheduler cycle.
  4. ``canonical_hash(title, company)`` for cross-source dedup, then
     ``upsert_discovered`` — idempotent, so re-running a cycle never double-inserts.

Observability: each board is one ``@stage("discover")`` unit (platform = the ATS), so
``av3 errors --stage discover`` and ``av3 stats`` see per-board start/ok/error rows for
free — same spine as every other stage. A bad token logs an error and the sweep
continues to the next board (isolation is the point).
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass, field

from auto_applier.config.settings import Settings
from auto_applier.db.repositories import JobRepo
from auto_applier.domain.dedup import canonical_hash
from auto_applier.domain.models import Job
from auto_applier.domain.state import JobState
from auto_applier.pipeline.stage import new_run_id, stage
from auto_applier.sources import (
    AshbySource,
    GreenhouseSource,
    LeverSource,
)

__all__ = ["DiscoverRunSummary", "DiscoverWorker", "BoardSpec", "title_matches"]


# Map an ATS name to its source class. New slug-keyed ATSes (SmartRecruiters,
# Recruitee, …) plug in here once their adapter exists — Phase 2 (spec §6).
_SOURCE_CLASSES = {
    "greenhouse": GreenhouseSource,
    "lever": LeverSource,
    "ashby": AshbySource,
}

# Sources whose discover() returns lightweight snippets WITHOUT the full JD, so we
# must call describe() per surviving listing. Lever/Ashby ship descriptionPlain at
# discovery and are absent here.
_NEEDS_DESCRIBE = {"greenhouse"}


# --------------------------------------------------------------- board spec

@dataclass(frozen=True)
class BoardSpec:
    """One (ATS, token) target. ``token`` is the per-company board slug/site/id."""

    ats: str
    token: str


def title_matches(title: str, wanted: list[str]) -> bool:
    """True if ``title`` matches the targeting filter.

    Empty ``wanted`` = no constraint (keep everything — spec §6c: empty list means
    "no constraint on this axis"). Otherwise a case-insensitive substring match against
    any wanted phrase ("data analyst" matches "Senior Data Analyst, Remote").
    """
    if not wanted:
        return True
    t = title.lower()
    return any(w.strip().lower() in t for w in wanted if w.strip())


# --------------------------------------------------------------- run summary

@dataclass
class DiscoverRunSummary:
    """One ``run_once()`` invocation's outcome — observable, not side-effect-only.

    ``seen`` = raw listings returned across all boards. ``matched`` = survived the
    title pre-filter. ``inserted`` = newly added DISCOVERED rows. ``duplicates`` =
    listings whose (source, source_job_id) was already in the DB (idempotent re-run).
    ``described`` = Greenhouse JD fetches performed. ``board_errors`` = boards whose
    discover() raised (bad/dead token, network) — counted, not fatal.
    """

    run_id: str
    boards_swept: int = 0
    seen: int = 0
    matched: int = 0
    inserted: int = 0
    duplicates: int = 0
    described: int = 0
    board_errors: int = 0
    elapsed_s: float = 0.0
    per_source: dict[str, int] = field(default_factory=dict)  # ats -> inserted
    notes: list[str] = field(default_factory=list)


# --------------------------------------------------------------- the worker

class DiscoverWorker:
    """Produce-side of the DISCOVERED queue.

    Construct once, call :meth:`run_once`. Like the other workers it's stateless across
    runs aside from the DB, so the scheduler keeps one alive and calls ``run_once`` each
    cycle. Boards + title filter default from ``settings.targeting`` so ``av3 discover``
    and headless ``av3 run`` sweep the same set; both can be overridden per call.

    Inject ``sources`` (``{ats: source_obj}``) in tests to avoid real HTTP; in production
    the worker builds one rate-limited source per ATS and closes them after the run.
    """

    source_name = "discover"

    def __init__(
        self,
        *,
        settings: Settings,
        conn: sqlite3.Connection,
        boards: list[BoardSpec] | None = None,
        title_filter: list[str] | None = None,
        per_board_limit: int | None = None,
        describe_greenhouse: bool = True,
        sources: dict[str, object] | None = None,
    ):
        self._settings = settings
        self._conn = conn
        self._job_repo = JobRepo(conn)
        self._boards = boards if boards is not None else boards_from_settings(settings)
        self._title_filter = (
            title_filter if title_filter is not None
            else list(settings.targeting.titles)
        )
        self._per_board_limit = per_board_limit
        self._describe_greenhouse = describe_greenhouse
        self._injected_sources = sources  # None in prod; a dict in tests

    # -- public ------------------------------------------------------------

    async def run_once(self, limit: int | None = None) -> DiscoverRunSummary:
        """Sweep every configured board, seeding DISCOVERED jobs.

        ``limit`` (when set) caps the matched listings *per board* for this call —
        the scheduler / ``--limit`` knob, keeping a cycle bounded. Returns a structured
        summary so the CLI / dashboard can show what just happened without re-querying.
        """
        run_id = new_run_id()
        summary = DiscoverRunSummary(run_id=run_id)
        t0 = time.perf_counter()

        if not self._boards:
            summary.notes.append("no boards configured (settings.targeting.*_boards empty)")
            summary.elapsed_s = time.perf_counter() - t0
            return summary

        per_board_cap = limit if limit is not None else self._per_board_limit
        sources = self._build_sources()
        try:
            for spec in self._boards:
                source = sources.get(spec.ats)
                if source is None:
                    summary.notes.append(f"unknown ATS '{spec.ats}' (token {spec.token}) — skipped")
                    continue
                summary.boards_swept += 1
                try:
                    await self._discover_board(
                        spec=spec, source=source, cap=per_board_cap,
                        summary=summary, platform=spec.ats,
                    )
                except Exception:  # noqa: BLE001 — board isolation (@stage already logged)
                    summary.board_errors += 1
                    summary.notes.append(f"{spec.ats}:{spec.token} discover failed")
        finally:
            self._close_sources(sources)

        summary.elapsed_s = time.perf_counter() - t0
        return summary

    # -- per-board (the @stage spine emits start/ok/error around this) -----

    @stage("discover")
    async def _discover_board(
        self,
        *,
        spec: BoardSpec,
        source,
        cap: int | None,
        summary: DiscoverRunSummary,
        platform: str | None = None,  # picked up by @stage for the event row
    ) -> None:
        listings = source.discover(spec.token)
        summary.seen += len(listings)

        matched = [lst for lst in listings if title_matches(lst.title, self._title_filter)]
        if cap is not None:
            matched = matched[:cap]
        summary.matched += len(matched)

        for lst in matched:
            self._ensure_description(spec.ats, source, lst, summary)
            job = self._to_job(spec.ats, lst)
            existing = self._job_repo.get_by_source(job.source, job.source_job_id)
            self._job_repo.upsert_discovered(job)
            if existing is None:
                summary.inserted += 1
                summary.per_source[spec.ats] = summary.per_source.get(spec.ats, 0) + 1
            else:
                summary.duplicates += 1

    # -- helpers -----------------------------------------------------------

    def _ensure_description(self, ats: str, source, listing, summary: DiscoverRunSummary) -> None:
        """Fill the full JD when the snippet lacks one (Greenhouse). Best-effort: a
        describe failure leaves the description empty (the score worker fail-closes it
        to REVIEW later) and is noted, never fatal to the board sweep."""
        if getattr(listing, "description", ""):
            return
        if ats not in _NEEDS_DESCRIBE or not self._describe_greenhouse:
            return
        try:
            source.describe(listing)
            summary.described += 1
        except Exception:  # noqa: BLE001 — one JD fetch; keep going
            summary.notes.append(
                f"{ats}: describe failed for {getattr(listing, 'source_job_id', '?')}"
            )

    @staticmethod
    def _to_job(ats: str, listing) -> Job:
        """Convert an adapter listing (JobListing/LeverListing/AshbyListing — they share
        the snippet fields) into a domain :class:`Job` in DISCOVERED with a dedup hash."""
        title = getattr(listing, "title", "") or ""
        company = getattr(listing, "company", "") or ""
        return Job(
            source=ats,
            source_job_id=str(getattr(listing, "source_job_id", "") or ""),
            title=title,
            company=company,
            canonical_hash=canonical_hash(title, company),
            location=getattr(listing, "location", "") or "",
            url=getattr(listing, "url", "") or "",
            description=getattr(listing, "description", "") or "",
            posted_at=str(getattr(listing, "posted_at", "") or ""),
            state=JobState.DISCOVERED,
        )

    def _build_sources(self) -> dict[str, object]:
        """One source instance per ATS we actually target (reused across that ATS's
        tokens so the per-host throttle is shared). Injected sources win for tests."""
        if self._injected_sources is not None:
            return self._injected_sources
        wanted_ats = {spec.ats for spec in self._boards}
        return {
            ats: _SOURCE_CLASSES[ats]()
            for ats in wanted_ats
            if ats in _SOURCE_CLASSES
        }

    def _close_sources(self, sources: dict[str, object]) -> None:
        # Don't close injected (test-owned) sources; we only own the ones we built.
        if self._injected_sources is not None:
            return
        for src in sources.values():
            close = getattr(src, "close", None)
            if callable(close):
                close()


def boards_from_settings(settings: Settings) -> list[BoardSpec]:
    """Flatten ``settings.targeting.{greenhouse,lever,ashby}_boards`` into one ordered
    list of :class:`BoardSpec`. Greenhouse first (most common ATS), then Lever, Ashby."""
    t = settings.targeting
    specs: list[BoardSpec] = []
    specs.extend(BoardSpec("greenhouse", tok) for tok in t.greenhouse_boards)
    specs.extend(BoardSpec("lever", tok) for tok in t.lever_boards)
    specs.extend(BoardSpec("ashby", tok) for tok in t.ashby_boards)
    return specs
