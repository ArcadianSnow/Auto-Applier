"""PII scrubber for telemetry (spec §9).

Local ``events.db`` keeps FULL detail. The scrubbers only run on the path that mirrors
a scrubbed subset to the opt-in remote relay. They are the *client-side* first line of
defense; the relay re-scrubs as the second.

Two layers:

* :func:`scrub` — *text-level* scrubber for free-form strings. Strips emails, phone
  numbers, file-system paths, and truncates anything over ``_MAX_LEN``. Used inside
  the category scrubbers below for any free-text field.
* :func:`scrub_error_event` and :func:`scrub_inferred_answer_event` — *category-level*
  scrubbers for the §9 mirror payload schemas. They operate on structured dicts,
  enforce an allow-list of fields per category, drop everything else (defence in
  depth against accidentally mirroring a new context key), and apply :func:`scrub`
  to any field that may carry free text.

Why category-level scrubbers at all: the §9 spec calls out two mirror categories
with *different* allow-lists. The error category mirrors ``error_msg`` after
text-scrubbing; the inferred-answer category mirrors ``question_text`` but **never
the answer value** — answer text stays local even if scrubbed. A single
``scrub(text)`` helper cannot enforce that "answer never leaves" invariant; a
category-shaped helper can (and does, by simply not having a field for it).
"""

from __future__ import annotations

import re
from typing import Any

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


# ---- category schemas (spec §9 mirror payloads) ---------------------------

# Allow-lists: the *only* keys that can appear in a mirrored payload. Anything
# else in the input dict is dropped on the floor. We bias toward dropping
# silently rather than raising, because a noisy enqueue path would either spam
# events.db with its own errors or, worse, be try/excepted away by a caller and
# lose the row entirely. The category scrubbers are a *defence-in-depth* layer
# — by the time we reach them the call sites have already shaped the payload.
_ERROR_FIELDS = {
    "user_id",          # sha256(handle)[:10] — never the raw name
    "app_version",
    "stage",
    "platform",
    "error_type",
    "scrubbed_error_msg",
    "ts",
}
_INFERRED_ANSWER_FIELDS = {
    "user_id",
    "question_text",
    "category",         # 'work_authorization' | 'sponsorship' | 'salary' | 'none' (NEVER 'eeo' — dropped upstream)
    "confidence",
    "outcome",          # 'answered' | 'bailed'
    "ts",
}


def _allowlist(payload: dict[str, Any], allowed: set[str]) -> dict[str, Any]:
    """Drop any key not in ``allowed``; return a new dict. Values pass through
    untouched — text scrubbing is the caller's responsibility for each field."""
    return {k: v for k, v in payload.items() if k in allowed}


def scrub_error_event(payload: dict[str, Any]) -> dict[str, Any]:
    """Return a mirror-safe error payload (spec §9 (a)).

    Caller is expected to populate ``error_msg`` as the raw message (we re-key
    and scrub it into ``scrubbed_error_msg`` here so the contract is "give us
    what the worker raised; we'll sanitize"). ``error_type`` is short and
    class-name-shaped — passed through but truncated to ``_MAX_LEN`` defensively.
    Any other key is dropped.
    """
    raw_msg = payload.get("error_msg") or payload.get("scrubbed_error_msg")
    scrubbed_msg = scrub(raw_msg) if isinstance(raw_msg, str) else None
    out: dict[str, Any] = {
        "user_id": payload.get("user_id"),
        "app_version": payload.get("app_version"),
        "stage": payload.get("stage"),
        "platform": payload.get("platform"),
        "error_type": _truncate(payload.get("error_type")),
        "scrubbed_error_msg": scrubbed_msg,
        "ts": payload.get("ts"),
    }
    # Strip None values so the wire payload is compact; the relay treats
    # absent and null-valued fields the same way.
    return {k: v for k, v in out.items() if v is not None}


def scrub_inferred_answer_event(payload: dict[str, Any]) -> dict[str, Any]:
    """Return a mirror-safe inferred-answer payload (spec §9 (b)).

    The §8b iteration loop fires one of these every time the LLM tier-3 path
    produces a confidence-gated answer. We mirror the *metadata* about that
    decision — what was asked, in which sensitive category, how confident the
    model was, and whether it filled or bailed — but **never the answer value
    itself**. There is no ``answer`` field in the schema; if a caller passes
    one it is dropped here.

    EEO rows must already be filtered upstream by the apply worker; we
    defence-in-depth on it anyway because the §8d guarantee is "EEO answers
    stay 100% local, including the metadata row".
    """
    if payload.get("category") == "eeo":
        return {}
    allowed = _allowlist(payload, _INFERRED_ANSWER_FIELDS)
    if isinstance(allowed.get("question_text"), str):
        allowed["question_text"] = scrub(allowed["question_text"])
    # Strip None values for compact wire payload.
    return {k: v for k, v in allowed.items() if v is not None}


def _truncate(value: Any) -> Any:
    """Truncate a string value to ``_MAX_LEN``; pass through non-strings."""
    if isinstance(value, str) and len(value) > _MAX_LEN:
        return value[:_MAX_LEN] + "…[truncated]"
    return value
