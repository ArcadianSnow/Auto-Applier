"""Canonical job hashing for cross-source deduplication.

The same job listing often appears on multiple platforms — a company
cross-posts a "Senior Data Analyst" role on LinkedIn, Indeed, and its
own careers page. Each platform assigns its own opaque ``job_id``, so
our per-source dedup still lets the orchestrator apply to the same
job 2-3 times.

This module produces a **canonical hash** that identifies a job by
its normalized company + title, independent of platform. Two listings
with the same company and title (after normalization) will hash
identically regardless of which source found them.

Location is deliberately NOT part of the hash. The same job appears
as "Remote, US" on one board and "United States" on another — folding
both into a single listing is a feature, not a bug.
"""

from __future__ import annotations

import hashlib
import re

# Common corporate suffixes to strip before hashing. Ordered longest
# first so "LLC" doesn't match inside "L.L.C." after punctuation strip.
_CORP_SUFFIXES = [
    "incorporated", "corporation", "limited", "holdings", "group",
    "company", "co", "corp", "inc", "llc", "ltd", "plc", "gmbh",
    "sa", "ag", "bv", "oy", "ab", "pty",
]

# Title suffixes / decorators that shouldn't affect identity.
_TITLE_JUNK_PATTERNS = [
    r"\(remote[^)]*\)",
    r"\(hybrid[^)]*\)",
    r"\(on[- ]?site[^)]*\)",
    r"\(contract[^)]*\)",
    r"\(full[- ]?time[^)]*\)",
    r"\(part[- ]?time[^)]*\)",
    r"\bremote\b",
    r"\bhybrid\b",
    r"\bon[- ]?site\b",
    r"\bcontract\b",
    r"\bw2\b",
    r"\b[uU]\.?[sS]\.?\b",
]


def normalize_company(company: str) -> str:
    """Return a canonical form of a company name.

    - lowercase
    - strip punctuation (keep letters, digits, spaces)
    - drop trailing corporate suffixes (up to 2 in a row to catch
      "Acme Holdings, Inc." → "acme")
    - collapse whitespace
    """
    if not company:
        return ""
    s = company.lower()
    # Replace punctuation with spaces so "Acme,Inc" → "acme inc"
    s = re.sub(r"[^\w\s]", " ", s)
    # Collapse whitespace
    s = re.sub(r"\s+", " ", s).strip()
    # Drop trailing corp suffixes repeatedly
    for _ in range(3):
        parts = s.split()
        if parts and parts[-1] in _CORP_SUFFIXES:
            parts.pop()
            s = " ".join(parts)
        else:
            break
    return s.strip()


def normalize_title(title: str) -> str:
    """Return a canonical form of a job title.

    - lowercase
    - strip parenthetical / bracketed annotations about work mode
    - strip punctuation
    - collapse whitespace
    """
    if not title:
        return ""
    s = title.lower()
    for pat in _TITLE_JUNK_PATTERNS:
        s = re.sub(pat, " ", s)
    # Drop punctuation
    s = re.sub(r"[^\w\s]", " ", s)
    # Collapse whitespace
    s = re.sub(r"\s+", " ", s).strip()
    return s


def canonical_job_hash(company: str, title: str) -> str:
    """Return a 16-char hex digest identifying a job canonically.

    Empty inputs produce an empty string, which callers should treat as
    "unknown, fall back to per-source identity".
    """
    c = normalize_company(company)
    t = normalize_title(title)
    if not c or not t:
        return ""
    payload = f"{c}|{t}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:16]
