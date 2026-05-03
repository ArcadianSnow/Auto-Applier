"""Greenhouse public board API adapter.

Greenhouse exposes a documented JSON endpoint for every public
board. Per the Greenhouse Job Board API docs:

    https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true

The ``content=true`` flag asks for the full posting body inline
(otherwise we'd get just a stub and have to fetch each posting
individually). The ``token`` is the company's slug — the last
segment of the URL when you visit the company's careers page on
boards.greenhouse.io.

Schema (relevant fields):

    {
      "jobs": [
        {
          "id": 1234567,
          "title": "Senior Backend Engineer",
          "absolute_url": "https://boards.greenhouse.io/stripe/jobs/1234567",
          "location": {"name": "San Francisco, CA"},
          "departments": [{"name": "Engineering", ...}, ...],
          "offices":     [{"name": "San Francisco", ...}, ...],
          "content": "&lt;p&gt;Stripe is …&lt;/p&gt;",   # HTML-escaped
          "updated_at": "2026-04-30T12:00:00-04:00",
          "company_name": "Stripe",  # not always present
          ...
        }
      ],
      "meta": {"total": 42}
    }

The ``content`` is HTML — we decode entities and strip tags so the
LLM scorer sees clean text rather than escaped markup.

API discovery: companies don't always publish their slug. The
practical method is to find a public job posting on the company's
career page hosted under boards.greenhouse.io/&lt;slug&gt;, then take
&lt;slug&gt;.
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


GREENHOUSE_API_BASE = "https://boards-api.greenhouse.io/v1/boards"

# HTML tag stripper — Greenhouse content is always sanitized HTML
# (paragraphs, lists, links). We don't need a full parser; a simple
# regex-and-decode is fine for our purposes (LLM-readable text).
_TAG_RE = re.compile(r"<[^>]+>")


def _strip_html(text: str) -> str:
    """Strip tags and decode entities. Returns a single string with
    paragraph breaks preserved as double newlines.

    Greenhouse uses ``&lt;p&gt;`` for paragraphs and ``&lt;ul&gt;/&lt;li&gt;`` for
    bullet lists — replacing those with newlines before tag removal
    keeps the structure readable for the scoring LLM.
    """
    if not text:
        return ""
    # Preserve structure: paragraph breaks → double newline,
    # list items → bullet.
    s = re.sub(r"</p>", "\n\n", text, flags=re.IGNORECASE)
    s = re.sub(r"<br\s*/?>", "\n", s, flags=re.IGNORECASE)
    s = re.sub(r"<li[^>]*>", "\n• ", s, flags=re.IGNORECASE)
    s = _TAG_RE.sub("", s)
    s = html.unescape(s)
    # Collapse runs of whitespace but keep paragraph breaks.
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r"\n\s*\n\s*", "\n\n", s)
    return s.strip()


class ATSGreenhousePlatform(ATSAPIPlatform):
    source_id = "ats_greenhouse"
    display_name = "Greenhouse (ATS API)"
    ats_id = "greenhouse"

    async def fetch_company_jobs(
        self, client: httpx.AsyncClient, company_slug: str,
    ) -> list[Job]:
        url = f"{GREENHOUSE_API_BASE}/{company_slug}/jobs?content=true"
        logger.debug("Greenhouse: GET %s", url)
        resp = await client.get(url)
        resp.raise_for_status()
        data = resp.json()
        raw_jobs = data.get("jobs", []) if isinstance(data, dict) else []
        logger.info(
            "Greenhouse: %s -> %d job(s)", company_slug, len(raw_jobs),
        )

        jobs: list[Job] = []
        for entry in raw_jobs:
            try:
                job = self._parse(entry, company_slug)
            except Exception as exc:
                logger.debug(
                    "Greenhouse: skipping malformed entry from %s: %s",
                    company_slug, exc,
                )
                continue
            if job is not None:
                jobs.append(job)
        return jobs

    def _parse(self, entry: dict[str, Any], company_slug: str) -> Job | None:
        """Convert a Greenhouse job dict into our Job model.

        Tolerates field-shape variations between boards (some put
        company name in ``company_name``, some inline it via the URL,
        some omit it entirely).
        """
        gh_id = entry.get("id")
        title = (entry.get("title") or "").strip()
        if not gh_id or not title:
            return None

        url = (entry.get("absolute_url") or "").strip()

        # Company name resolution. Greenhouse's free tier doesn't
        # always include company_name in the payload — fall back to
        # the slug humanized.
        company = (
            entry.get("company_name")
            or _humanize_slug(company_slug)
        )

        # Location: prefer the structured ``location.name`` field.
        loc_obj = entry.get("location") or {}
        location_str = ""
        if isinstance(loc_obj, dict):
            location_str = (loc_obj.get("name") or "").strip()
        # Some boards use "offices" instead.
        if not location_str:
            offices = entry.get("offices") or []
            if isinstance(offices, list) and offices:
                names = [
                    str(o.get("name", "")).strip()
                    for o in offices if isinstance(o, dict)
                ]
                location_str = ", ".join(n for n in names if n)

        description = _strip_html(entry.get("content") or "")
        # Prefix the location line so it's visible to the scorer
        # (location is one of our seven scoring axes).
        if location_str:
            description = f"Location: {location_str}\n\n{description}"

        return Job(
            job_id=f"gh_{company_slug}_{gh_id}",
            title=title,
            company=company,
            url=url,
            description=description,
            source=self.source_id,
        )


def _humanize_slug(slug: str) -> str:
    """``stripe`` → ``Stripe``. ``my-cool-co`` → ``My Cool Co``."""
    return " ".join(part.capitalize() for part in slug.replace("_", "-").split("-"))
