"""Lever discovery via the public Postings API (spec §6a, research/ats-discovery-seeding.md).

``https://api.lever.co/v0/postings/{site}?mode=json`` — unauthenticated, public, and the
listing already includes the full plain-text JD (``descriptionPlain``) and the direct
``applyUrl``, so discovery + describe are one call. Apply form = ``{hostedUrl}/apply``
(server-rendered, real ``<form>``; Lever ships invisible hCaptcha — lighter than GH
Enterprise per the research).
"""

from __future__ import annotations

import time
from dataclasses import dataclass

import httpx

_API = "https://api.lever.co/v0/postings"
_UA = "auto-applier-v3/3.0 (personal job-search tool; contact via repo)"
#: Lever apply form: standard fields are name-keyed; this is the form-present tell.
FORM_SELECTOR = "input[name='name']"


@dataclass
class LeverListing:
    source_job_id: str
    title: str
    company: str
    location: str
    url: str          # hostedUrl
    apply_url: str    # {hostedUrl}/apply
    description: str = ""
    posted_at: str = ""
    source = "lever"


class LeverSource:
    source_name = "lever"
    form_selector = FORM_SELECTOR
    spa = False  # server-rendered

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

    def discover(self, site: str) -> list[LeverListing]:
        resp = self._get(f"{_API}/{site}?mode=json")
        if resp.status_code != 200:
            return []
        try:
            postings = resp.json()
        except ValueError:
            return []
        out: list[LeverListing] = []
        for p in postings:
            pid = str(p.get("id", ""))
            if not pid:
                continue
            hosted = p.get("hostedUrl", "") or f"https://jobs.lever.co/{site}/{pid}"
            out.append(
                LeverListing(
                    source_job_id=pid,
                    title=p.get("text", "").strip(),
                    company=site,
                    location=(p.get("categories") or {}).get("location", ""),
                    url=hosted,
                    apply_url=p.get("applyUrl") or f"{hosted}/apply",
                    description=p.get("descriptionPlain", "") or p.get("description", ""),
                    posted_at=str(p.get("createdAt", "")),
                )
            )
        return out

    def close(self) -> None:
        if self._owns:
            self._client.close()


def confirm_probe(site: str, client: httpx.Client | None = None) -> tuple[bool, int]:
    owns = client is None
    client = client or httpx.Client(headers={"User-Agent": _UA}, timeout=10.0)
    try:
        r = client.get(f"{_API}/{site}?mode=json")
        if r.status_code != 200:
            return (False, 0)
        n = len(r.json())
        return (n > 0, n)
    except (httpx.HTTPError, ValueError):
        return (False, 0)
    finally:
        if owns:
            client.close()
