"""JobSpy discovery wrapper tests (spec section 6a, Phase 2).

Stays pure-Python: injects a fake ``scrape_jobs`` so the tests never need
``python-jobspy`` (and its pandas transitive dep) installed. The wrapper accepts both
DataFrame-like and list-of-dicts inputs from ``_to_records``, so the fakes return plain
dicts.

What's covered:
  * Happy path: query → list of :class:`JobSpyListing` with the per-row ``site`` becoming
    each listing's ``source`` (so cross-source dedup behaves).
  * Stable ID fallback when JobSpy doesn't emit an ``id`` column (older versions).
  * NaN / missing-cell tolerance (pandas uses NaN for empties).
  * Empty result / scraper exception → ``[]`` (best-effort discovery).
  * Default sites = Indeed + ZipRecruiter ONLY (LinkedIn cut from v3).
  * Lazy import: clean ``ImportError`` if ``python-jobspy`` is absent and no scraper
    was injected.
"""

from __future__ import annotations

import math

import pytest

from av3.sources.jobspy import (
    DEFAULT_SITES,
    JobSpyListing,
    JobSpyQuery,
    JobSpySource,
)


# --------------------------------------------------------------- happy path

def _make_fake_scraper(rows):
    """Return a callable matching ``jobspy.scrape_jobs`` signature; records its kwargs."""
    captured = {}

    def _scraper(**kwargs):
        captured.update(kwargs)
        return rows

    _scraper.captured = captured  # type: ignore[attr-defined]
    return _scraper


def test_discover_normalizes_row_into_listing():
    rows = [{
        "id": "ind-1",
        "title": "Senior Data Analyst",
        "company": "Cigna",
        "location": "Remote (US)",
        "date_posted": "2026-05-25",
        "job_url": "https://www.indeed.com/viewjob?jk=abc123",
        "site": "indeed",
        "description": "Build dashboards.",
        "compensation": "$120k-$160k",
    }]
    src = JobSpySource(scraper=_make_fake_scraper(rows))

    out = src.discover(JobSpyQuery(search_term="data analyst"))

    assert len(out) == 1
    listing = out[0]
    assert isinstance(listing, JobSpyListing)
    assert listing.source_job_id == "ind-1"
    assert listing.title == "Senior Data Analyst"
    assert listing.company == "Cigna"
    assert listing.source == "indeed"  # per-row site, NOT the wrapper's source_name
    assert listing.compensation == "$120k-$160k"
    assert listing.url == "https://www.indeed.com/viewjob?jk=abc123"


def test_discover_threads_query_kwargs_into_scraper():
    fake = _make_fake_scraper(rows=[])
    src = JobSpySource(scraper=fake)
    src.discover(JobSpyQuery(
        search_term="data engineer",
        location="Austin, TX",
        sites=("indeed",),
        results_wanted=50,
        country_indeed="USA",
        hours_old=24,
    ))
    assert fake.captured == {
        "site_name": ["indeed"],
        "search_term": "data engineer",
        "location": "Austin, TX",
        "results_wanted": 50,
        "country_indeed": "USA",
        "hours_old": 24,
    }


def test_default_sites_exclude_linkedin():
    """v3 cut LinkedIn (TLS fingerprinting defeats stealth) — must NOT appear in the
    default site list, even though JobSpy supports it."""
    assert "linkedin" not in DEFAULT_SITES
    assert "indeed" in DEFAULT_SITES
    assert "zip_recruiter" in DEFAULT_SITES


def test_hours_old_omitted_when_none():
    """``hours_old=None`` means 'use JobSpy's default' — don't pass the kwarg through
    (passing None would force JobSpy to dedup an arbitrary default that may change)."""
    fake = _make_fake_scraper(rows=[])
    src = JobSpySource(scraper=fake)
    src.discover(JobSpyQuery(search_term="x"))
    assert "hours_old" not in fake.captured


# --------------------------------------------------------------- robustness

def test_stable_id_fallback_when_jobspy_omits_id_column():
    """Older JobSpy versions don't emit an ``id`` — must derive a stable id from the
    job_url so re-runs of the same query dedup correctly."""
    rows = [{
        "title": "X", "company": "Y", "location": "Z",
        "job_url": "https://www.indeed.com/viewjob?jk=abc123",
        "site": "indeed",
    }]
    src = JobSpySource(scraper=_make_fake_scraper(rows))
    [first] = src.discover(JobSpyQuery(search_term="x"))
    [again] = src.discover(JobSpyQuery(search_term="x"))
    assert first.source_job_id == again.source_job_id
    assert first.source_job_id  # not empty


def test_row_with_no_id_and_no_url_is_dropped():
    """Nothing stable to dedup on -> drop the row rather than emit a duplicate-prone
    listing."""
    rows = [{"title": "X", "company": "Y", "location": "Z", "site": "indeed"}]
    src = JobSpySource(scraper=_make_fake_scraper(rows))
    assert src.discover(JobSpyQuery(search_term="x")) == []


def test_nan_cells_are_treated_as_missing():
    """JobSpy uses pandas NaN for empties; the row coercer must treat NaN as missing
    and not crash on the string conversion."""
    rows = [{
        "id": "ind-1",
        "title": "X",
        "company": "Y",
        "location": "Z",
        "job_url": "https://example.com/j/1",
        "site": "indeed",
        "description": math.nan,        # missing description
        "date_posted": math.nan,        # missing date
    }]
    src = JobSpySource(scraper=_make_fake_scraper(rows))
    [listing] = src.discover(JobSpyQuery(search_term="x"))
    assert listing.description == ""
    assert listing.posted_at == ""


def test_empty_result_returns_empty_list():
    src = JobSpySource(scraper=_make_fake_scraper(rows=[]))
    assert src.discover(JobSpyQuery(search_term="x")) == []


def test_scraper_exception_degrades_to_empty_list():
    """Discovery is best-effort; a network/library failure must NOT crash the pipeline."""

    def _boom(**_kwargs):
        raise RuntimeError("scraper exploded")

    src = JobSpySource(scraper=_boom)
    assert src.discover(JobSpyQuery(search_term="x")) == []


def test_lazy_import_raises_clear_error_when_jobspy_absent(monkeypatch):
    """No injected scraper + jobspy not installed -> a clear ImportError pointing the
    user at the install command."""
    src = JobSpySource()  # no scraper injected -> tries the real import
    # Force the import to fail even if jobspy *is* installed on this machine.
    import sys
    monkeypatch.setitem(sys.modules, "jobspy", None)
    with pytest.raises(ImportError) as exc:
        src.discover(JobSpyQuery(search_term="x"))
    assert "python-jobspy" in str(exc.value)
    assert "install" in str(exc.value).lower()


# --------------------------------------------------------------- multi-site routing

def test_each_row_carries_its_own_source_for_cross_site_dedup():
    """When a single query returns rows from Indeed AND ZipRecruiter, each listing's
    ``source`` must reflect the per-row ``site`` — the worker dispatches by source so
    they never get conflated."""
    rows = [
        {"id": "ind-1", "title": "X", "company": "A", "location": "L",
         "job_url": "https://www.indeed.com/j/1", "site": "indeed"},
        {"id": "zip-1", "title": "X", "company": "A", "location": "L",
         "job_url": "https://www.ziprecruiter.com/j/1", "site": "zip_recruiter"},
    ]
    src = JobSpySource(scraper=_make_fake_scraper(rows))
    listings = src.discover(JobSpyQuery(search_term="x"))
    sources = {l.source for l in listings}
    assert sources == {"indeed", "zip_recruiter"}


def test_pandas_dataframe_input_is_accepted_via_to_dict_records():
    """Production gets a DataFrame; tests that DO have pandas can pass it directly. The
    wrapper's ``_to_records`` must call ``.to_dict(orient='records')``."""

    class _FakeDataFrame:
        def __init__(self, rows):
            self.rows = rows
            self.calls = []

        def to_dict(self, orient=None):
            self.calls.append(orient)
            return self.rows

    fake_df = _FakeDataFrame([
        {"id": "ind-1", "title": "X", "company": "Y", "location": "Z",
         "job_url": "https://example.com/1", "site": "indeed"},
    ])
    src = JobSpySource(scraper=lambda **_kw: fake_df)
    [listing] = src.discover(JobSpyQuery(search_term="x"))
    assert listing.source == "indeed"
    assert fake_df.calls == ["records"]


# --------------------------------------------------------------- listing fields

def test_listing_canonical_hash_is_empty_string():
    """canonical_hash is computed at storage time (cross-source title+company normalization),
    NOT at JobSpy discovery — different responsibilities, different layers."""
    listing = JobSpyListing(
        source_job_id="x", title="t", company="c", location="l",
        url="https://example.com",
    )
    assert listing.canonical_hash == ""
