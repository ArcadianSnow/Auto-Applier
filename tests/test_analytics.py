"""Outcome feedback analytics (spec §8e, Phase 6 4/M).

Two layers:
  * OutcomeRepo — record + the applied-jobs-×-outcomes join feed (uses a real tmp DB).
  * analytics.py — pure aggregation of that feed into a ConversionReport + advisory
    weight-nudge recommendations (no I/O; the bulk of the coverage is here).
"""

from __future__ import annotations

import sqlite3

from auto_applier.analytics import (
    SCORE_BANDS,
    ConversionReport,
    GroupStat,
    compute_conversion_report,
    compute_funnel,
    furthest_outcomes,
    recommend_weight_nudges,
)
from auto_applier.db.repositories import JobRepo, OutcomeRepo, ScoreRepo
from auto_applier.domain.models import Job, JobScore, Outcome
from auto_applier.domain.state import JobState, OutcomeKind


# --------------------------------------------------------------- OutcomeKind ranking

def test_outcome_rank_orders_funnel():
    assert OutcomeKind.GHOST.rank < OutcomeKind.REJECTION.rank
    assert OutcomeKind.REJECTION.rank < OutcomeKind.RESPONSE.rank
    assert OutcomeKind.RESPONSE.rank < OutcomeKind.INTERVIEW.rank
    assert OutcomeKind.INTERVIEW.rank < OutcomeKind.OFFER.rank


def test_outcome_is_positive():
    assert OutcomeKind.RESPONSE.is_positive
    assert OutcomeKind.INTERVIEW.is_positive
    assert OutcomeKind.OFFER.is_positive
    assert not OutcomeKind.GHOST.is_positive
    assert not OutcomeKind.REJECTION.is_positive


# --------------------------------------------------------------- OutcomeRepo (real DB)

def _applied_job(conn, *, source="lever", title="Data Engineer", company="Acme",
                 source_job_id="j1", score=None) -> Job:
    repo = JobRepo(conn)
    job = Job(source=source, source_job_id=source_job_id, title=title, company=company,
              url=f"https://x/{source_job_id}")
    repo.add(job)
    for nxt in (JobState.DESCRIBED, JobState.SCORED, JobState.DECIDED,
                JobState.QUEUED_APPLY, JobState.APPLYING, JobState.APPLIED):
        repo.set_state(job.id, nxt)
    if score is not None:
        ScoreRepo(conn).upsert(JobScore(job_id=job.id, total=score, dimensions={}))
    return repo.get(job.id)


def test_repo_records_and_lists_outcomes(conn: sqlite3.Connection):
    job = _applied_job(conn, source_job_id="r1")
    repo = OutcomeRepo(conn)
    repo.add(Outcome(job_id=job.id, kind=OutcomeKind.RESPONSE))
    repo.add(Outcome(job_id=job.id, kind=OutcomeKind.INTERVIEW))
    got = repo.list_by_job(job.id)
    assert [o.kind for o in got] == [OutcomeKind.RESPONSE, OutcomeKind.INTERVIEW]
    assert repo.count_by_kind() == {"response": 1, "interview": 1}


def test_applied_with_outcomes_includes_silent_jobs(conn: sqlite3.Connection):
    """An APPLIED job with NO recorded outcome still appears once (kind=None) — the
    implicit-ghost denominator."""
    silent = _applied_job(conn, source_job_id="s1", score=8.0)
    answered = _applied_job(conn, source_job_id="s2", score=9.0)
    OutcomeRepo(conn).add(Outcome(job_id=answered.id, kind=OutcomeKind.RESPONSE))

    feed = OutcomeRepo(conn).applied_with_outcomes()
    by_job = {r["job_id"]: r for r in feed}
    assert by_job[silent.id]["kind"] is None
    assert by_job[answered.id]["kind"] == "response"


def test_applied_with_outcomes_excludes_non_applied(conn: sqlite3.Connection):
    repo = JobRepo(conn)
    job = Job(source="lever", source_job_id="na1", title="X", company="Y", url="u")
    repo.add(job)
    repo.set_state(job.id, JobState.DESCRIBED)  # never applied
    feed = OutcomeRepo(conn).applied_with_outcomes()
    assert all(r["job_id"] != job.id for r in feed)


# --------------------------------------------------------------- compute_conversion_report (pure)

def _feed_row(job_id, source, title, score, kind):
    return {"job_id": job_id, "source": source, "title": title, "company": "C",
            "score": score, "kind": kind, "noted_at": "2026-05-30T00:00:00"}


def test_empty_feed_is_zero_report():
    rep = compute_conversion_report([])
    assert isinstance(rep, ConversionReport)
    assert rep.total_applied == 0
    assert rep.overall_rate == 0.0


def test_furthest_outcome_wins_per_job():
    """A job with response THEN interview counts once, as the deeper (interview) stage."""
    feed = [
        _feed_row("j1", "lever", "DE", 8.0, "response"),
        _feed_row("j1", "lever", "DE", 8.0, "interview"),
    ]
    rep = compute_conversion_report(feed)
    assert rep.total_applied == 1
    assert rep.total_converted == 1
    assert rep.outcome_counts == {"interview": 1}  # furthest stage only, counted once


def test_silent_job_counts_as_applied_not_converted():
    feed = [_feed_row("j1", "lever", "DE", 8.0, None)]
    rep = compute_conversion_report(feed)
    assert rep.total_applied == 1
    assert rep.total_converted == 0
    assert rep.overall_rate == 0.0
    # ghosted (implicit): the single source group has ghosted=1
    src = next(s for s in rep.by_source if s.key == "lever")
    assert src.ghosted == 1


def test_conversion_rate_by_source():
    feed = [
        _feed_row("a", "lever", "DE", 8.0, "interview"),   # convert
        _feed_row("b", "lever", "DE", 8.0, "rejection"),   # no
        _feed_row("c", "greenhouse", "DE", 8.0, "offer"),  # convert
        _feed_row("d", "greenhouse", "DE", 8.0, None),     # silent
    ]
    rep = compute_conversion_report(feed)
    by = {s.key: s for s in rep.by_source}
    assert by["lever"].applied == 2 and by["lever"].converted == 1
    assert by["lever"].rate == 0.5
    assert by["greenhouse"].applied == 2 and by["greenhouse"].converted == 1
    assert rep.total_applied == 4 and rep.total_converted == 2
    assert rep.overall_rate == 0.5


def test_score_band_grouping():
    feed = [
        _feed_row("a", "lever", "DE", 2.0, "rejection"),   # low band
        _feed_row("b", "lever", "DE", 5.0, "response"),    # mid band
        _feed_row("c", "lever", "DE", 9.0, "offer"),       # high band
        _feed_row("d", "lever", "DE", None, "response"),   # unscored
    ]
    rep = compute_conversion_report(feed)
    bands = {s.key: s for s in rep.by_band}
    assert "low[0-4)" in bands and bands["low[0-4)"].applied == 1
    assert "mid[4-7)" in bands and bands["mid[4-7)"].applied == 1
    assert "high[7-10]" in bands and bands["high[7-10]"].applied == 1
    assert "unscored" in bands and bands["unscored"].applied == 1


def test_outcome_counts_tallies_furthest_stage():
    feed = [
        _feed_row("a", "lever", "DE", 8.0, "response"),
        _feed_row("a", "lever", "DE", 8.0, "offer"),   # furthest = offer
        _feed_row("b", "lever", "DE", 8.0, "ghost"),
    ]
    rep = compute_conversion_report(feed)
    assert rep.outcome_counts == {"offer": 1, "ghost": 1}


# --------------------------------------------------------------- compute_funnel (pure, Direction 2/B)

def test_compute_funnel_cumulative():
    """Each applied job is counted once by its furthest stage; positive stages are
    CUMULATIVE (an offer counts in responded + interviewed + offered)."""
    feed = [
        _feed_row("j1", "lever", "DE", 8.0, "response"),
        _feed_row("j1", "lever", "DE", 8.0, "offer"),       # furthest = offer
        _feed_row("j2", "lever", "DE", 8.0, "interview"),   # furthest = interview
        _feed_row("j3", "lever", "DE", 8.0, "response"),    # furthest = response
        _feed_row("j4", "lever", "DE", 8.0, "rejection"),
        _feed_row("j5", "lever", "DE", 8.0, "ghost"),
        _feed_row("j6", "lever", "DE", 8.0, None),          # applied, silent
    ]
    f = compute_funnel(feed)
    assert f.applied == 6
    assert f.responded == 3      # offer + interview + response
    assert f.interviewed == 2    # offer + interview
    assert f.offered == 1        # offer
    assert f.rejected == 1
    assert f.ghosted == 1        # explicit ghost only
    assert f.awaiting == 1       # the silent job — NOT counted as ghosted


def test_compute_funnel_empty_is_all_zero():
    f = compute_funnel([])
    assert (f.applied, f.responded, f.interviewed, f.offered,
            f.rejected, f.ghosted, f.awaiting) == (0, 0, 0, 0, 0, 0, 0)


def test_funnel_responded_equals_converted():
    """responded must stay equal to ConversionReport.total_converted on the same feed."""
    feed = [
        _feed_row("a", "lever", "DE", 8.0, "offer"),
        _feed_row("b", "greenhouse", "DE", 5.0, "rejection"),
        _feed_row("c", "ashby", "DE", 9.0, "interview"),
        _feed_row("d", "lever", "DE", None, None),
    ]
    assert compute_funnel(feed).responded == compute_conversion_report(feed).total_converted


# --------------------------------------------------------------- furthest_outcomes (pure)

def test_furthest_outcomes_maps_furthest_and_keeps_silent_none():
    feed = [
        _feed_row("j1", "lever", "DE", 8.0, "response"),
        _feed_row("j1", "lever", "DE", 8.0, "interview"),   # furthest wins
        _feed_row("j2", "lever", "DE", 8.0, None),          # silent → None (not ghost)
    ]
    got = furthest_outcomes(feed)
    assert got == {"j1": "interview", "j2": None}


# --------------------------------------------------------------- weight nudges (advisory)

def _band_report(high_rate, low_rate, n_each=50):
    """Hand-build a report with given high/low band conversion rates."""
    high_conv = int(round(high_rate * n_each))
    low_conv = int(round(low_rate * n_each))
    return ConversionReport(
        total_applied=n_each * 2,
        total_converted=high_conv + low_conv,
        by_band=[
            GroupStat(key="high[7-10]", applied=n_each, converted=high_conv),
            GroupStat(key="low[0-4)", applied=n_each, converted=low_conv),
        ],
    )


def test_no_nudge_below_min_samples():
    rep = _band_report(0.5, 0.1, n_each=5)  # 10 total < 20
    assert recommend_weight_nudges(rep) == []


def test_positive_nudge_when_high_band_converts_better():
    rep = _band_report(0.40, 0.10)  # gap +0.30
    nudges = recommend_weight_nudges(rep)
    assert len(nudges) == 1
    assert nudges[0].direction == +1
    assert nudges[0].axis == "skills"


def test_negative_nudge_when_high_band_converts_worse():
    rep = _band_report(0.10, 0.40)  # gap -0.30
    nudges = recommend_weight_nudges(rep)
    assert len(nudges) == 1
    assert nudges[0].direction == -1


def test_no_nudge_on_small_gap():
    rep = _band_report(0.30, 0.25)  # gap +0.05 < 0.10
    assert recommend_weight_nudges(rep) == []


def test_min_samples_override():
    rep = _band_report(0.5, 0.1, n_each=5)  # 10 total
    assert recommend_weight_nudges(rep, min_samples=10) != []


def test_score_bands_cover_full_range():
    # Sanity: bands are contiguous and cover 0..10.
    assert SCORE_BANDS[0][1] == 0.0
    assert SCORE_BANDS[-1][2] >= 10.0
