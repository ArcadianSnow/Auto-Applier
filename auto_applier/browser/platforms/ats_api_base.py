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
    # Phase 2.2 — Browser-driven quick-apply (DOM prefill, halt before
    # Submit). Off by default; engine opt-in via auto_quick_apply_ats.
    # ------------------------------------------------------------------

    async def quick_apply(
        self,
        job: Job,
        resume_path: str,
        cover_letter_text: str,
        personal_info: dict,
        page,
    ) -> ApplyResult:
        """Open the apply URL in ``page``, run form_filler to populate
        every field we can, upload the resume, paste the cover letter,
        and HALT before clicking Submit.

        Returns ``ApplyResult`` with:
          - ``success=True`` and ``requires_manual_apply=True`` on a
            successful prefill (user must review + click Submit).
          - ``success=False, requires_manual_apply=True`` on prefill
            failure (page didn't render, no form found, etc.).

        The actual submit is the USER's responsibility — that's the
        whole legal/ethical defense of the quick-apply pattern. Per
        Phase 2 research: ATS apply endpoints have anti-abuse
        layers (Greenhouse invisible reCAPTCHA, Lever hCaptcha,
        Ashby private POST), but the apply FORM rendered in a real
        browser session can be filled and reviewed without those
        engaging.

        Subclasses can override ``_resolve_apply_url`` if their
        listing URL needs derivation (e.g. Ashby uses ``applyUrl``
        rather than ``jobUrl``). Default: trust ``job.url``.
        """
        from auto_applier.browser.form_filler import FormFiller

        apply_url = self._resolve_apply_url(job)
        if not apply_url:
            return ApplyResult(
                success=False,
                failure_reason=(
                    "No apply URL available for this ATS job — open "
                    "the listing in your browser manually."
                ),
                requires_manual_apply=True,
            )

        try:
            await page.goto(apply_url, wait_until="domcontentloaded", timeout=20000)
        except Exception as exc:
            logger.warning(
                "%s quick-apply: navigation to %s failed: %s",
                self.display_name, apply_url, exc,
            )
            return ApplyResult(
                success=False,
                failure_reason=(
                    f"Couldn't open the apply page ({exc}). Open the "
                    "URL manually."
                ),
                requires_manual_apply=True,
            )

        # Give React-rendered forms a moment to hydrate. Greenhouse
        # and Ashby are SPAs; Lever is server-rendered but still
        # benefits from the brief settle.
        try:
            await page.wait_for_timeout(2500)
        except Exception:
            pass

        # Run form_filler against the apply page. This handles
        # personal-info → answers.json → LLM cascade per field. The
        # platform adapters all use the same {find_form_fields →
        # fill_field per field} pattern, mirrored here.
        from auto_applier.browser.selector_utils import find_form_fields

        # Resolve a router. The base ATSAPIPlatform doesn't carry a
        # FormFiller, but the engine does pass one in via the
        # form_filler argument when we're called from the engine's
        # apply path. Caller must wire this; we surface a clear
        # error if it's missing.
        if self.form_filler is None or getattr(self.form_filler, "router", None) is None:
            return ApplyResult(
                success=False,
                failure_reason=(
                    "Quick-apply needs an LLM router but the platform "
                    "adapter wasn't given one. This is a wiring bug — "
                    "the engine must pass form_filler= to the adapter "
                    "for quick-apply to work."
                ),
                requires_manual_apply=True,
            )

        from auto_applier.browser.form_filler import FormFiller

        try:
            filler = FormFiller(
                router=self.form_filler.router,
                personal_info=personal_info or {},
                resume_text="",
                job_description=job.description or "",
                company_name=job.company,
                job_title=job.title,
                resume_label=getattr(self.form_filler, "resume_label", ""),
                platform_display_name=self.display_name,
            )
        except Exception as exc:
            logger.warning(
                "%s quick-apply: FormFiller init failed: %s",
                self.display_name, exc,
            )
            return ApplyResult(
                success=False,
                failure_reason=f"Couldn't start form filler: {exc}",
                requires_manual_apply=True,
            )

        # Resume upload first — the form's resume field is usually
        # at the top, and several ATSes cascade subsequent fields
        # only after a resume is attached. ``pick_resume_input``
        # finds the right slot via classification.
        try:
            slot = await FormFiller.pick_resume_input(page, self.display_name)
            if slot is not None and resume_path:
                await slot.set_input_files(resume_path)
                await FormFiller.wait_for_upload_complete(
                    page, expected_name=resume_path, timeout=15.0,
                )
                # Some ATSes parse the resume and auto-populate fields.
                # Give that a beat so we don't overwrite their work.
                await page.wait_for_timeout(2000)
        except Exception as exc:
            logger.debug(
                "%s quick-apply: resume upload skipped (%s)",
                self.display_name, exc,
            )

        # Prefill the rest — discover fields, then fill each. Any
        # single-field error is logged at WARNING by fill_field
        # itself; we keep going.
        try:
            fields = await find_form_fields(page)
        except Exception as exc:
            logger.warning(
                "%s quick-apply: find_form_fields raised: %s",
                self.display_name, exc,
            )
            fields = []

        for field in fields:
            try:
                await filler.fill_field(page, field)
            except Exception as exc:
                logger.debug(
                    "%s quick-apply: fill_field raised on %r: %s",
                    self.display_name, getattr(field, "label", "?"), exc,
                )

        # Paste cover letter into a textarea if one is visible. The
        # form_filler also has a cover-letter handler that fires on
        # recognized fields; we treat THIS path as the fallback for
        # the case where cover-letter text was pre-generated upstream
        # but the form didn't expose a recognized cover-letter field.
        # Best-effort — never fails the prefill.
        if cover_letter_text:
            try:
                await self._paste_cover_letter_if_textarea(page, cover_letter_text)
            except Exception as exc:
                logger.debug(
                    "%s quick-apply: cover-letter paste skipped: %s",
                    self.display_name, exc,
                )

        logger.info(
            "%s quick-apply: prefilled %d/%d fields for %s @ %s — "
            "HALTING before Submit; user must review and click "
            "Submit themselves",
            self.display_name,
            filler.fields_filled, filler.fields_total,
            job.title[:50], job.company[:40],
        )
        return ApplyResult(
            success=True,
            failure_reason=(
                "Form prefilled — review and click Submit yourself in "
                "the browser window. Quick-apply never auto-submits."
            ),
            requires_manual_apply=True,
            fields_filled=filler.fields_filled,
            fields_total=filler.fields_total,
            cover_letter_generated=bool(cover_letter_text),
            used_llm=filler.used_llm,
            gaps=list(filler.gaps),
        )

    def _resolve_apply_url(self, job: Job) -> str:
        """Derive the apply-page URL from a Job.

        Default: trust ``job.url`` — Lever's ``hostedUrl`` is already
        the apply-form gateway. Greenhouse / Ashby override this so
        ``boards.greenhouse.io/<slug>/jobs/<id>`` becomes the
        apply-mode URL.
        """
        return job.url or ""

    async def _paste_cover_letter_if_textarea(self, page, text: str) -> None:
        """Find a cover-letter textarea on the apply page and fill
        it with ``text``. Best-effort — silently does nothing if no
        recognizable cover-letter field is visible.

        We deliberately avoid an aggressive "fill any large textarea"
        strategy because some boards have a "Why us?" custom textarea
        that should NOT receive a generic cover letter — that field
        gets answered by the form_filler's LLM cascade.
        """
        candidates = (
            "textarea[name*='cover' i]",
            "textarea[id*='cover' i]",
            "textarea[aria-label*='cover letter' i]",
            "textarea[placeholder*='cover letter' i]",
        )
        for sel in candidates:
            try:
                el = await page.query_selector(sel)
                if not el:
                    continue
                if not await el.is_visible():
                    continue
                # Only fill if the field is empty — don't clobber
                # something the form-filler already wrote.
                current = await el.input_value()
                if current and current.strip():
                    return
                await el.fill(text)
                return
            except Exception:
                continue

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
