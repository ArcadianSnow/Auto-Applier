"""Ashby discovery via the public posting API (spec §6a, research/ats-discovery-seeding.md).

``https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true`` —
unauthenticated, public; the listing includes ``descriptionPlain`` and a direct
``applyUrl`` (= ``{jobUrl}/application``). The apply form is a **React SPA with no
``<form>``** and an XHR submit (the trickiest of the three to drive/confirm); Ashby ships
invisible reCAPTCHA. Slugs are often case-sensitive — preserve casing.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

import httpx

_API = "https://api.ashbyhq.com/posting-api/job-board"
_UA = "auto-applier-v3/3.0 (personal job-search tool; contact via repo)"
#: Ashby standard fields use stable _systemfield_* ids — the form-present tell.
FORM_SELECTOR = "#_systemfield_name"


@dataclass
class AshbyListing:
    source_job_id: str
    title: str
    company: str
    location: str
    url: str          # jobUrl
    apply_url: str    # {jobUrl}/application
    description: str = ""
    posted_at: str = ""
    source = "ashby"


class AshbySource:
    source_name = "ashby"
    form_selector = FORM_SELECTOR
    spa = True  # React SPA — the form renders client-side; survey must wait for it

    def __init__(self, client: httpx.Client | None = None, min_interval_s: float = 1.0):
        self._client = client or httpx.Client(headers={"User-Agent": _UA}, timeout=10.0)
        self._owns = client is None
        self._min = min_interval_s
        self._last = 0.0

    def _get(self, url: str) -> httpx.Response:
        elapsed = time.monotonic() - self._last
        if elapsed < self._min:
            time.sleep(self._min - elapsed)
        self._last = time.monotonic()
        return self._client.get(url)

    def discover(self, slug: str) -> list[AshbyListing]:
        resp = self._get(f"{_API}/{slug}?includeCompensation=true")
        if resp.status_code != 200:
            return []
        try:
            jobs = resp.json().get("jobs", [])
        except (ValueError, AttributeError):
            return []
        out: list[AshbyListing] = []
        for j in jobs:
            if j.get("isListed") is False:
                continue
            jid = str(j.get("id", ""))
            if not jid:
                continue
            job_url = j.get("jobUrl", "") or f"https://jobs.ashbyhq.com/{slug}/{jid}"
            out.append(
                AshbyListing(
                    source_job_id=jid,
                    title=j.get("title", "").strip(),
                    company=slug,
                    location=j.get("location", "") or "",
                    url=job_url,
                    apply_url=j.get("applyUrl") or f"{job_url}/application",
                    description=j.get("descriptionPlain", ""),
                    posted_at=j.get("publishedAt", "") or "",
                )
            )
        return out

    def close(self) -> None:
        if self._owns:
            self._client.close()


def confirm_probe(slug: str, client: httpx.Client | None = None) -> tuple[bool, int]:
    owns = client is None
    client = client or httpx.Client(headers={"User-Agent": _UA}, timeout=10.0)
    try:
        r = client.get(f"{_API}/{slug}?includeCompensation=true")
        if r.status_code != 200:
            return (False, 0)
        jobs = r.json().get("jobs", [])
        return (len(jobs) > 0, len(jobs))
    except (httpx.HTTPError, ValueError, AttributeError):
        return (False, 0)
    finally:
        if owns:
            client.close()
