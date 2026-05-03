"""Ashby public board API adapter.

Ashby exposes a documented JSON endpoint at:

    https://api.ashbyhq.com/posting-api/job-board/{org-slug}

The slug is the company's Ashby identifier — visible at
``jobs.ashbyhq.com/<slug>``. By default, the response includes
basic metadata only; passing ``?includeCompensation=true`` adds
salary ranges where the org has chosen to publish them.

Schema (relevant fields):

    {
      "jobs": [
        {
          "id": "abcd-1234-…",
          "title": "Senior Backend Engineer",
          "department": "Engineering",
          "team": "Platform",
          "location": "San Francisco, CA",
          "employmentType": "FullTime",
          "jobUrl": "https://jobs.ashbyhq.com/openai/abcd-1234",
          "publishedDate": "2026-04-30",
          "descriptionPlain": "About OpenAI…",
          "descriptionHtml":  "<p>About OpenAI…</p>",
          "compensation": {                       # optional
            "compensationTierSummary": "$200k - $250k",
            ...
          },
          ...
        }
      ]
    }

Ashby ships ``descriptionPlain`` natively which is ideal for our
LLM scorer. We fall back to stripping the HTML version when it's
absent (rare).
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


ASHBY_API_BASE = "https://api.ashbyhq.com/posting-api/job-board"

_TAG_RE = re.compile(r"<[^>]+>")


def _strip_html(text: str) -> str:
    """Strip tags and decode entities. Order matters — unescape
    FIRST so escaped tags become real tags before strip.
    See ats_greenhouse._strip_html for context.
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


class ATSAshbyPlatform(ATSAPIPlatform):
    source_id = "ats_ashby"
    display_name = "Ashby (ATS API)"
    ats_id = "ashby"

    async def fetch_company_jobs(
        self, client: httpx.AsyncClient, company_slug: str,
    ) -> list[Job]:
        url = (
            f"{ASHBY_API_BASE}/{company_slug}"
            f"?includeCompensation=true"
        )
        logger.debug("Ashby: GET %s", url)
        resp = await client.get(url)
        resp.raise_for_status()
        data = resp.json()
        raw_jobs = data.get("jobs", []) if isinstance(data, dict) else []
        logger.info(
            "Ashby: %s -> %d job(s)", company_slug, len(raw_jobs),
        )

        jobs: list[Job] = []
        for entry in raw_jobs:
            try:
                job = self._parse(entry, company_slug)
            except Exception as exc:
                logger.debug(
                    "Ashby: skipping malformed entry from %s: %s",
                    company_slug, exc,
                )
                continue
            if job is not None:
                jobs.append(job)
        return jobs

    def _parse(self, entry: dict[str, Any], company_slug: str) -> Job | None:
        ashby_id = entry.get("id")
        title = (entry.get("title") or "").strip()
        if not ashby_id or not title:
            return None

        url = (entry.get("jobUrl") or "").strip()
        company = _humanize_slug(company_slug)

        body_parts: list[str] = []
        location = (entry.get("location") or "").strip()
        if location:
            body_parts.append(f"Location: {location}")
        department = (entry.get("department") or "").strip()
        if department:
            body_parts.append(f"Department: {department}")
        team = (entry.get("team") or "").strip()
        if team:
            body_parts.append(f"Team: {team}")
        employment = (entry.get("employmentType") or "").strip()
        if employment:
            body_parts.append(f"Type: {employment}")

        # Compensation summary — we surface it so the salary axis of
        # the multi-dimensional scorer has signal. Most Ashby orgs
        # publish ranges; a missing compensation block is fine, just
        # means scoring will rate that axis on what it can infer.
        comp = entry.get("compensation")
        if isinstance(comp, dict):
            summary = (
                comp.get("compensationTierSummary")
                or comp.get("summary")
                or ""
            ).strip()
            if summary:
                body_parts.append(f"Compensation: {summary}")

        body = (
            entry.get("descriptionPlain")
            or _strip_html(entry.get("descriptionHtml") or "")
        )
        if body:
            body_parts.append("")
            body_parts.append(body)

        description = "\n".join(body_parts).strip()

        return Job(
            job_id=f"ashby_{company_slug}_{ashby_id}",
            title=title,
            company=company,
            url=url,
            description=description,
            source=self.source_id,
        )


def _humanize_slug(slug: str) -> str:
    return " ".join(part.capitalize() for part in slug.replace("_", "-").split("-"))
