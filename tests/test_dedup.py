"""Tests for canonical job hashing and cross-source dedup."""
import pytest
from unittest.mock import patch

from auto_applier.storage.dedup import (
    canonical_job_hash,
    normalize_company,
    normalize_title,
)
from auto_applier.storage.models import Job, Application
from auto_applier.storage import repository


class TestNormalizeCompany:
    @pytest.mark.parametrize("input_name,expected", [
        ("Acme Inc.", "acme"),
        ("Acme, Inc", "acme"),
        ("Acme Corporation", "acme"),
        ("Acme LLC", "acme"),
        ("ACME CORP", "acme"),
        ("Acme Holdings, Inc.", "acme"),
        ("Acme Group", "acme"),
        ("Big Data Co.", "big data"),
        ("Google LLC", "google"),
        ("Meta Platforms, Inc.", "meta platforms"),
        ("  Acme   Inc  ", "acme"),
        ("", ""),
    ])
    def test_normalization(self, input_name, expected):
        assert normalize_company(input_name) == expected


class TestNormalizeTitle:
    @pytest.mark.parametrize("input_title,expected", [
        ("Senior Data Analyst", "senior data analyst"),
        ("Senior Data Analyst (Remote)", "senior data analyst"),
        ("Senior Data Analyst (Hybrid)", "senior data analyst"),
        ("Data Analyst - Remote", "data analyst"),
        ("Data Analyst, Contract", "data analyst"),
        ("DATA ANALYST", "data analyst"),
        ("Data Engineer (Remote, US)", "data engineer"),
        ("  Data   Engineer  ", "data engineer"),
        ("", ""),
    ])
    def test_normalization(self, input_title, expected):
        assert normalize_title(input_title) == expected


class TestCanonicalHash:
    def test_same_company_same_title_match(self):
        a = canonical_job_hash("Acme Inc.", "Senior Data Analyst")
        b = canonical_job_hash("ACME Corp", "Senior Data Analyst")
        assert a == b
        assert a != ""

    def test_remote_variants_match(self):
        a = canonical_job_hash("Acme", "Data Engineer (Remote)")
        b = canonical_job_hash("Acme", "Data Engineer - Remote")
        c = canonical_job_hash("Acme", "Data Engineer")
        assert a == b == c

    def test_different_companies_differ(self):
        a = canonical_job_hash("Acme", "Data Analyst")
        b = canonical_job_hash("Globex", "Data Analyst")
        assert a != b

    def test_different_titles_differ(self):
        a = canonical_job_hash("Acme", "Data Analyst")
        b = canonical_job_hash("Acme", "Data Engineer")
        assert a != b

    def test_empty_inputs_return_empty(self):
        assert canonical_job_hash("", "Data Analyst") == ""
        assert canonical_job_hash("Acme", "") == ""
        assert canonical_job_hash("", "") == ""

    def test_hash_is_stable(self):
        a = canonical_job_hash("Acme Inc.", "Data Analyst")
        b = canonical_job_hash("Acme Inc.", "Data Analyst")
        assert a == b

    def test_hash_is_short(self):
        h = canonical_job_hash("Acme Inc.", "Senior Data Analyst")
        assert len(h) == 16
        assert all(c in "0123456789abcdef" for c in h)


class TestJobPostInit:
    def test_canonical_hash_auto_populated(self):
        job = Job(
            job_id="li-1",
            title="Senior Data Analyst",
            company="Acme Inc.",
            url="https://example.com",
        )
        assert job.canonical_hash != ""
        assert len(job.canonical_hash) == 16

    def test_same_cross_source_same_hash(self):
        a = Job(job_id="li-1", title="Senior Data Analyst", company="Acme Inc.", url="u", source="linkedin")
        b = Job(job_id="ind-99", title="Senior Data Analyst", company="ACME Corp", url="u", source="indeed")
        assert a.canonical_hash == b.canonical_hash
        assert a.job_id != b.job_id  # different platform IDs
        assert a.source != b.source

    def test_empty_when_missing_fields(self):
        job = Job(job_id="x", title="", company="", url="u")
        assert job.canonical_hash == ""

    def test_explicit_hash_preserved(self):
        job = Job(
            job_id="x", title="T", company="C", url="u",
            canonical_hash="deadbeefcafebabe",
        )
        assert job.canonical_hash == "deadbeefcafebabe"


class TestCrossSourceDedup:
    @pytest.fixture
    def temp_csvs(self, tmp_path):
        with patch.object(repository, '_CSV_MAP', {
            Job: tmp_path / "jobs.csv",
            Application: tmp_path / "applications.csv",
            __import__("auto_applier.storage.models", fromlist=["SkillGap"]).SkillGap: tmp_path / "skill_gaps.csv",
        }):
            yield tmp_path

    def test_empty_hash_returns_false(self, temp_csvs):
        assert repository.job_seen_canonically("") is False

    def test_unseen_hash_returns_false(self, temp_csvs):
        repository.save(Job(job_id="li-1", title="Analyst", company="Acme", url="u", source="linkedin"))
        assert repository.job_seen_canonically("abcdef0123456789") is False

    def test_unscored_job_does_not_dedupe(self, temp_csvs):
        """Phase A: scraped-but-never-scored Jobs must NOT dedupe.
        Continuous-run mode relies on this — cycle 1 may only score
        3 of 99 scraped jobs, cycle 2 must still find the other 96.
        """
        job = Job(
            job_id="li-1", title="Senior Data Analyst",
            company="Acme Inc.", url="u", source="linkedin",
        )
        repository.save(job)
        # No Application saved → job is not yet "processed"
        assert repository.job_seen_canonically(job.canonical_hash) is False

    def test_scored_job_dedupes_across_sources(self, temp_csvs):
        """A Job with any Application row (even skipped) dedupes its
        canonical_hash everywhere — that's the cross-source guard."""
        job = Job(
            job_id="li-1", title="Senior Data Analyst",
            company="Acme Inc.", url="u", source="linkedin",
        )
        repository.save(job)
        repository.save(Application(job_id="li-1", source="linkedin", status="skipped"))
        dup = Job(
            job_id="ind-99", title="Senior Data Analyst",
            company="ACME Corp", url="u2", source="indeed",
        )
        assert dup.canonical_hash == job.canonical_hash
        assert repository.job_seen_canonically(dup.canonical_hash) is True

    def test_applied_job_dedupes(self, temp_csvs):
        """Backwards-compat: applied Applications dedup just like
        skipped ones (any Application row is enough)."""
        job = Job(
            job_id="li-1", title="Senior Data Analyst",
            company="Acme Inc.", url="u", source="linkedin",
        )
        repository.save(job)
        repository.save(Application(job_id="li-1", source="linkedin", status="applied"))
        assert repository.job_seen_canonically(job.canonical_hash) is True

    def test_different_titles_not_deduped(self, temp_csvs):
        repository.save(Job(job_id="li-1", title="Analyst", company="Acme", url="u", source="linkedin"))
        repository.save(Application(job_id="li-1", source="linkedin", status="skipped"))
        other = Job(job_id="ind-99", title="Engineer", company="Acme", url="u", source="indeed")
        assert repository.job_seen_canonically(other.canonical_hash) is False
