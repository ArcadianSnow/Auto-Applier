"""Auto-update feed check (spec §11a, Phase 5 5/M).

"The app checks a release feed (e.g. GitHub Releases) and **prompts to update** —
important because selectors and source quirks drift, so keeping users current is
itself a reliability feature" (spec §11a).

v3.0 scope is **check + prompt**, not auto-download-and-replace. `av3 update`
hits the GitHub Releases API, compares the latest tag against the running
version (PEP 440 via ``packaging``), and tells the user where to get the new
installer. Auto-apply of the update is intentionally out of scope — replacing a
running, browser-driving service in place is its own risk surface; a human
running the installer is the safe v3.0 path.

The network fetch is injectable (``http_get``) so the comparison logic is tested
without touching GitHub. ``check_for_update`` returns ``None`` on any failure
(offline, rate-limited, malformed feed) — an update check must NEVER raise into
a launcher or doctor run.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

__all__ = [
    "DEFAULT_REPO",
    "UpdateInfo",
    "check_for_update",
    "compare_versions",
    "parse_release_feed",
]

# The project's GitHub slug (see reference memory). The release feed is
# https://api.github.com/repos/<repo>/releases. Overridable via ``av3 update --repo``.
DEFAULT_REPO = "ArcadianSnow/Auto-Applier"

# A pluggable GET transport: ``(url) -> (status_int, json_obj)``. Injectable so
# the feed parsing is tested without network. Default uses httpx.
GetFn = Callable[[str], "tuple[int, Any]"]


@dataclass(frozen=True)
class UpdateInfo:
    """Result of a feed check."""

    current: str
    latest: str
    url: str
    is_newer: bool


def compare_versions(current: str, latest: str) -> bool:
    """Return True iff ``latest`` is a strictly newer version than ``current``
    (PEP 440). Bad/unparseable versions compare as "not newer" (fail safe —
    we never nag about a version we can't reason about)."""
    from packaging.version import InvalidVersion, Version

    try:
        return Version(_strip_v(latest)) > Version(_strip_v(current))
    except InvalidVersion:
        return False


def _strip_v(tag: str) -> str:
    """``v3.0.0`` → ``3.0.0``. GitHub tags conventionally carry a leading 'v'."""
    t = (tag or "").strip()
    return t[1:] if t[:1].lower() == "v" else t


def parse_release_feed(
    payload: Any, current_version: str, *, allow_prerelease: bool = True
) -> UpdateInfo | None:
    """Turn a GitHub Releases payload into an :class:`UpdateInfo`, or ``None`` if
    the feed has no usable release.

    Accepts either shape the API can return:
      * a **list** from ``/releases`` — we pick the newest non-draft release
        (honoring ``allow_prerelease``);
      * a **dict** from ``/releases/latest`` — used as-is.

    ``allow_prerelease`` defaults True because v3.0 ships to a small group on
    alpha builds (current = ``3.0.0a0``); skipping prereleases would hide alpha
    updates from exactly the people testing them. Flip it for a stable channel.
    """
    release: dict | None = None
    if isinstance(payload, list):
        candidates = []
        for r in payload:
            if not isinstance(r, dict) or r.get("draft"):
                continue
            if r.get("prerelease") and not allow_prerelease:
                continue
            tag = r.get("tag_name")
            if not tag:
                continue
            candidates.append(r)
        if not candidates:
            return None
        from packaging.version import InvalidVersion, Version

        def _key(r: dict):
            try:
                return Version(_strip_v(r["tag_name"]))
            except InvalidVersion:
                return Version("0")

        release = max(candidates, key=_key)
    elif isinstance(payload, dict):
        if payload.get("draft") or (payload.get("prerelease") and not allow_prerelease):
            return None
        release = payload

    if not release or not release.get("tag_name"):
        return None

    latest = _strip_v(release["tag_name"])
    url = release.get("html_url") or f"https://github.com/{DEFAULT_REPO}/releases"
    return UpdateInfo(
        current=current_version,
        latest=latest,
        url=url,
        is_newer=compare_versions(current_version, latest),
    )


def _httpx_get(timeout_s: float) -> GetFn:
    import httpx

    def _get(url: str) -> tuple[int, Any]:
        resp = httpx.get(
            url,
            timeout=timeout_s,
            headers={"Accept": "application/vnd.github+json"},
            follow_redirects=True,
        )
        try:
            body = resp.json()
        except Exception:
            body = None
        return resp.status_code, body

    return _get


def check_for_update(
    current_version: str,
    *,
    repo: str = DEFAULT_REPO,
    allow_prerelease: bool = True,
    timeout_s: float = 5.0,
    http_get: GetFn | None = None,
) -> UpdateInfo | None:
    """Fetch the release feed and return an :class:`UpdateInfo`, or ``None`` on
    any failure (offline, HTTP error, malformed feed). Never raises — callers
    (launcher, ``av3 update``, a future dashboard badge) treat ``None`` as
    "couldn't check, carry on"."""
    get = http_get or _httpx_get(timeout_s)
    url = f"https://api.github.com/repos/{repo}/releases"
    try:
        status, body = get(url)
    except Exception:
        return None
    if status < 200 or status >= 300 or body is None:
        return None
    return parse_release_feed(body, current_version, allow_prerelease=allow_prerelease)
