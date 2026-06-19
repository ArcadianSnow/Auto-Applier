"""Match a classified email back to an applied job (email-outcome-loop Phase A;
precision tightened Phase C after live findings).

Pure / offline. There is no email column on :class:`~auto_applier.domain.models.Job`,
so the matcher keys on signals that DO exist (spec §4 / domain/dedup.py): the job's
``url`` appearing in the email body (strongest), then a normalized ``company`` match,
disambiguated by the **role named in the email** when the user applied to several roles
at one company.

Precedence (first decisive signal wins):
  1. ``url``            — any job whose non-empty ``url`` is a substring of the body → 0.95
  2. ``company+role``   — normalized company match AND the email identifies the role  → 0.80
  3. ``company``        — single applied role at that company, no role signal needed   → 0.60
  4. ``company-ambiguous`` — several applied roles at that company and the email can't
                             tell them apart → job_id=None, 0.50 (below the worker's
                             floor) so it routes to REVIEW rather than guessing a sibling
  5. ``none``           — no company match                                             → 0.00

**Why role matching reads the BODY, not just the classifier hint.** Live Greenhouse /
Lever / Monzo rejection templates name the role in the *body* ("your application for the
*Machine Learning Platform Engineer* position", "the *Data Engineer II* opening") while
the subject is generic ("Your application to Monzo"). So the role text is the subject +
body + the classifier's role hint, and a candidate whose **full title appears verbatim**
in that text wins — preferring the **longest** matching title so "Data Engineer II" beats
its sibling "Data Engineer" (and not vice-versa, since "Data Engineer II" only appears
when the email actually says "II").

The worker treats anything below a confidence floor (0.6) as "no confident match →
review", honoring the APPLIED invariant: a guessed match never silently records an
outcome, and an ambiguous multi-role company fails *closed* to the human.
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
    reason: str   # "url" | "company+role" | "company" | "company-ambiguous" | "none"


def _tokens(text: str) -> set[str]:
    """Normalized token set (lowercased alnum words)."""
    return {t for t in normalize(text).split() if t}


# Trailing legal-entity tokens dropped before comparing companies, so the ATS legal
# name ("Gusto, Inc.") matches the email-domain hint ("gusto"). Conservative — only
# unambiguous suffixes; descriptive tails like "labs"/"group" are intentionally kept.
_LEGAL_SUFFIXES = frozenset({
    "inc", "incorporated", "llc", "ltd", "limited", "corp", "corporation",
    "co", "company", "gmbh", "ag", "sa", "plc", "bv", "oy", "ab", "as", "srl", "spa",
})


def _company_key(name: str) -> str:
    """Normalized company name with trailing legal-suffix tokens stripped.

    "Gusto, Inc." → "gusto" matches the email's domain-derived hint "gusto"; "Acme LLC"
    → "acme". Only *trailing* suffix tokens are removed, so "Coffee Co Roasters" keeps
    "coffee co roasters" (the "co" isn't trailing).
    """
    toks = normalize(name).split()
    while toks and toks[-1] in _LEGAL_SUFFIXES:
        toks.pop()
    return " ".join(toks)


def _role_score(job: Job, role_text: str) -> int:
    """How strongly ``role_text`` (normalized subject+body+hint) identifies this job's
    title. A verbatim full-title phrase dominates (and the longer the title, the more
    specific → the higher the score, so a superset title like "data engineer ii" beats
    its prefix "data engineer"); otherwise fall back to plain title-token overlap.

    Returns 0 when nothing in the email points at this title.
    """
    title_norm = normalize(job.title)
    if not title_norm:
        return 0
    toks = title_norm.split()
    # Verbatim multi-word title in the email → decisive; length breaks ties toward the
    # most specific role. (Single-word titles are too weak to phrase-match safely.)
    if len(toks) >= 2 and title_norm in role_text:
        return 1000 + len(title_norm)
    # Fallback: how many of the title's tokens appear anywhere in the email text.
    rt = set(role_text.split())
    return sum(1 for t in set(toks) if t in rt)


def match_email(cls: EmailClass, email: FetchedEmail, applied_jobs: list[Job]) -> MatchResult:
    """Best-match ``email`` to one of ``applied_jobs`` by the precedence above."""
    body = email.body_text or ""

    # (1) URL substring — the strongest signal, independent of the classifier hints.
    for job in applied_jobs:
        url = (job.url or "").strip()
        if url and url in body:
            return MatchResult(job_id=job.id, confidence=0.95, reason="url")

    company_key = _company_key(cls.company_hint)
    if not company_key:
        return MatchResult(job_id=None, confidence=0.0, reason="none")

    candidates = [j for j in applied_jobs if _company_key(j.company) == company_key]
    if not candidates:
        return MatchResult(job_id=None, confidence=0.0, reason="none")

    # The role signal: the classifier hint + the subject AND body. Rejection templates
    # name the role in the body, so the body is where the disambiguator usually lives.
    role_text = normalize(f"{cls.role_hint} {email.subject} {body}")

    scored = sorted(
        ((_role_score(j, role_text), j) for j in candidates),
        key=lambda s: s[0],
        reverse=True,
    )
    top_score, top_job = scored[0]

    # (3) Single applied role at this company — company match is enough; a role
    #     confirmation just bumps confidence (2).
    if len(candidates) == 1:
        if top_score > 0:
            return MatchResult(job_id=top_job.id, confidence=0.80, reason="company+role")
        return MatchResult(job_id=top_job.id, confidence=0.60, reason="company")

    # Several applied roles at this company — resolve to the EXACT one the email names.
    second_score = scored[1][0]
    if top_score > 0 and top_score > second_score:
        return MatchResult(job_id=top_job.id, confidence=0.80, reason="company+role")

    # (4) A tie (or no role signal at all) → don't blind-pick a sibling. Fail closed to
    #     review with a sub-floor confidence so the worker records no outcome.
    return MatchResult(job_id=None, confidence=0.50, reason="company-ambiguous")
