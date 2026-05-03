"""Base class for ATS public-API discovery adapters.

Many companies host their job board through one of a small number
of Applicant Tracking Systems (Greenhouse, Lever, Ashby, Workable,
SmartRecruiters, ...) — and most of those expose a public JSON
endpoint with the full posting list. No authentication, no
captchas, no TLS fingerprinting, no anti-bot defenses to evade.
We just hit the URL and parse JSON.

Tradeoffs vs. browser-based discovery:

* **Pros**
   - Zero anti-detect risk. The endpoints are designed for embedding
     in third-party sites; they expect bot traffic.
   - Stable schemas. ATS vendors version their public APIs and
     break them rarely (vs. weekly DOM rewrites on LinkedIn).
   - Full job description in the listing payload — no detail-page
     scrape needed.
   - 10-50× faster than rendering a browser page. A full board
     pull is one HTTP call.

* **Cons**
   - User must know which companies use which ATS. We can't
     auto-discover; companies don't advertise this.
   - No location filtering on most endpoints — we filter client-side.
   - Apply path is still browser-mediated (the actual submit form
     lives behind the same ATS). For now, ATS adapters are
     ``discovery_only=True``: the engine surfaces matches in the
     "almost" / manual-apply queue and the user clicks through.

Subclassing
-----------

Each ATS implementation derives from :class:`ATSAPIPlatform` and
provides:

* ``source_id`` — registry key, e.g. ``"ats_greenhouse"``.
* ``display_name`` — pretty name for logs / dashboard.
* ``ats_id`` — short slug used to look up the company list in
  user_config (``ats_api_companies.<ats_id>``).
* :meth:`fetch_company_jobs` — async function that hits the ATS
  endpoint for one company slug and returns a list of normalized
  :class:`~auto_applier.storage.models.Job` objects.

The base class wires everything else: configuration loading,
search_jobs orchestration, optional keyword/location filtering,
empty-results handling, and the discovery-only ApplyResult.

User configuration
------------------

In ``data/user_config.json``::

    {
      "enabled_platforms": ["ats_greenhouse", "ats_lever"],
      "ats_api_companies": {
        "greenhouse": ["stripe", "airbnb", "github"],
        "lever":      ["netflix", "shopify"],
        "ashby":      ["openai", "ramp"]
      }
    }

Each list entry is the company's *slug* on that ATS — usually the
last URL segment when you visit the company's careers page hosted
by the ATS (e.g. ``boards.greenhouse.io/stripe`` -> ``stripe``).

Failure handling
----------------

A 404 / network error on one company is logged at WARNING and the
adapter moves on to the next company. We never let one bad slug
kill the whole search. Empty company lists log INFO and return
``[]`` so users who haven't configured an ATS yet just get a no-op
rather than a confusing crash.
"""
from __future__ import annotations

import asyncio
import logging
from abc import abstractmethod
from typing import Any

import httpx

from auto_applier.browser.base_platform import JobPlatform
from auto_applier.storage.models import ApplyResult, Job

logger = logging.getLogger(__name__)


# Single shared HTTP client per adapter instance. Connection pooling
# matters: a board pull commonly does 5-30 GETs in sequence (one per
# configured company), and reusing the TLS handshake matters for
# large lists. Timeout is conservative — these endpoints are usually
# fast (<1s) but a stalled CDN edge shouldn't hang us.
DEFAULT_TIMEOUT = 10.0


class ATSAPIPlatform(JobPlatform):
    """Common scaffolding for ATS public-API adapters.

    Subclasses override :meth:`fetch_company_jobs` and the three
    class attributes; everything else is provided.
    """

    # Discovery-only — applying still goes through the company's
    # actual ATS-hosted form (which we don't drive yet). User opens
    # the URL from "cli almost" and submits manually.
    discovery_only: bool = True
    discovery_only_reason: str = (
        "ATS API discovery — open the URL and apply manually "
        "(applying through the ATS API itself isn't supported)."
    )

    # Subclasses set this to the slug used in user_config's
    # ``ats_api_companies`` dict. Kept separate from ``source_id``
    # because source_id is namespaced ("ats_greenhouse") while the
    # config key is the bare ATS name ("greenhouse").
    ats_id: str = ""

    def __init__(self, context, config: dict, form_filler=None) -> None:
        # Accept ``context`` to satisfy the engine's wiring contract,
        # but ATS adapters never use a browser. We never instantiate
        # a Page — the base class's ``get_page`` would fail if called,
        # but search_jobs / get_job_description override every code
        # path that needs one.
        super().__init__(context, config, form_filler)
        self._http: httpx.AsyncClient | None = None

    # ------------------------------------------------------------------
    # JobPlatform contract
    # ------------------------------------------------------------------

    async def ensure_logged_in(self) -> bool:
        """No login needed. Public endpoints don't auth."""
        return True

    async def search_jobs(self, keyword: str, location: str) -> list[Job]:
        """Pull jobs from every configured company on this ATS.

        ``keyword`` is applied client-side as a **word-level OR**
        filter against the job title — any title word ≥3 chars from
        the keyword that appears in the job title is enough to match.
        This is dramatically more permissive than the previous
        substring-AND filter, which dropped 1500+ raw Greenhouse jobs
        down to 0 because "data analyst" had to appear as a literal
        substring in title-or-description (rejected: "Senior Data
        Engineer", "Marketing Analyst", "Software Engineer in Data",
        etc.).

        ``location`` is **NOT filtered** by the adapter — ATS jobs
        often phrase location inconsistently ("Remote", "100% remote",
        "Distributed", "Anywhere in the US", "Hybrid - SF") and the
        downstream multi-axis scorer evaluates location-fit per axis
        anyway. Better to let the scorer handle it and surface
        borderline matches in ``cli almost``.

        Cap: each company contributes at most ``max_jobs_per_company``
        (default 30) and the overall batch is capped at
        ``max_jobs_per_search`` (default 200) to bound the LLM-cost
        per cycle. Continuous-run mode chews through the rest over
        subsequent cycles via the dedup layer.

        Empty keyword → no kw filter; all jobs from configured boards
        are returned.
        """
        companies = self._configured_companies()
        if not companies:
            logger.info(
                "%s: no companies configured. Add slugs to "
                "user_config.json -> ats_api_companies.%s "
                "(e.g. 'stripe', 'github').",
                self.display_name, self.ats_id,
            )
            return []

        # Per-cycle caps. Defaulted so a board the size of Stripe's
        # (~500 jobs) can't single-handedly swamp the LLM scorer.
        # Override per-platform via config, e.g.::
        #
        #   "ats_greenhouse": {"max_jobs_per_company": 50, "max_jobs_per_search": 300}
        plat_cfg = self.config.get(self.source_id, {}) or {}
        max_per_company = int(plat_cfg.get("max_jobs_per_company", 30))
        max_total = int(plat_cfg.get("max_jobs_per_search", 200))

        client = await self._get_http()
        all_jobs: list[Job] = []
        kw_words = self._extract_keyword_words(keyword)

        for slug in companies:
            try:
                jobs = await self.fetch_company_jobs(client, slug)
            except httpx.HTTPStatusError as exc:
                # 404 = wrong slug, 403 = ATS pulled the public board.
                # Both are user-recoverable: fix or remove the entry
                # in user_config.
                logger.warning(
                    "%s: %s returned %s for company '%s'. Slug may "
                    "be wrong, or this company has disabled public API.",
                    self.display_name, type(exc).__name__,
                    exc.response.status_code if exc.response else "?",
                    slug,
                )
                continue
            except (httpx.RequestError, asyncio.TimeoutError) as exc:
                logger.warning(
                    "%s: network error fetching '%s': %s",
                    self.display_name, slug, exc,
                )
                continue
            except Exception as exc:
                # Schema drift on the ATS side. Log loudly so the
                # user / maintainer notices but keep going on the
                # other companies.
                logger.warning(
                    "%s: unexpected error parsing '%s': %s",
                    self.display_name, slug, exc, exc_info=True,
                )
                continue

            # Stamp the source on each job so dedup + reporting can
            # tell ATS jobs apart from per-platform browser jobs.
            for j in jobs:
                if not j.source:
                    j.source = self.source_id
                if keyword and not j.search_keyword:
                    j.search_keyword = keyword

            # Word-level OR keyword filter. Any title word ≥3 chars
            # that matches a keyword word counts. Empty kw_words
            # passes everything through.
            if kw_words:
                pre_count = len(jobs)
                jobs = [
                    j for j in jobs
                    if self._title_matches_any(j, kw_words)
                ]
                logger.debug(
                    "%s: %s kw-filtered %d/%d (any word from %r in title)",
                    self.display_name, slug, len(jobs), pre_count,
                    keyword,
                )

            # Per-company cap. Take the FIRST N — ATS endpoints sort
            # newest-first by default, so this lets continuous-run
            # mode work through the backlog over time without us
            # having to write a sorter here.
            if max_per_company > 0:
                jobs = jobs[:max_per_company]

            all_jobs.extend(jobs)
            if max_total > 0 and len(all_jobs) >= max_total:
                # Stop fetching more companies once total cap hit;
                # the rest fall to next cycle's dedup churn.
                logger.info(
                    "%s: reached max_jobs_per_search=%d after %d "
                    "company board(s); deferring remaining boards "
                    "to next cycle",
                    self.display_name, max_total,
                    companies.index(slug) + 1,
                )
                break

        logger.info(
            "%s: fetched %d job(s) across %d configured company board(s)",
            self.display_name, len(all_jobs), len(companies),
        )
        return all_jobs

    async def get_job_description(self, job: Job) -> str:
        """Return the description we already have from the search.

        ATS endpoints return full descriptions inline, so we never
        need a second fetch. ``search_jobs`` populates ``job.description``;
        this method is the contract-compliant pass-through.
        """
        return job.description or ""

    async def apply_to_job(
        self, job: Job, resume_path: str, dry_run: bool = False
    ) -> ApplyResult:
        """ATS adapters don't drive the apply form. Return a clear
        failure with a manual-apply reason; the engine's discovery-
        only path (set by ``discovery_only=True``) actually short-
        circuits this code path before it's reached, but we
        implement the method for completeness and as a defensive
        fallback.
        """
        # ApplyResult has no ``dry_run`` field; the discovery-only
        # reason is the load-bearing signal. Set
        # ``requires_manual_apply`` so the engine routes this to the
        # "skipped → manual apply" bucket if it's ever reached.
        return ApplyResult(
            success=False,
            failure_reason=self.discovery_only_reason,
            requires_manual_apply=True,
        )

    # ------------------------------------------------------------------
    # Subclass contract
    # ------------------------------------------------------------------

    @abstractmethod
    async def fetch_company_jobs(
        self, client: httpx.AsyncClient, company_slug: str,
    ) -> list[Job]:
        """Fetch jobs for one company from this ATS.

        Implementations should:
          - Hit the ATS public endpoint with ``client.get``
          - Call ``response.raise_for_status()``
          - Parse the JSON into :class:`Job` objects
          - Populate ``job_id`` (use the ATS's posting id), ``title``,
            ``company``, ``url``, ``description``. Other fields are
            optional.

        Network and JSON errors propagate up to ``search_jobs``,
        which catches them per-company and continues.
        """
        ...

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _configured_companies(self) -> list[str]:
        """Read the company-slug list for this ATS from user_config.

        Tolerates a missing key, a top-level dict (modern shape), or
        a list-of-dicts shape some users configure by hand. Always
        returns a deduplicated, stripped, non-empty slug list.
        """
        raw = self.config.get("ats_api_companies", {}) or {}
        if isinstance(raw, list):
            # Legacy shape: [{"ats": "greenhouse", "company": "stripe"}, ...]
            slugs = [
                str(entry.get("company", "")).strip()
                for entry in raw
                if isinstance(entry, dict)
                and entry.get("ats", "").lower() == self.ats_id.lower()
            ]
        elif isinstance(raw, dict):
            entries = raw.get(self.ats_id, []) or []
            if not isinstance(entries, list):
                return []
            slugs = [str(s).strip() for s in entries]
        else:
            return []

        # Dedup while preserving order — stripped, lowercased keys.
        seen: set[str] = set()
        unique: list[str] = []
        for s in slugs:
            if not s:
                continue
            key = s.lower()
            if key in seen:
                continue
            seen.add(key)
            unique.append(s)
        return unique

    @staticmethod
    def _extract_keyword_words(keyword: str) -> list[str]:
        """Tokenize ``keyword`` into matchable words.

        Returns lowercase word-tokens of length ≥3. Drops short
        words ("a", "of", "in") that would otherwise dominate the
        OR-filter and waste an LLM scoring call on every job whose
        description contains "in". Empty keyword → empty list.
        """
        if not keyword:
            return []
        import re as _re
        return [
            w for w in _re.findall(r"[a-z0-9]+", keyword.lower())
            if len(w) >= 3
        ]

    @staticmethod
    def _title_matches_any(job: Job, kw_words: list[str]) -> bool:
        """True when the JOB TITLE contains any keyword word.

        Title-only (not description) because:
          1. Descriptions are long; almost any 3-letter word will
             match somewhere → effectively no filter.
          2. The whole point of the kw filter is "is this the kind
             of role I want?", which the title carries.
          3. The downstream multi-axis scorer reads the description
             anyway.
        """
        if not kw_words:
            return True
        title_lower = (job.title or "").lower()
        return any(w in title_lower for w in kw_words)

    async def _get_http(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(
                timeout=DEFAULT_TIMEOUT,
                # User-Agent helps a few ATSes (Lever in particular)
                # — they 403 default httpx UA on some boards.
                headers={
                    "User-Agent": (
                        "AutoApplier/2 (+https://github.com/) "
                        "discovery-only board reader"
                    ),
                    "Accept": "application/json",
                },
                follow_redirects=True,
            )
        return self._http

    async def aclose(self) -> None:
        """Close the HTTP client. Called by tests; the engine doesn't
        currently teardown adapters but this keeps the resource
        hygiene story clean.
        """
        if self._http is not None:
            await self._http.aclose()
            self._http = None
