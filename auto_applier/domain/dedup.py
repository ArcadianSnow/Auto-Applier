"""Cross-source dedup key (spec §4, §7 #2).

A job discovered on Greenhouse and the same job re-posted on a JobSpy board are the
*same* opening to a human, but carry different ``(source, source_job_id)`` pairs. The
``canonical_hash`` collapses them to one stable key derived from the normalized
title + company, so :meth:`JobRepo.applied_canonical_hashes` dedups across sources
(only ``APPLIED`` counts — spec §5).

This is deliberately *coarse*: title + company, normalized hard. Two different reqs
for "Data Analyst" at the same company collapse to one hash — that's the intended
behavior for an apply-dedup guard (we don't want to fire twice at one employer for
near-identical roles). ``source_job_id`` remains the fine-grained per-posting key the
DB enforces uniqueness on; this hash is the human-level "same job" signal layered on top.
"""

from __future__ import annotations

import hashlib
import re

__all__ = ["canonical_hash", "normalize"]

# Punctuation/whitespace runs collapse to a single space; everything is lowercased.
# We keep alphanumerics + spaces only so "Sr. Data-Analyst (Remote)" and
# "sr data analyst remote" hash identically.
_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def normalize(text: str) -> str:
    """Lowercase, strip non-alphanumerics to single spaces, collapse + trim.

    Returns ``""`` for falsy / all-punctuation input.
    """
    if not text:
        return ""
    return _NON_ALNUM.sub(" ", text.lower()).strip()


def canonical_hash(title: str, company: str) -> str:
    """Stable cross-source dedup key = ``sha256(normalize(title)|normalize(company))[:16]``.

    16 hex chars (64 bits) — collision-negligible for a personal/small-group job
    corpus and matches the width JobSpy already uses for its own key. Returns ``""``
    only if BOTH title and company normalize to empty (nothing to key on); callers
    treat an empty hash as "not dedup-eligible" rather than letting empties collide.
    """
    t, c = normalize(title), normalize(company)
    if not t and not c:
        return ""
    return hashlib.sha256(f"{t}|{c}".encode("utf-8")).hexdigest()[:16]
