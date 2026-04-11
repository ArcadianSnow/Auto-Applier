"""Pattern analysis over the application history.

Read-only analytics that surface which resumes / platforms / keywords
/ time windows have actually produced successful applications. Runs
entirely offline — no LLM, no network — so it can be invoked any
time to inform the next session's search strategy.

The analyzer never makes judgements on sample size. A single
application at 80% success rate will rank highly if nothing else
exists. It's the user's job (and the report's job) to weight the
numbers against the sample size column.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime

from auto_applier.storage import repository
from auto_applier.storage.models import Application, Job, SkillGap


@dataclass
class PatternReport:
    total_applications: int
    applied: int
    failed: int
    skipped: int
    dry_run: int
    resume_stats: list[tuple[str, int, int, float]]     # (label, applied, total, win_rate)
    platform_stats: list[tuple[str, int, int, float]]   # (source, applied, total, win_rate)
    keyword_stats: list[tuple[str, int, int, float]]    # (keyword, applied, total, win_rate)
    hour_stats: list[tuple[int, int, int, float]]       # (hour, applied, total, win_rate)
    dow_stats: list[tuple[str, int, int, float]]        # (day-of-week, applied, total, win_rate)
    score_buckets: list[tuple[str, int, int, float]]    # ("0-2", "3-4", ...)
    top_gaps: list[tuple[str, int]]                     # (field_label, count)
    dead_listing_sources: list[tuple[str, int]]         # (source, dead_count)


def _bucket_score(score: int) -> str:
    if score <= 2:
        return "0-2 (skip)"
    if score <= 4:
        return "3-4 (review)"
    if score <= 6:
        return "5-6 (marginal)"
    if score <= 8:
        return "7-8 (good)"
    return "9-10 (great)"


def _win_rate(applied: int, total: int) -> float:
    return (applied / total) if total > 0 else 0.0


def _rank_by_rate(
    counter_applied: Counter, counter_total: Counter,
) -> list[tuple[str, int, int, float]]:
    """Return sorted list of (key, applied, total, win_rate), best first."""
    rows = []
    for key, total in counter_total.items():
        applied = counter_applied.get(key, 0)
        rows.append((key, applied, total, _win_rate(applied, total)))
    # Sort by (win_rate desc, applied desc) so ties break on raw count
    rows.sort(key=lambda r: (-r[3], -r[1]))
    return rows


def analyze() -> PatternReport:
    """Build a pattern report from the current CSVs."""
    apps = repository.load_all(Application)
    jobs = {(j.job_id, j.source): j for j in repository.load_all(Job)}
    gaps = repository.load_all(SkillGap)

    status_counts = Counter(a.status for a in apps)

    resume_applied: Counter = Counter()
    resume_total: Counter = Counter()
    platform_applied: Counter = Counter()
    platform_total: Counter = Counter()
    keyword_applied: Counter = Counter()
    keyword_total: Counter = Counter()
    hour_applied: Counter = Counter()
    hour_total: Counter = Counter()
    dow_applied: Counter = Counter()
    dow_total: Counter = Counter()
    bucket_applied: Counter = Counter()
    bucket_total: Counter = Counter()
    dead_by_source: Counter = Counter()

    dow_names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

    for a in apps:
        is_applied = a.status == "applied"
        resume = a.resume_used or "(unspecified)"
        resume_total[resume] += 1
        if is_applied:
            resume_applied[resume] += 1

        platform = a.source or "(unspecified)"
        platform_total[platform] += 1
        if is_applied:
            platform_applied[platform] += 1

        # Pull keyword from the matching Job
        job = jobs.get((a.job_id, a.source))
        if job and job.search_keyword:
            keyword_total[job.search_keyword] += 1
            if is_applied:
                keyword_applied[job.search_keyword] += 1

        # Time-of-day bucketing
        try:
            ts = datetime.fromisoformat(a.applied_at)
            hour_total[ts.hour] += 1
            dow_total[dow_names[ts.weekday()]] += 1
            if is_applied:
                hour_applied[ts.hour] += 1
                dow_applied[dow_names[ts.weekday()]] += 1
        except (ValueError, TypeError):
            pass

        # Score buckets
        bucket = _bucket_score(a.score or 0)
        bucket_total[bucket] += 1
        if is_applied:
            bucket_applied[bucket] += 1

        if a.failure_reason == "dead listing":
            dead_by_source[a.source or "(unspecified)"] += 1

    top_gaps = Counter(g.field_label for g in gaps).most_common(15)

    return PatternReport(
        total_applications=len(apps),
        applied=status_counts.get("applied", 0),
        failed=status_counts.get("failed", 0),
        skipped=status_counts.get("skipped", 0),
        dry_run=status_counts.get("dry_run", 0),
        resume_stats=_rank_by_rate(resume_applied, resume_total),
        platform_stats=_rank_by_rate(platform_applied, platform_total),
        keyword_stats=_rank_by_rate(keyword_applied, keyword_total)[:20],
        hour_stats=[
            (h, hour_applied.get(h, 0), hour_total[h],
             _win_rate(hour_applied.get(h, 0), hour_total[h]))
            for h in sorted(hour_total)
        ],
        dow_stats=[
            (d, dow_applied.get(d, 0), dow_total[d],
             _win_rate(dow_applied.get(d, 0), dow_total[d]))
            for d in dow_names if d in dow_total
        ],
        score_buckets=_rank_by_rate(bucket_applied, bucket_total),
        top_gaps=top_gaps,
        dead_listing_sources=dead_by_source.most_common(),
    )
