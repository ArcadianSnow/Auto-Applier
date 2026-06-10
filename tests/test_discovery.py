"""Discovery producer (spec §7 #1) — contract tests for the DISCOVERED-seeding head.

Mirrors the shape of ``test_filter_worker.py``: we fake the *source adapters*
(deterministic listings keyed by token) instead of the live ATS HTTP path, so these
tests stay focused on the worker's contract:

  * discover -> Job(DISCOVERED) with canonical_hash + JD populated (``inserted``);
  * Greenhouse JD is filled via describe(); Lever/Ashby keep their snippet JD;
  * re-running is idempotent (no double-insert; second run is all ``duplicates``);
  * the title pre-filter runs BEFORE describe (no JD fetch on off-target roles);
  * ``per_board_limit`` caps matched listings per board;
  * a board whose discover() raises is isolated (``board_errors``), the sweep continues;
  * ``describe_greenhouse=False`` skips the JD fetch;
  * no-boards is a no-op note, not an error;
  * built sources are closed; injected (test-owned) sources are not.

The live ATS HTTP path (real httpx against Greenhouse/Lever/Ashby) is covered by the
adapter modules' own tests + the `smoke` survey; re-testing it here would just couple
this file to each vendor's wire format.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from auto_applier.config.settings import Settings
from auto_applier.db.repositories import JobRepo
from auto_applier.domain.dedup import canonical_hash
from auto_applier.domain.state import JobState
from auto_applier.pipeline.discover_worker import (
    BoardSpec,
    DiscoverWorker,
    boards_from_settings,
    title_matches,
)


# --------------------------------------------------------------- fakes

def _listing(sid: str, title: str, company: str, description: str = "") -> SimpleNamespace:
    """A stand-in adapter listing. The worker reads its fields via getattr, so a
    SimpleNamespace with the shared snippet attrs stands in for any of
    JobListing / LeverListing / AshbyListing."""
    return SimpleNamespace(
        source_job_id=sid, title=title, company=company,
        location="Remote", url=f"https://example/{sid}",
        description=description, posted_at="2026-06-01",
    )


class _FakeSource:
    """Deterministic source stub. ``by_token`` maps token -> [listings]. When
    ``fills_jd`` is True (Greenhouse-like) describe() populates the JD and records the
    call; tokens in ``raise_for`` make discover() raise (dead/bad token)."""

    def __init__(self, by_token: dict[str, list], *, fills_jd: bool = False):
        self._by_token = by_token
        self._fills_jd = fills_jd
        self.describe_calls: list[str] = []
        self.discover_calls: list[str] = []
        self.closed = False
        self.raise_for: set[str] = set()

    def discover(self, token: str) -> list:
        self.discover_calls.append(token)
        if token in self.raise_for:
            raise RuntimeError(f"simulated dead token {token!r}")
        return list(self._by_token.get(token, []))

    def describe(self, listing) -> str:
        self.describe_calls.append(listing.source_job_id)
        listing.description = f"JD for {listing.source_job_id}"
        return listing.description

    def close(self) -> None:
        self.closed = True


def _worker(settings, conn, *, boards, sources, **kw) -> DiscoverWorker:
    return DiscoverWorker(
        settings=settings, conn=conn, boards=boards, sources=sources, **kw
    )


# --------------------------------------------------------------- title filter (unit)

def test_title_matches_empty_filter_keeps_everything():
    assert title_matches("Anything At All", []) is True


def test_title_matches_is_case_insensitive_substring():
    assert title_matches("Senior Data Analyst, Remote", ["data analyst"]) is True
    assert title_matches("Frontend Engineer", ["data analyst"]) is False
    assert title_matches("Frontend Engineer", ["data analyst", "engineer"]) is True


# --------------------------------------------------------------- seed + describe

def test_discovers_and_seeds_greenhouse_with_jd(settings: Settings, conn):
    gh = _FakeSource({"acme": [_listing("1", "Data Analyst", "Acme")]}, fills_jd=True)
    w = _worker(settings, conn, boards=[BoardSpec("greenhouse", "acme")],
                sources={"greenhouse": gh})

    summary = asyncio.run(w.run_once())

    assert summary.inserted == 1
    assert summary.duplicates == 0
    assert summary.described == 1                      # JD was fetched
    assert gh.describe_calls == ["1"]
    jobs = JobRepo(conn).list_by_state(JobState.DISCOVERED)
    assert len(jobs) == 1
    job = jobs[0]
    assert job.source == "greenhouse"
    assert job.description == "JD for 1"               # describe() populated it
    assert job.canonical_hash == canonical_hash("Data Analyst", "Acme")
    assert summary.per_source == {"greenhouse": 1}


def test_lever_ashby_keep_snippet_jd_no_describe(settings: Settings, conn):
    # Lever/Ashby ship descriptionPlain at discovery → describe() must NOT be called.
    lever = _FakeSource(
        {"globex": [_listing("L1", "Data Analyst", "Globex", description="full lever JD")]},
    )  # fills_jd defaults False; describe() would still be a no-op but shouldn't be hit
    w = _worker(settings, conn, boards=[BoardSpec("lever", "globex")],
                sources={"lever": lever})

    summary = asyncio.run(w.run_once())

    assert summary.inserted == 1
    assert summary.described == 0
    assert lever.describe_calls == []                  # already had a JD
    job = JobRepo(conn).list_by_state(JobState.DISCOVERED)[0]
    assert job.description == "full lever JD"


# --------------------------------------------------------------- idempotency

def test_rerun_is_idempotent(settings: Settings, conn):
    gh = _FakeSource({"acme": [_listing("1", "Data Analyst", "Acme")]}, fills_jd=True)
    boards = [BoardSpec("greenhouse", "acme")]

    first = asyncio.run(_worker(settings, conn, boards=boards,
                                sources={"greenhouse": gh}).run_once())
    second = asyncio.run(_worker(settings, conn, boards=boards,
                                 sources={"greenhouse": gh}).run_once())

    assert first.inserted == 1
    assert second.inserted == 0
    assert second.duplicates == 1
    assert len(JobRepo(conn).list_by_state(JobState.DISCOVERED)) == 1


# --------------------------------------------------------------- title pre-filter

def test_title_filter_runs_before_describe(settings: Settings, conn):
    gh = _FakeSource(
        {"acme": [
            _listing("1", "Data Analyst", "Acme"),
            _listing("2", "Warehouse Associate", "Acme"),
        ]},
        fills_jd=True,
    )
    w = _worker(settings, conn, boards=[BoardSpec("greenhouse", "acme")],
                sources={"greenhouse": gh}, title_filter=["data analyst"])

    summary = asyncio.run(w.run_once())

    assert summary.seen == 2
    assert summary.matched == 1
    assert summary.inserted == 1
    # The off-target role was filtered BEFORE we paid for its JD fetch.
    assert gh.describe_calls == ["1"]
    titles = [j.title for j in JobRepo(conn).list_by_state(JobState.DISCOVERED)]
    assert titles == ["Data Analyst"]


# --------------------------------------------------------------- limit

def test_per_board_limit_caps_matched(settings: Settings, conn):
    gh = _FakeSource(
        {"acme": [_listing(str(i), f"Data Analyst {i}", "Acme") for i in range(5)]},
        fills_jd=True,
    )
    w = _worker(settings, conn, boards=[BoardSpec("greenhouse", "acme")],
                sources={"greenhouse": gh}, per_board_limit=2)

    summary = asyncio.run(w.run_once())

    assert summary.matched == 2
    assert summary.inserted == 2
    assert len(JobRepo(conn).list_by_state(JobState.DISCOVERED)) == 2


def test_run_once_limit_arg_overrides_per_board(settings: Settings, conn):
    gh = _FakeSource(
        {"acme": [_listing(str(i), f"Data Analyst {i}", "Acme") for i in range(5)]},
        fills_jd=True,
    )
    w = _worker(settings, conn, boards=[BoardSpec("greenhouse", "acme")],
                sources={"greenhouse": gh})

    summary = asyncio.run(w.run_once(limit=1))
    assert summary.matched == 1


# --------------------------------------------------------------- isolation

def test_dead_token_is_isolated_other_boards_proceed(settings: Settings, conn):
    gh = _FakeSource(
        {
            "dead": [],            # raises below
            "live": [_listing("9", "Data Analyst", "Live Co")],
        },
        fills_jd=True,
    )
    gh.raise_for.add("dead")
    w = _worker(
        settings, conn,
        boards=[BoardSpec("greenhouse", "dead"), BoardSpec("greenhouse", "live")],
        sources={"greenhouse": gh},
    )

    summary = asyncio.run(w.run_once())

    assert summary.board_errors == 1
    assert summary.inserted == 1                       # the live board still seeded
    assert len(JobRepo(conn).list_by_state(JobState.DISCOVERED)) == 1


# --------------------------------------------------------------- describe toggle

def test_describe_disabled_leaves_jd_empty(settings: Settings, conn):
    gh = _FakeSource({"acme": [_listing("1", "Data Analyst", "Acme")]}, fills_jd=True)
    w = _worker(settings, conn, boards=[BoardSpec("greenhouse", "acme")],
                sources={"greenhouse": gh}, describe_greenhouse=False)

    summary = asyncio.run(w.run_once())

    assert summary.described == 0
    assert gh.describe_calls == []
    assert JobRepo(conn).list_by_state(JobState.DISCOVERED)[0].description == ""


# --------------------------------------------------------------- edge cases

def test_no_boards_is_a_noop_note(settings: Settings, conn):
    w = _worker(settings, conn, boards=[], sources={})
    summary = asyncio.run(w.run_once())
    assert summary.boards_swept == 0
    assert summary.inserted == 0
    assert summary.notes and "no boards" in summary.notes[0]


def test_unknown_ats_is_skipped_with_note(settings: Settings, conn):
    w = _worker(settings, conn, boards=[BoardSpec("workday", "tenant")], sources={})
    summary = asyncio.run(w.run_once())
    assert summary.inserted == 0
    assert any("unknown ATS" in n for n in summary.notes)


def test_injected_sources_are_not_closed(settings: Settings, conn):
    gh = _FakeSource({"acme": [_listing("1", "Data Analyst", "Acme")]}, fills_jd=True)
    w = _worker(settings, conn, boards=[BoardSpec("greenhouse", "acme")],
                sources={"greenhouse": gh})
    asyncio.run(w.run_once())
    assert gh.closed is False                          # test owns the source; worker mustn't close it


# --------------------------------------------------------------- settings wiring

def test_boards_from_settings_orders_gh_lever_ashby(settings: Settings):
    specs = boards_from_settings(settings)
    # Defaults are non-empty; order is greenhouse, then lever, then ashby.
    ats_seen = [s.ats for s in specs]
    assert ats_seen == sorted(
        ats_seen, key=lambda a: {"greenhouse": 0, "lever": 1, "ashby": 2}[a]
    )
    assert any(s.ats == "greenhouse" for s in specs)


# --------------------------------------------------------------- scheduler wiring

class _FakeStageWorker:
    """Minimal worker stub for the scheduler-order test (shared call log)."""

    def __init__(self, label: str, summary, call_log: list[str]):
        self.label = label
        self._summary = summary
        self._call_log = call_log

    async def run_once(self):
        self._call_log.append(self.label)
        return self._summary


def test_scheduler_runs_discover_first_when_wired():
    """When a discover_worker is provided it leads the cycle (gather stage), and its
    summary is stashed on the cycle. Without one, the loop is unchanged (covered by
    test_scheduler.py's exact-order assertions, which still expect no 'discover')."""
    from auto_applier.pipeline import (
        ApplyRunSummary,
        DiscoverRunSummary,
        FilterRunSummary,
        OptimizeRunSummary,
        Scheduler,
        ScoreRunSummary,
        parse_quiet_hours,
    )

    call_log: list[str] = []
    d = _FakeStageWorker("discover", DiscoverRunSummary(run_id="d-1"), call_log)
    f = _FakeStageWorker("filter", FilterRunSummary(run_id="f-1"), call_log)
    s = _FakeStageWorker("score", ScoreRunSummary(run_id="s-1"), call_log)
    o = _FakeStageWorker("optimize", OptimizeRunSummary(run_id="o-1"), call_log)
    a = _FakeStageWorker("apply", ApplyRunSummary(run_id="a-1"), call_log)

    async def _sleep(_seconds): ...

    scheduler = Scheduler(
        discover_worker=d,                      # type: ignore[arg-type]
        filter_worker=f,                        # type: ignore[arg-type]
        score_worker=s,                         # type: ignore[arg-type]
        optimize_worker=o,                      # type: ignore[arg-type]
        apply_worker=a,                         # type: ignore[arg-type]
        quiet_hours=parse_quiet_hours(None),
        sleep=_sleep,
    )
    summary = asyncio.run(scheduler.run(max_cycles=1))

    assert call_log == ["discover", "filter", "score", "optimize", "apply"]
    assert summary.cycles[0].discover_summary is not None
    assert summary.cycles[0].discover_summary.run_id == "d-1"
