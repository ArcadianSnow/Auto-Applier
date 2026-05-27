"""PII scrubber for telemetry (spec §9).

Local ``events.db`` keeps FULL detail. The scrubber only runs on the path that mirrors
a scrubbed subset to the opt-in remote relay (Phase 5). It is the *client-side* first
line of defense; the relay re-scrubs as the second. We strip emails, phone numbers,
URLs with query strings, and anything that looks like a long free-text blob (résumé /
answer values must never leave the machine).

Provided now so the rule is defined and unit-tested from the start, even though the
remote mirror itself lands in Phase 5.
"""

from __future__ import annotations

import re

_EMAIL_RE = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")
_PHONE_RE = re.compile(r"\b(?:\+?\d[\d\s().-]{7,}\d)\b")
# Common file-system paths that can embed a username.
_PATH_RE = re.compile(r"(?:[A-Za-z]:\\|/)(?:[^\s\\/]+[\\/])+[^\s\\/]+")

_MAX_LEN = 500  # error messages over this are truncated; nothing free-text-long mirrors


def scrub(text: str | None) -> str | None:
    """Return a scrubbed copy safe to mirror remotely, or ``None`` unchanged."""
    if not text:
        return text
    text = _EMAIL_RE.sub("[email]", text)
    text = _PHONE_RE.sub("[phone]", text)
    text = _PATH_RE.sub("[path]", text)
    if len(text) > _MAX_LEN:
        text = text[:_MAX_LEN] + "…[truncated]"
    return text
