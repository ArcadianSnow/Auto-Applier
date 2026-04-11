"""Tests for pattern analysis."""
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from auto_applier.analysis.patterns import analyze
from auto_applier.storage import repository
from auto_applier.storage.models import Application, Followup, Job, SkillGap


@pytest.fixture
def temp_csvs(tmp_path):
    with patch.object(repository, '_CSV_MAP', {
        Job: tmp_path / "jobs.csv",
        Application: tmp_path / "applications.csv",
        SkillGap: tmp_path / "skill_gaps.csv",
        Followup: tmp_path / "followups.csv",
    }):
        yield tmp_path


def _iso(hour: int, weekday: int) -> str:
    """Build an ISO timestamp at the given (hour, weekday 0=Mon)."""
    # Pick a known reference date: 2026-04-06 is a Monday
    base = datetime(2026, 4, 6, hour=hour, tzinfo=timezone.utc)
    # Shift to the desired weekday
    from datetime import timedelta
    return (base + timedelta(days=weekday)).isoformat()


class TestEmptyStore:
    def test_zero_applications(self, temp_csvs):
        r = analyze()
        assert r.total_applications == 0
        assert r.resume_stats == []
        assert r.platform_stats == []


class TestResumeStats:
    def test_ranks_by_win_rate_then_count(self, temp_csvs):
        # analyst: 4/5 = 80%; engineer: 1/1 = 100%; entry: 0/2 = 0%
        for _ in range(4):
            repository.save(Application(job_id="x", status="applied", source="li", resume_used="analyst"))
        repository.save(Application(job_id="x", status="failed", source="li", resume_used="analyst"))
        repository.save(Application(job_id="y", status="applied", source="li", resume_used="engineer"))
        repository.save(Application(job_id="z", status="failed", source="li", resume_used="entry"))
        repository.save(Application(job_id="z", status="skipped", source="li", resume_used="entry"))

        r = analyze()
        labels_in_order = [row[0] for row in r.resume_stats]
        assert labels_in_order[0] == "engineer"  # 100% rate wins
        assert labels_in_order[1] == "analyst"   # 80% second
        assert labels_in_order[-1] == "entry"    # 0% last


class TestPlatformStats:
    def test_groups_by_source(self, temp_csvs):
        repository.save(Application(job_id="x", status="applied", source="linkedin"))
        repository.save(Application(job_id="x", status="applied", source="linkedin"))
        repository.save(Application(job_id="y", status="failed", source="indeed"))
        r = analyze()
        platforms = {row[0]: row for row in r.platform_stats}
        assert platforms["linkedin"][1] == 2
        assert platforms["linkedin"][3] == 1.0
        assert platforms["indeed"][1] == 0
        assert platforms["indeed"][3] == 0.0


class TestKeywordStats:
    def test_pulls_keyword_from_job(self, temp_csvs):
        job = Job(job_id="j1", title="A", company="C", url="u", source="li", search_keyword="python developer")
        repository.save(job)
        repository.save(Application(job_id="j1", status="applied", source="li"))
        r = analyze()
        assert any(row[0] == "python developer" for row in r.keyword_stats)


class TestTimeStats:
    def test_bucketed_by_hour(self, temp_csvs):
        a = Application(job_id="x", status="applied", source="li")
        a.applied_at = _iso(hour=9, weekday=0)
        repository.save(a)

        b = Application(job_id="y", status="failed", source="li")
        b.applied_at = _iso(hour=14, weekday=0)
        repository.save(b)

        r = analyze()
        hours = {h: (applied, total) for h, applied, total, _ in r.hour_stats}
        assert hours.get(9) == (1, 1)
        assert hours.get(14) == (0, 1)

    def test_bucketed_by_day_of_week(self, temp_csvs):
        a = Application(job_id="x", status="applied", source="li")
        a.applied_at = _iso(hour=9, weekday=0)  # Monday
        repository.save(a)

        r = analyze()
        days = {d: total for d, applied, total, _ in r.dow_stats}
        assert days.get("Mon") == 1


class TestScoreBuckets:
    def test_maps_scores_to_buckets(self, temp_csvs):
        repository.save(Application(job_id="x", status="applied", source="li", score=9))
        repository.save(Application(job_id="y", status="applied", source="li", score=5))
        repository.save(Application(job_id="z", status="failed", source="li", score=2))
        r = analyze()
        bucket_names = {row[0] for row in r.score_buckets}
        assert any("9-10" in b for b in bucket_names)
        assert any("5-6" in b for b in bucket_names)
        assert any("0-2" in b for b in bucket_names)


class TestDeadListings:
    def test_counts_by_source(self, temp_csvs):
        repository.save(Application(
            job_id="x", status="skipped", source="linkedin",
            failure_reason="dead listing",
        ))
        repository.save(Application(
            job_id="y", status="skipped", source="linkedin",
            failure_reason="dead listing",
        ))
        repository.save(Application(
            job_id="z", status="skipped", source="indeed",
            failure_reason="dead listing",
        ))
        r = analyze()
        dead = dict(r.dead_listing_sources)
        assert dead["linkedin"] == 2
        assert dead["indeed"] == 1


class TestTopGaps:
    def test_most_common(self, temp_csvs):
        for _ in range(5):
            repository.save(SkillGap(job_id="x", field_label="years of kubernetes"))
        for _ in range(2):
            repository.save(SkillGap(job_id="y", field_label="work auth status"))
        r = analyze()
        top = r.top_gaps[0]
        assert top[0] == "years of kubernetes"
        assert top[1] == 5
