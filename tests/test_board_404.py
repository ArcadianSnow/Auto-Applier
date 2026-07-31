"""A dead board token is a marked ``failure - 404``, never a dropped board.

Owner decision (2026-07-31): keep sweeping a 404'ing board every run so it self-heals the day
the company's board comes back — but stop filing it as a generic error. Before this, the same
dead Greenhouse token produced one identical error row per daily run for weeks, sitting in the
same bucket as real failures, and recurring known noise is exactly what hides a new problem.

Lever and Ashby previously returned ``[]`` on ANY non-200, so a dead token there was
indistinguishable from "this company has no open roles" — completely invisible.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from auto_applier.pipeline.discover_worker import (
    BOARD_404_REASON,
    BoardSpec,
    DiscoverWorker,
)
from auto_applier.sources import BoardNotFound
from auto_applier.sources.ashby import AshbySource
from auto_applier.sources.greenhouse import GreenhouseSource
from auto_applier.sources.lever import LeverSource


class _Resp:
    def __init__(self, status: int, payload=None):
        self.status_code = status
        self._payload = payload if payload is not None else {}

    def json(self):
        return self._payload


# --------------------------------------------------------------- per-source 404 signal

@pytest.mark.parametrize(
    "source_cls, ats",
    [(GreenhouseSource, "greenhouse"), (LeverSource, "lever"), (AshbySource, "ashby")],
)
def test_every_ats_reports_a_dead_token_the_same_way(source_cls, ats, monkeypatch):
    src = source_cls()
    monkeypatch.setattr(src, "_get", lambda *a, **kw: _Resp(404))
    with pytest.raises(BoardNotFound) as exc:
        src.discover("deadtoken")
    assert exc.value.ats == ats
    assert exc.value.token == "deadtoken"


@pytest.mark.parametrize("source_cls", [LeverSource, AshbySource])
def test_other_non_200s_stay_tolerant(source_cls, monkeypatch):
    """Only a 404 is the permanent 'board is gone' signal; a 5xx stays a quiet empty result
    (transient, and this path has always been tolerant of it)."""
    src = source_cls()
    monkeypatch.setattr(src, "_get", lambda *a, **kw: _Resp(503))
    assert src.discover("acme") == []


# --------------------------------------------------------------- worker bookkeeping

class _DeadSource:
    def __init__(self, ats="greenhouse"):
        self.ats = ats
        self.calls = 0

    def discover(self, token):
        self.calls += 1
        raise BoardNotFound(self.ats, token)


class _LiveSource:
    def discover(self, token):
        return []


def test_a_404_board_is_marked_not_counted_as_an_error(settings, conn):
    worker = DiscoverWorker(
        settings=settings, conn=conn,
        boards=[BoardSpec("greenhouse", "dbtlabsinc")],
        title_filter=[], sources={"greenhouse": _DeadSource()},
    )
    summary = asyncio.run(worker.run_once())

    assert summary.boards_missing == 1
    assert summary.board_errors == 0            # NOT an error — that bucket is for real faults
    assert any(BOARD_404_REASON in n and "dbtlabsinc" in n for n in summary.notes)


def test_a_404_board_never_stops_the_sweep(settings, conn):
    """Isolation: the boards after a dead one are still swept."""
    live = _LiveSource()
    worker = DiscoverWorker(
        settings=settings, conn=conn,
        boards=[BoardSpec("greenhouse", "dead"), BoardSpec("lever", "alive")],
        title_filter=[], sources={"greenhouse": _DeadSource(), "lever": live},
    )
    summary = asyncio.run(worker.run_once())

    assert summary.boards_swept == 2
    assert summary.boards_missing == 1


def test_a_404_board_is_retried_on_the_next_run(settings, conn):
    """The board is NEVER dropped from targeting — it must be swept again so it self-heals
    the day the company's board comes back."""
    dead = _DeadSource()
    worker = DiscoverWorker(
        settings=settings, conn=conn, boards=[BoardSpec("greenhouse", "dead")],
        title_filter=[], sources={"greenhouse": dead},
    )
    asyncio.run(worker.run_once())
    asyncio.run(worker.run_once())
    assert dead.calls == 2


def test_the_404_lands_on_the_event_spine_as_a_skip_with_its_reason(settings, conn, sink):
    """`av3 errors` must stay clean while the outcome stays fully recorded — that's the whole
    trade. The reason string is what `av3 doctor` reads back."""
    worker = DiscoverWorker(
        settings=settings, conn=conn, boards=[BoardSpec("greenhouse", "dbtlabsinc")],
        title_filter=[], sources={"greenhouse": _DeadSource()},
    )
    asyncio.run(worker.run_once())

    rows = sink.conn.execute(
        "SELECT status, context_json FROM events WHERE stage='discover' AND status IN ('skip','error')"
    ).fetchall()
    assert [r[0] for r in rows] == ["skip"]                      # never 'error'
    reason = json.loads(rows[0][1])["reason"]
    assert BOARD_404_REASON in reason and "dbtlabsinc" in reason


# --------------------------------------------------------------- doctor visibility

def test_doctor_names_the_404_boards(settings, conn, sink):
    """Keeping dead boards out of `av3 errors` only works if something still surfaces them."""
    from auto_applier.doctor import Status, check_boards

    worker = DiscoverWorker(
        settings=settings, conn=conn, boards=[BoardSpec("greenhouse", "dbtlabsinc")],
        title_filter=[], sources={"greenhouse": _DeadSource()},
    )
    asyncio.run(worker.run_once())

    result = check_boards(settings)
    assert result.status is Status.WARN
    assert "dbtlabsinc" in result.detail
    assert "still swept" in result.fix       # says the board is retried, not dropped


def test_doctor_is_quiet_when_every_board_is_healthy(settings, conn, sink):
    from auto_applier.doctor import Status, check_boards

    worker = DiscoverWorker(
        settings=settings, conn=conn, boards=[BoardSpec("lever", "alive")],
        title_filter=[], sources={"lever": _LiveSource()},
    )
    asyncio.run(worker.run_once())

    assert check_boards(settings).status is Status.PASS


def test_doctor_also_reads_legacy_404_error_rows(settings, sink):
    """Before this change a 404 was logged as a plain error row. Reading those too means the
    check works against an existing events.db immediately, not only after the next run —
    verified against the owner's real spine, where it names `greenhouse:dbtlabsinc`."""
    from auto_applier.doctor import Status, check_boards

    sink.emit(
        stage="discover", status="error", run_id="r1", platform="greenhouse",
        error_type="GreenhouseError",
        error_msg="board token 'dbtlabsinc' not found (404)",
    )
    result = check_boards(settings)
    assert result.status is Status.WARN
    assert "greenhouse:dbtlabsinc" in result.detail


def test_doctor_ignores_unrelated_discover_errors(settings, sink):
    from auto_applier.doctor import Status, check_boards

    sink.emit(
        stage="discover", status="error", run_id="r1", platform="greenhouse",
        error_type="GreenhouseError",
        error_msg="network error listing typeform: The read operation timed out",
    )
    assert check_boards(settings).status is Status.PASS


def test_doctor_tolerates_a_missing_events_db(settings):
    from auto_applier.doctor import Status, check_boards
    assert check_boards(settings).status is Status.PASS
