"""Lever public board API adapter.

Lever exposes a documented JSON endpoint at:

    https://api.lever.co/v0/postings/{site}?mode=json

The ``site`` is the company's Lever slug — the second segment of
the URL when you visit ``jobs.lever.co/<slug>``. ``mode=json`` is
critical; without it Lever returns HTML.

Schema (relevant fields):

    [
      {
        "id":   "abcd-1234-…",            # Lever posting UUID
        "text": "Senior Backend Engineer",
        "hostedUrl":  "https://jobs.lever.co/stripe/abcd-1234",
        "applyUrl":   "https://jobs.lever.co/stripe/abcd-1234/apply",
        "categories": {
          "team":     "Engineering",
          "location": "San Francisco",
          "commitment": "Full-time"
        },
        "description":     "<div>About Stripe…</div>",
        "descriptionPlain":"About Stripe…",
        "lists": [{"text": "Requirements", "content": "<ul>…</ul>"}],
        "additional":      "<div>Equal opportunity employer…</div>",
        "additionalPlain": "Equal opportunity employer…",
        ...
      }
    ]

We prefer the ``*Plain`` variants where present (Lever ships them
specifically for programmatic consumers) and fall back to stripping
HTML from the rich variants. The ``lists`` array contains the
"Requirements" / "What you'll do" / "Nice to have" sections — we
flatten them into the description so the scorer sees them.
"""
from __future__ import annotations

import html
import logging
import re
from typing import Any

import httpx

from auto_applier.browser.platforms.ats_api_base import ATSAPIPlatform
from auto_applier.storage.models import Job

logger = logging.getLogger(__name__)


LEVER_API_BASE = "https://api.lever.co/v0/postings"

_TAG_RE = re.compile(r"<[^>]+>")


def _strip_html(text: str) -> str:
    """Same logic as the Greenhouse stripper, kept module-local so
    each ATS adapter can evolve its own quirks if needed.

    Order matters — ``html.unescape`` runs FIRST so escaped tags
    (``&lt;h2&gt;``) become real tags before the strip step. See
    ats_greenhouse._strip_html for the regression context.
    """
    if not text:
        return ""
    s = html.unescape(text)
    s = re.sub(r"</p>", "\n\n", s, flags=re.IGNORECASE)
    s = re.sub(r"<br\s*/?>", "\n", s, flags=re.IGNORECASE)
    s = re.sub(r"<li[^>]*>", "\n• ", s, flags=re.IGNORECASE)
    s = re.sub(r"</h[1-6]>", "\n", s, flags=re.IGNORECASE)
    s = re.sub(r"<h[1-6][^>]*>", "\n", s, flags=re.IGNORECASE)
    s = _TAG_RE.sub("", s)
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r"\n\s*\n\s*", "\n\n", s)
    return s.strip()


class ATSLeverPlatform(ATSAPIPlatform):
    source_id = "ats_lever"
    display_name = "Lever (ATS API)"
    ats_id = "lever"

    async def fetch_company_jobs(
        self, client: httpx.AsyncClient, company_slug: str,
    ) -> list[Job]:
        url = f"{LEVER_API_BASE}/{company_slug}?mode=json"
        logger.debug("Lever: GET %s", url)
        resp = await client.get(url)
        resp.raise_for_status()
        data = resp.json()
        # Lever returns a top-level list, not a wrapped object.
        raw_jobs = data if isinstance(data, list) else []
        logger.info(
            "Lever: %s -> %d posting(s)", company_slug, len(raw_jobs),
        )

        jobs: list[Job] = []
        for entry in raw_jobs:
            try:
                job = self._parse(entry, company_slug)
            except Exception as exc:
                logger.debug(
                    "Lever: skipping malformed entry from %s: %s",
                    company_slug, exc,
                )
                continue
            if job is not None:
                jobs.append(job)
        return jobs

    def _parse(self, entry: dict[str, Any], company_slug: str) -> Job | None:
        posting_id = entry.get("id")
        title = (entry.get("text") or "").strip()
        if not posting_id or not title:
            return None

        # ``hostedUrl`` is the public-facing posting page; ``applyUrl``
        # appends ``/apply`` to it. Hosted URL is what the user wants
        # to click through to from "cli almost".
        url = (entry.get("hostedUrl") or entry.get("applyUrl") or "").strip()
        company = _humanize_slug(company_slug)

        # Categories carry location + team + commitment.
        categories = entry.get("categories") or {}
        location_str = ""
        if isinstance(categories, dict):
            location_str = str(categories.get("location") or "").strip()

        # Build description, preferring plain-text variants.
        body_parts: list[str] = []
        if location_str:
            body_parts.append(f"Location: {location_str}")
        team = (categories.get("team") if isinstance(categories, dict) else "") or ""
        if team:
            body_parts.append(f"Team: {team}")
        commitment = (
            categories.get("commitment") if isinstance(categories, dict) else ""
        ) or ""
        if commitment:
            body_parts.append(f"Type: {commitment}")

        # Main body — descriptionPlain if present, else stripped HTML.
        main = (
            entry.get("descriptionPlain")
            or _strip_html(entry.get("description") or "")
        )
        if main:
            body_parts.append("")
            body_parts.append(main)

        # Lever's "lists" field is the Responsibilities / Requirements
        # bullet sections. Each entry has its own text + content.
        lists = entry.get("lists") or []
        if isinstance(lists, list):
            for sec in lists:
                if not isinstance(sec, dict):
                    continue
                heading = str(sec.get("text") or "").strip()
                content = _strip_html(sec.get("content") or "")
                if not (heading or content):
                    continue
                if heading:
                    body_parts.append("")
                    body_parts.append(heading)
                if content:
                    body_parts.append(content)

        # Closing / additional section (EEO statement, etc.) — usually
        # not load-bearing for scoring but include it for completeness.
        additional = (
            entry.get("additionalPlain")
            or _strip_html(entry.get("additional") or "")
        )
        if additional:
            body_parts.append("")
            body_parts.append(additional)

        description = "\n".join(body_parts).strip()

        return Job(
            job_id=f"lever_{company_slug}_{posting_id}",
            title=title,
            company=company,
            url=url,
            description=description,
            source=self.source_id,
        )


def _humanize_slug(slug: str) -> str:
    return " ".join(part.capitalize() for part in slug.replace("_", "-").split("-"))
