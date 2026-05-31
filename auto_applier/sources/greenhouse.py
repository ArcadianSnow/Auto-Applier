"""Greenhouse discovery via the public Job Board API (spec §6a, research/ats-discovery-seeding.md).

The read API is unauthenticated and public — discovery costs no login, no browser, no
anti-detection. This module covers the *discovery* half of the slice:

  * ``confirm_probe(token)``     — is this board token valid + does it have open jobs?
  * ``GreenhouseSource.discover``— list lightweight job snippets (no full content).
  * ``GreenhouseSource.describe``— fetch the FULL JD for one job (content=true), HTML→text.

Submits do NOT happen here — the API can't submit for us (employer-credential-gated,
spec §6a). The browser apply path is ``sources/browser/greenhouse_apply.py``.

Politeness (research guardrails): single-threaded, descriptive User-Agent, ~1 req/s,
short timeouts, fail-closed. Read endpoints are explicitly public per Greenhouse docs.
"""

from __future__ import annotations

import html
import re
import time
from dataclasses import dataclass, field

import httpx

_API_BASE = "https://boards-api.greenhouse.io/v1/boards"
_USER_AGENT = "auto-applier-v3/3.0 (personal job-search tool; contact via repo)"
_MIN_INTERVAL_S = 1.0  # ~1 req/s per host (politeness)

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"[ \t]*\n[ \t]*")


class GreenhouseError(RuntimeError):
    """Raised on a non-recoverable API response (bad token, malformed payload)."""


@dataclass
class JobListing:
    """A discovered Greenhouse posting (the discovery snippet; full JD via describe())."""

    source_job_id: str          # Greenhouse numeric id as string
    title: str
    company: str                # board display name (or token fallback)
    location: str
    url: str                    # canonical job-boards.greenhouse.io URL (drive THIS, not wrappers)
    board_token: str
    posted_at: str = ""
    description: str = ""        # populated by describe()


def html_to_text(raw: str) -> str:
    """Greenhouse ``content`` is HTML-entity-encoded markup. Unescape → strip tags →
    collapse whitespace, into plain text suitable for full-JD scoring (spec §7 step 4)."""
    if not raw:
        return ""
    unescaped = html.unescape(raw)
    text = _TAG_RE.sub("\n", unescaped)
    text = html.unescape(text)  # double-encoded entities appear in some boards
    text = _WS_RE.sub("\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


class GreenhouseSource:
    """Discovery + describe for one Greenhouse board family. One instance per process;
    holds a polite rate-limited httpx client."""

    source_name = "greenhouse"

    def __init__(self, client: httpx.Client | None = None, min_interval_s: float = _MIN_INTERVAL_S):
        self._client = client or httpx.Client(
            headers={"User-Agent": _USER_AGENT}, timeout=10.0, follow_redirects=True
        )
        self._owns_client = client is None
        self._min_interval = min_interval_s
        self._last_call = 0.0
        self._board_names: dict[str, str] = {}

    # -- politeness ---------------------------------------------------------
    def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last_call
        if elapsed < self._min_interval:
            time.sleep(self._min_interval - elapsed)
        self._last_call = time.monotonic()

    def _get(self, url: str) -> httpx.Response:
        self._throttle()
        return self._client.get(url)

    # -- board metadata -----------------------------------------------------
    def board_name(self, token: str) -> str:
        """Company display name for a board token (cached). Falls back to the token."""
        if token in self._board_names:
            return self._board_names[token]
        name = token
        try:
            resp = self._get(f"{_API_BASE}/{token}")
            if resp.status_code == 200:
                name = resp.json().get("name") or token
        except (httpx.HTTPError, ValueError):
            pass
        self._board_names[token] = name
        return name

    # -- discovery ----------------------------------------------------------
    def discover(self, token: str) -> list[JobListing]:
        """List open postings for ``token`` (lightweight — no content). Raises
        :class:`GreenhouseError` on a bad token / malformed payload."""
        try:
            resp = self._get(f"{_API_BASE}/{token}/jobs")
        except httpx.HTTPError as exc:
            raise GreenhouseError(f"network error listing {token}: {exc}") from exc
        if resp.status_code == 404:
            raise GreenhouseError(f"board token '{token}' not found (404)")
        if resp.status_code != 200:
            raise GreenhouseError(f"unexpected {resp.status_code} listing {token}")
        try:
            payload = resp.json()
        except ValueError as exc:
            raise GreenhouseError(f"non-JSON response for {token}") from exc

        company = self.board_name(token)
        listings: list[JobListing] = []
        for job in payload.get("jobs", []):
            jid = str(job.get("id", ""))
            if not jid:
                continue
            loc = (job.get("location") or {}).get("name", "") if isinstance(
                job.get("location"), dict
            ) else ""
            listings.append(
                JobListing(
                    source_job_id=jid,
                    title=job.get("title", "").strip(),
                    company=company,
                    location=loc,
                    # canonical hosted URL — we always drive this, never a company wrapper
                    url=f"https://job-boards.greenhouse.io/{token}/jobs/{jid}",
                    board_token=token,
                    posted_at=job.get("updated_at", "") or job.get("first_published", ""),
                )
            )
        return listings

    def describe(self, listing: JobListing) -> str:
        """Fetch the FULL JD text for one listing (content=true). Mutates and returns
        ``listing.description``. Scoring always runs on this, never the snippet (spec §7)."""
        url = f"{_API_BASE}/{listing.board_token}/jobs/{listing.source_job_id}?content=true"
        try:
            resp = self._get(url)
            resp.raise_for_status()
            content = resp.json().get("content", "")
        except (httpx.HTTPError, ValueError) as exc:
            raise GreenhouseError(
                f"failed to describe {listing.board_token}/{listing.source_job_id}: {exc}"
            ) from exc
        listing.description = html_to_text(content)
        return listing.description

    def close(self) -> None:
        if self._owns_client:
            self._client.close()


def confirm_probe(token: str, client: httpx.Client | None = None) -> tuple[bool, int]:
    """Seeding's confirm-probe (research §3): one GET to validate a candidate token.

    Returns ``(is_valid, open_job_count)``. Valid = HTTP 200 with ≥1 open job. Used to
    turn a guessed/harvested/dataset slug into a verified seed entry, cached by the caller.
    """
    owns = client is None
    client = client or httpx.Client(headers={"User-Agent": _USER_AGENT}, timeout=10.0)
    try:
        resp = client.get(f"{_API_BASE}/{token}/jobs")
        if resp.status_code != 200:
            return (False, 0)
        n = len(resp.json().get("jobs", []))
        return (n > 0, n)
    except (httpx.HTTPError, ValueError):
        return (False, 0)
    finally:
        if owns:
            client.close()
