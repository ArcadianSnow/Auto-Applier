"""Match a classified email back to an applied job (email-outcome-loop Phase A).

Pure / offline. There is no email column on :class:`~auto_applier.domain.models.Job`,
so the matcher keys on signals that DO exist (spec §4 / domain/dedup.py): the job's
``url`` appearing in the email body (strongest), then a normalized ``company`` match
(+ a role-token overlap to tighten it), then company-only as the weak floor.

Precedence (first hit wins):
  1. ``url``          — any job whose non-empty ``url`` is a substring of the body → 0.95
  2. ``company+role`` — normalized company match AND a shared role token       → 0.80
  3. ``company``      — normalized company match alone                          → 0.60
  4. ``none``         — no match                                                → 0.00

The worker treats anything below a confidence floor as "no confident match → review",
honoring the APPLIED invariant (a guessed match never silently records an outcome).
"""

from __future__ import annotations

from dataclasses import dataclass

from auto_applier.domain.dedup import normalize
from auto_applier.domain.models import Job
from auto_applier.inbox.classify import EmailClass
from auto_applier.inbox.parse import FetchedEmail

__all__ = ["MatchResult", "match_email"]


@dataclass(frozen=True)
class MatchResult:
    """The job (if any) an email pertains to, with why and how confidently."""

    job_id: str | None
    confidence: float
    reason: str   # "url" | "company+role" | "company" | "none"


def _tokens(text: str) -> set[str]:
    """Normalized token set (lowercased alnum words)."""
    return {t for t in normalize(text).split() if t}


def match_email(cls: EmailClass, email: FetchedEmail, applied_jobs: list[Job]) -> MatchResult:
    """Best-match ``email`` to one of ``applied_jobs`` by the precedence above."""
    body = email.body_text or ""

    # (1) URL substring — the strongest signal, independent of the classifier hints.
    for job in applied_jobs:
        url = (job.url or "").strip()
        if url and url in body:
            return MatchResult(job_id=job.id, confidence=0.95, reason="url")

    company_norm = normalize(cls.company_hint)
    role_tokens = _tokens(cls.role_hint)

    if company_norm:
        company_matches = [j for j in applied_jobs if normalize(j.company) == company_norm]

        # (2) company + role-token overlap.
        if role_tokens:
            for job in company_matches:
                if role_tokens & _tokens(job.title):
                    return MatchResult(job_id=job.id, confidence=0.80, reason="company+role")

        # (3) company-only.
        if company_matches:
            return MatchResult(job_id=company_matches[0].id, confidence=0.60, reason="company")

    return MatchResult(job_id=None, confidence=0.0, reason="none")
