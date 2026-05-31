"""Outcome feedback analytics (spec §8e) — Phase 6 / v3.1.

Turns recorded :class:`auto_applier.domain.models.Outcome` rows into **read-only insights**: which
sources, titles, and score-bands actually convert. This is the "gets smarter over time"
mechanism — but the *acting* on those insights is deliberately split:

  * **This module is pure + read-only** (the "gather" half, Rule 2.6). It aggregates the
    APPLIED-jobs-×-outcomes feed into conversion stats and produces a **weight-nudge
    *recommendation*** — it never mutates ``settings.scoring`` itself.
  * **Applying a nudge is an "act"** (it changes what the bot applies to next), so it stays
    behind the user: ``av3 analytics`` shows the recommendation; the user edits config (or a
    future gated command applies it). Auto-mutating scoring weights from sparse early data
    is exactly the compounding mistake §8e's "bounded auto-tuning" + the project's
    research-first discipline guard against — so v3.1 ships the recommendation, not the
    silent rewrite. See `research/phase6-v3.1.md` §(4/M).

"Conversion" = a positive outcome (response / interview / offer) on an APPLIED job. A job
with no recorded outcome counts as a non-conversion (the "applied, silent" denominator) —
so rates are honest even before the user backfills outcomes.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from auto_applier.domain.models import Job
from auto_applier.domain.state import OutcomeKind
from auto_applier.reconcile import extract_candidate_skills

__all__ = [
    "GroupStat",
    "ConversionReport",
    "WeightNudge",
    "SkillGapTrend",
    "compute_conversion_report",
    "recommend_weight_nudges",
    "compute_skill_gap_trends",
    "SCORE_BANDS",
    "HIGH_BAND_MIN",
]

#: A job at/above this score is "high-fit" — a gap demanded by these is more worth
#: learning than one demanded only by marginal jobs. Matches the top SCORE_BAND floor.
HIGH_BAND_MIN = 7.0

#: Score-band buckets (label, low-inclusive, high-exclusive) over the 0–10 scale. The top
#: band is high-inclusive so a perfect 10 lands somewhere. Mirrors the §10 decision bands
#: loosely (low / mid / high) without coupling to the exact thresholds (those are tunable).
SCORE_BANDS: tuple[tuple[str, float, float], ...] = (
    ("low[0-4)", 0.0, 4.0),
    ("mid[4-7)", 4.0, 7.0),
    ("high[7-10]", 7.0, 10.0001),
)


def _band_for(score: float | None) -> str:
    if score is None:
        return "unscored"
    for label, lo, hi in SCORE_BANDS:
        if lo <= score < hi:
            return label
    return "unscored"


@dataclass(frozen=True)
class GroupStat:
    """Conversion stats for one group (a source, a title, or a score-band).

    ``applied`` = APPLIED jobs in the group; ``converted`` = those with a positive outcome
    (response/interview/offer); ``rate`` = converted/applied (0.0 when applied==0). ``ghosted``
    counts jobs whose furthest outcome is GHOST (or no outcome at all → implicit ghost)."""

    key: str
    applied: int = 0
    converted: int = 0
    ghosted: int = 0

    @property
    def rate(self) -> float:
        return (self.converted / self.applied) if self.applied else 0.0


@dataclass
class ConversionReport:
    """The full §8e read-model: conversion stats sliced by source / title / score-band,
    plus the raw outcome-kind tally. Pure data — the CLI/dashboard render it."""

    total_applied: int = 0
    total_converted: int = 0
    by_source: list[GroupStat] = field(default_factory=list)
    by_title: list[GroupStat] = field(default_factory=list)
    by_band: list[GroupStat] = field(default_factory=list)
    outcome_counts: dict[str, int] = field(default_factory=dict)

    @property
    def overall_rate(self) -> float:
        return (self.total_converted / self.total_applied) if self.total_applied else 0.0


def _furthest_per_job(feed: list[dict]) -> dict[str, dict]:
    """Collapse the (job × outcomes) feed to one record per job, keeping the
    furthest-reached outcome (by :attr:`OutcomeKind.rank`). A job with no outcome row keeps
    ``kind=None``. Carries source/title/score through for grouping."""
    per_job: dict[str, dict] = {}
    for row in feed:
        jid = row["job_id"]
        kind_raw = row.get("kind")
        kind = OutcomeKind(kind_raw) if kind_raw else None
        cur = per_job.get(jid)
        if cur is None:
            per_job[jid] = {
                "source": row.get("source") or "",
                "title": row.get("title") or "",
                "score": row.get("score"),
                "kind": kind,
            }
        elif kind is not None and (cur["kind"] is None or kind.rank > cur["kind"].rank):
            cur["kind"] = kind
    return per_job


def _accumulate(groups: dict[str, dict[str, int]], key: str, *, converted: bool, ghosted: bool) -> None:
    g = groups.setdefault(key, {"applied": 0, "converted": 0, "ghosted": 0})
    g["applied"] += 1
    if converted:
        g["converted"] += 1
    if ghosted:
        g["ghosted"] += 1


def _to_statlist(groups: dict[str, dict[str, int]]) -> list[GroupStat]:
    """Sort by conversion rate desc, then volume desc — best-converting groups first."""
    stats = [
        GroupStat(key=k, applied=v["applied"], converted=v["converted"], ghosted=v["ghosted"])
        for k, v in groups.items()
    ]
    stats.sort(key=lambda s: (s.rate, s.applied), reverse=True)
    return stats


def compute_conversion_report(feed: list[dict]) -> ConversionReport:
    """Aggregate the ``OutcomeRepo.applied_with_outcomes()`` feed into a
    :class:`ConversionReport`. Pure function — no I/O, deterministic, fully unit-testable.

    A job converts iff its furthest outcome is positive (response/interview/offer). A job
    is ghosted iff its furthest outcome is GHOST **or it has no recorded outcome at all**
    (applied-and-silent is the implicit ghost — honest denominator)."""
    per_job = _furthest_per_job(feed)

    by_source: dict[str, dict[str, int]] = {}
    by_title: dict[str, dict[str, int]] = {}
    by_band: dict[str, dict[str, int]] = {}
    outcome_counts: dict[str, int] = {}
    total_applied = 0
    total_converted = 0

    for rec in per_job.values():
        kind: OutcomeKind | None = rec["kind"]
        converted = kind is not None and kind.is_positive
        ghosted = kind is None or kind is OutcomeKind.GHOST
        total_applied += 1
        if converted:
            total_converted += 1
        if kind is not None:
            outcome_counts[kind.value] = outcome_counts.get(kind.value, 0) + 1

        _accumulate(by_source, rec["source"] or "(unknown)", converted=converted, ghosted=ghosted)
        _accumulate(by_title, rec["title"] or "(unknown)", converted=converted, ghosted=ghosted)
        _accumulate(by_band, _band_for(rec["score"]), converted=converted, ghosted=ghosted)

    return ConversionReport(
        total_applied=total_applied,
        total_converted=total_converted,
        by_source=_to_statlist(by_source),
        by_title=_to_statlist(by_title),
        by_band=_to_statlist(by_band),
        outcome_counts=outcome_counts,
    )


@dataclass(frozen=True)
class WeightNudge:
    """A *suggested* scoring-weight adjustment (spec §8e "gently auto-tune"). A RECOMMENDATION
    ONLY — never auto-applied. ``direction`` is +1 (lean into this) / -1 (lean away).
    ``rationale`` explains the data behind it for the dashboard / CLI."""

    axis: str
    direction: int           # +1 raise weight, -1 lower weight
    rationale: str


#: Minimum APPLIED jobs before we'll even suggest a nudge. Below this, conversion data is
#: noise — suggesting a weight change off 2 data points is the §8e anti-pattern. Conservative
#: on purpose; the user can lower it once they've accrued history.
MIN_SAMPLES_FOR_NUDGE = 20


def recommend_weight_nudges(
    report: ConversionReport, *, min_samples: int = MIN_SAMPLES_FOR_NUDGE
) -> list[WeightNudge]:
    """Suggest scoring-weight nudges from a conversion report (spec §8e bounded auto-tuning).

    **Pure + advisory** — returns suggestions; applying them is a gated user action (the
    "act" half, Rule 2.6). Deliberately minimal in v3.1: the one signal we trust is the
    **score-band conversion shape**. If high-band jobs convert materially better than
    low-band, the scoring is discriminating well → suggest leaning harder on it (a +skills
    nudge, the dominant axis). If high-band converts *worse* than low-band, the model is
    mis-ranking → suggest the inverse. Below ``min_samples`` total applied, return nothing
    (don't tune on noise).

    Richer per-axis attribution (which of the 7 axes actually predicts conversion) needs a
    real regression and more data than v3.1 will have early — explicitly out of scope; this
    is the honest, bounded version. Recorded in `research/phase6-v3.1.md` §(4/M).
    """
    if report.total_applied < min_samples:
        return []

    bands = {s.key: s for s in report.by_band}
    high = bands.get("high[7-10]")
    low = bands.get("low[0-4)")
    if high is None or low is None or high.applied == 0 or low.applied == 0:
        return []

    # Material gap = 10 percentage points, so a tiny wobble doesn't trigger a suggestion.
    gap = high.rate - low.rate
    if gap >= 0.10:
        return [WeightNudge(
            axis="skills", direction=+1,
            rationale=(
                f"high-band converts {high.rate:.0%} vs low-band {low.rate:.0%} "
                f"({high.applied}+{low.applied} jobs) — scoring discriminates well; "
                f"consider leaning harder on the dominant axis"
            ),
        )]
    if gap <= -0.10:
        return [WeightNudge(
            axis="skills", direction=-1,
            rationale=(
                f"high-band converts {high.rate:.0%} BELOW low-band {low.rate:.0%} "
                f"({high.applied}+{low.applied} jobs) — scoring may be mis-ranking; "
                f"review axis weights"
            ),
        )]
    return []


# --------------------------------------------------------------- what-to-learn trends (§7b/§10)

@dataclass(frozen=True)
class SkillGapTrend:
    """A skill the user lacks, ranked by *demand from high-fit jobs* (spec §10 "what to
    learn next" trends). ``demand_count`` = jobs whose JD demands it; ``high_fit_count`` =
    those scoring ≥ :data:`HIGH_BAND_MIN`; ``avg_demanding_score`` = mean score of the
    demanding jobs (None when none were scored). Ranking key is ``high_fit_count`` then
    ``demand_count`` — learning a skill that unlocks jobs you already fit best pays off most."""

    skill: str
    demand_count: int
    high_fit_count: int
    avg_demanding_score: float | None


def compute_skill_gap_trends(
    jobs: list[Job],
    scores_by_job_id: dict[str, float],
    bank_skills: list[str],
    *,
    vocabulary: tuple[str, ...] | None = None,
    top: int | None = None,
) -> list[SkillGapTrend]:
    """Rank skills the bank lacks by demand from high-fit jobs (spec §10/§7b, pure).

    For every job with a JD, extract demanded skills (deterministic, via
    :func:`auto_applier.reconcile.extract_candidate_skills`), drop those already in the bank, and
    accumulate per-skill demand count + how many demanding jobs are high-fit (score ≥
    :data:`HIGH_BAND_MIN`) + the mean demanding-job score. Ranked high_fit desc, then demand
    desc, then name. ``top`` caps the list. No I/O — the CLI supplies the jobs + scores."""
    have = {s.strip().lower() for s in bank_skills if s and s.strip()}
    demand: dict[str, int] = {}
    high_fit: dict[str, int] = {}
    score_sum: dict[str, float] = {}
    score_n: dict[str, int] = {}

    for job in jobs:
        demanded = extract_candidate_skills(job.description or "", vocabulary)
        score = scores_by_job_id.get(job.id)
        for skill in demanded:
            if skill.lower() in have:
                continue
            demand[skill] = demand.get(skill, 0) + 1
            if score is not None:
                score_sum[skill] = score_sum.get(skill, 0.0) + score
                score_n[skill] = score_n.get(skill, 0) + 1
                if score >= HIGH_BAND_MIN:
                    high_fit[skill] = high_fit.get(skill, 0) + 1

    trends = [
        SkillGapTrend(
            skill=skill,
            demand_count=demand[skill],
            high_fit_count=high_fit.get(skill, 0),
            avg_demanding_score=(
                round(score_sum[skill] / score_n[skill], 2) if score_n.get(skill) else None
            ),
        )
        for skill in demand
    ]
    trends.sort(key=lambda t: (t.high_fit_count, t.demand_count, t.skill), reverse=True)
    return trends[:top] if top is not None else trends
