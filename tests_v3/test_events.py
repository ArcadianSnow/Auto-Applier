"""Event spine: EventSink + the @stage wrapper (spec §7, §9)."""

from __future__ import annotations

import asyncio

import pytest

from av3.pipeline.stage import StageSkip, new_run_id, stage
from av3.telemetry.scrub import scrub


def test_sink_emit_and_recent(sink):
    sink.emit(stage="discover", status="ok", duration_ms=12)
    sink.emit(stage="score", status="error", error_type="ValueError", error_msg="bad")
    recent = sink.recent()
    assert recent[0]["stage"] == "score"  # newest first
    assert {r["stage"] for r in recent} == {"discover", "score"}
    errs = sink.errors()
    assert len(errs) == 1 and errs[0]["error_type"] == "ValueError"


def test_stage_decorator_emits_ok(sink):
    @stage("score")
    def do_score(job_id=None):
        return 42

    rid = new_run_id()
    assert do_score(job_id="job-1") == 42
    rows = sink.recent()
    statuses = [(r["stage"], r["status"], r["job_id"], r["run_id"]) for r in rows]
    assert ("score", "ok", "job-1", rid) in statuses
    assert ("score", "start", "job-1", rid) in statuses
    ok_row = next(r for r in rows if r["status"] == "ok")
    assert ok_row["duration_ms"] is not None


def test_stage_decorator_records_error_and_reraises(sink):
    @stage("apply")
    def boom(job_id=None):
        raise RuntimeError("selector miss")

    with pytest.raises(RuntimeError):
        boom(job_id="j2")
    err = sink.errors()[0]
    assert err["stage"] == "apply"
    assert err["error_type"] == "RuntimeError"
    assert "selector miss" in err["error_msg"]


def test_stage_skip_records_skip_not_error(sink):
    @stage("dedup")
    def maybe(job_id=None):
        raise StageSkip("duplicate")

    assert maybe(job_id="j3") is None  # swallowed, returns None
    skips = [r for r in sink.recent() if r["status"] == "skip"]
    assert len(skips) == 1
    assert "duplicate" in (skips[0]["context_json"] or "")
    assert sink.errors() == []  # a skip is not an error


def test_stage_async(sink):
    @stage("discover", platform="greenhouse")
    async def adiscover(job_id=None):
        await asyncio.sleep(0)
        return "done"

    new_run_id()
    assert asyncio.run(adiscover(job_id="aj")) == "done"
    rows = sink.recent()
    assert any(r["status"] == "ok" and r["platform"] == "greenhouse" for r in rows)


def test_stage_extracts_context_from_job_kwarg(sink):
    class FakeJob:
        id = "fake-id"
        source = "lever"

    @stage("describe")
    def describe(job=None):
        return None

    new_run_id()
    describe(job=FakeJob())
    start = next(r for r in sink.recent() if r["status"] == "start")
    assert start["job_id"] == "fake-id"
    assert start["platform"] == "lever"


def test_stage_stats(sink):
    @stage("score")
    def ok(job_id=None):
        return 1

    ok(job_id="a")
    ok(job_id="b")
    stats = {r["stage"]: r for r in sink.stage_stats()}
    assert stats["score"]["ok"] == 2


def test_no_sink_configured_is_silent():
    # domain/unit tests that never configure a sink must still run (no I/O, no crash)
    from av3.telemetry import reset_sink

    reset_sink()

    @stage("score")
    def f(job_id=None):
        return "ok"

    assert f(job_id="x") == "ok"


def test_scrub_removes_pii():
    assert "[email]" in scrub("contact me@example.com please")
    # long free-text is truncated so résumé/answer blobs can't ride along in an error
    long_out = scrub("x" * 600)
    assert long_out.endswith("[truncated]") and len(long_out) < 600
    # filesystem paths (which embed a username) are masked
    out = scrub(r"failed at C:\Users\alice\resume.pdf")
    assert "alice" not in out
