"""JobSpy discovery wrapper for browser-board sources (spec §6a, Phase 2).

JobSpy (`python-jobspy`) scrapes Indeed/ZipRecruiter/Glassdoor/Google/LinkedIn through a
single interface, returning a pandas DataFrame of postings. Per
``research/prior-art-and-methodology.md`` S3, the smoketest confirmed Indeed has no rate
limiting and works out of the box; ZipRecruiter is similar. **LinkedIn is intentionally
NOT included in v3's default site list** — its TLS fingerprinting defeats stealth even
through scrapers, and v3 cut LinkedIn end-to-end ([[project_linkedin_research_2026-04-14]]).

Discovery-only — there is no JobSpy apply path. Each row's ``site`` becomes the discovered
:class:`auto_applier.domain.models.Job`'s ``source`` (``indeed`` / ``zip_recruiter`` / ...). Since
no apply driver is registered for those sources, they pass through the apply worker's
unknown-source silent-skip branch — by design. Browser-board *applying* is a future
Phase-3 capability (manual login + behavioral score the same way ATSes work, but with the
session-expiry handling in spec §8b).

Lazy-imported: ``scrape_jobs`` is imported the first time you call ``discover()`` so the
core ``av3`` install stays light (jobspy → pandas). Install with::

    pip install -e ".[v3,jobspy]"

Tests inject a fake scraper (``_scraper`` constructor arg) so they never need pandas.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any, Callable

__all__ = [
    "DEFAULT_SITES",
    "JobSpyListing",
    "JobSpyQuery",
    "JobSpySource",
]

#: Default discovery surface. LinkedIn omitted (v3 cut) — see module docstring.
DEFAULT_SITES = ("indeed", "zip_recruiter")


@dataclass(frozen=True)
class JobSpyQuery:
    """One discovery search. ``sites`` defaults to Indeed + ZipRecruiter; expand by
    passing an explicit tuple. ``country_indeed`` defaults to USA per the v3.0 US-first
    locale (spec §10 / locale-agnostic eventually, US-first now)."""

    search_term: str
    location: str = "Remote"
    sites: tuple[str, ...] = DEFAULT_SITES
    results_wanted: int = 20
    country_indeed: str = "USA"
    hours_old: int | None = None  # None -> JobSpy's default (~72h on Indeed)


@dataclass
class JobSpyListing:
    """One row from a JobSpy result, normalized to the v3 :class:`Job` shape.

    ``source`` carries the per-row ``site`` (``indeed`` / ``zip_recruiter`` / etc.) so
    cross-source dedup works correctly — JobSpy is a meta-source, not a source the worker
    dispatches on.
    """

    source_job_id: str
    title: str
    company: str
    location: str
    url: str
    description: str = ""
    posted_at: str = ""
    source: str = "indeed"  # per-row, overwritten by JobSpySource
    compensation: str = ""

    @property
    def canonical_hash(self) -> str:
        return ""  # canonical hashing happens at storage time, not here


def _row_value(row: Any, key: str, default: str = "") -> str:
    """Coerce a DataFrame-row cell (or dict value) to a clean string.

    JobSpy uses NaN for missing cells; pandas `.get(k)` returns NaN, not None. We treat
    NaN as missing and return the default. Works for both DataFrame rows (.get) and
    plain dicts (.get) so test fakes don't need pandas.
    """
    try:
        val = row.get(key)
    except Exception:  # noqa: BLE001
        val = None
    if val is None:
        return default
    # NaN check without numpy: NaN != NaN.
    try:
        if val != val:
            return default
    except Exception:  # noqa: BLE001
        return default
    return str(val).strip() or default


def _stable_id(row_id: str, job_url: str) -> str:
    """Pick a stable per-row identifier.

    JobSpy >=1.1.50 emits a stable ``id`` column; older versions don't. Falls back to
    sha256(job_url)[:16] when the id is absent so re-running the same query doesn't
    create duplicate :class:`Job` rows in the DB (dedup keys off
    ``(source, source_job_id)``).
    """
    if row_id:
        return row_id
    if not job_url:
        return ""
    return hashlib.sha256(job_url.encode("utf-8")).hexdigest()[:16]


class JobSpySource:
    """Adapter around ``jobspy.scrape_jobs``.

    Discovery-only — no ``apply_url``, no apply driver. The discovered jobs are visible
    in the queue and surface in the dashboard, but the apply worker's unknown-source skip
    keeps them out of any auto-apply path until a Phase-3 browser-board driver lands.
    """

    source_name = "jobspy"

    def __init__(
        self,
        *,
        scraper: Callable[..., Any] | None = None,
    ):
        """``scraper`` lets tests inject a fake. Production keeps it ``None`` so the real
        ``jobspy.scrape_jobs`` is imported lazily on first ``discover()`` call."""
        self._scraper = scraper

    def _get_scraper(self) -> Callable[..., Any]:
        if self._scraper is not None:
            return self._scraper
        try:
            from jobspy import scrape_jobs  # type: ignore[import-not-found]
        except ImportError as exc:
            raise ImportError(
                "python-jobspy is not installed. Install with "
                "`pip install -e '.[v3,jobspy]'` to enable browser-board discovery."
            ) from exc
        self._scraper = scrape_jobs
        return scrape_jobs

    def discover(self, query: JobSpyQuery) -> list[JobSpyListing]:
        """Run one search. Empty results / scraper exceptions return ``[]`` (the source
        capability is "best-effort"; the discovery pipeline records the count and moves
        on)."""
        scrape = self._get_scraper()
        kwargs: dict[str, Any] = {
            "site_name": list(query.sites),
            "search_term": query.search_term,
            "location": query.location,
            "results_wanted": query.results_wanted,
            "country_indeed": query.country_indeed,
        }
        if query.hours_old is not None:
            kwargs["hours_old"] = query.hours_old

        try:
            df = scrape(**kwargs)
        except Exception:  # noqa: BLE001 — degrade gracefully
            return []

        rows = _to_records(df)
        out: list[JobSpyListing] = []
        for r in rows:
            url = _row_value(r, "job_url")
            row_id = _row_value(r, "id")
            sid = _stable_id(row_id, url)
            if not sid:
                continue  # nothing stable to dedup on -> drop
            out.append(JobSpyListing(
                source_job_id=sid,
                title=_row_value(r, "title"),
                company=_row_value(r, "company"),
                location=_row_value(r, "location"),
                url=url,
                description=_row_value(r, "description"),
                posted_at=_row_value(r, "date_posted"),
                source=_row_value(r, "site", default="indeed"),
                compensation=_row_value(r, "compensation") or _row_value(r, "salary"),
            ))
        return out


def _to_records(df: Any) -> list[Any]:
    """Convert a pandas DataFrame OR a plain list-of-dicts to an iterable of row-likes.

    Production: DataFrames implement ``.to_dict(orient='records')``. Tests pass a plain
    list of dicts so they don't need pandas.
    """
    if df is None:
        return []
    if isinstance(df, list):
        return df
    # Pandas DataFrame
    to_dict = getattr(df, "to_dict", None)
    if to_dict is not None:
        try:
            return to_dict(orient="records")
        except TypeError:
            return to_dict()
    # Anything else (a generator, etc.)
    try:
        return list(df)
    except TypeError:
        return []
