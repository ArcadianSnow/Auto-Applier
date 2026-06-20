"""Job-email classification (email-outcome-loop Phase A).

Mirrors :mod:`auto_applier.onboarding_chat`: a deterministic-first classifier with
a bounded-LLM fallback that **fail-safes** — any LLM error or a missing LLM degrades
to a safe "unknown" (``method="none"``) so the worker routes ambiguous mail to review
rather than guessing. ``classify`` NEVER raises.

The classifier projects onto the EXISTING five-value :class:`OutcomeKind` ladder
(GHOST / REJECTION / RESPONSE / INTERVIEW / OFFER); there is no ``confirmation`` value,
so an "application received" ack maps to ``RESPONSE``. ``GHOST`` is time-based and never
inferred from an email (the worker derives it elsewhere). A non-job email returns
``kind=None``.

``security_code_flag`` flags verification / one-time-code mail (the Greenhouse
security-code gate, Direction 3) so a later worker can route it to "finish assisted"
instead of treating it as an outcome.
"""

from __future__ import annotations

from dataclasses import dataclass

from auto_applier.domain.state import OutcomeKind
from auto_applier.inbox.parse import FetchedEmail
from auto_applier.llm.prompts import CLASSIFY_JOB_EMAIL

__all__ = ["EmailClass", "classify", "classify_deterministic"]


@dataclass(frozen=True)
class EmailClass:
    """The classifier verdict. ``kind=None`` == not a job-status email (ignore)."""

    kind: OutcomeKind | None
    company_hint: str
    role_hint: str
    confidence: float
    method: str               # "deterministic" | "llm" | "none"
    security_code_flag: bool


# --------------------------------------------------------------- keyword rules

# Each phrase is matched case-insensitively against the combined subject+body.
_REJECTION = (
    "unfortunately",
    "decided to move forward with other candidates",
    "move forward with other candidates",
    "not be moving forward",
    # Contraction form — real Gusto/Greenhouse rejections say "we won't be moving
    # forward at this time" with no "unfortunately". The apostrophe is normalized to
    # straight in _haystack; "wont" covers senders that drop it entirely.
    "won't be moving forward",
    "wont be moving forward",
    "will not be proceeding",
    "regret to inform",
    "no longer under consideration",
    "we have decided not to",
    "pursue other candidates",
)
# Only UNAMBIGUOUS interview invitations — an explicit invite or a concrete scheduling
# action aimed at the candidate. Deliberately NOT here: soft cues like "next steps",
# "schedule a", "your availability". VERIFIED on 30 days of live mail — bare "next steps"
# alone produced 12 false INTERVIEW positives (Snowflake/Gusto/Vanta/dbt/Render "thanks
# for applying" confirmations + a newsletter), "schedule a" 1 more (Venmo marketing), and
# ZERO true positives; even gating "next steps" on the word "interview" still leaked,
# because confirmations DESCRIBE their interview process. So a genuinely ambiguous
# interview email (a soft cue with no explicit invite) deliberately falls through to the
# LLM fallback rather than being guessed deterministically.
_INTERVIEW = (
    "invitation to interview",
    "invite you to interview",
    "invite you to an interview",
    "like to interview you",
    "interview invitation",
    "schedule an interview",
    "schedule a call",
    "schedule a phone",
    "set up a call",
    "set up a time to",
    "set up an interview",
    "phone screen",
    "phone interview",
    "video interview",
    "calendly",
    "would love to chat",
    "available to chat",
    "hop on a call",
    "jump on a call",
)
# Only unambiguous EMPLOYMENT-offer phrases. Deliberately NOT here: "your offer" /
# "job offer" — VERIFIED to produce 5 marketing false positives over 30 days of live
# mail ("your offer" fired on Ally Invest / Capital One promos), zero true positives.
# OFFER is the highest-severity outcome, so a real offer must NAME the act.
_OFFER = (
    "offer of employment",
    "pleased to offer you",
    "we are pleased to offer",
    "delighted to offer you",
    "happy to offer you",
    "extend an offer",
    "extend a formal offer",
    "your offer letter",
    "offer you the position",
    "offer you the role",
    "offer you a position",
)
_RESPONSE = (
    "thank you for applying",
    "we have received your application",
    "we've received your application",
    "application received",
    "thanks for your interest in",
    "thank you for your interest in",
    # "application has been received" not bare "has been received" — the latter fired on
    # utility/payment mail ("your payment has been received": Atmos Energy, Paymentus).
    "application has been received",
    "received your application",
    "thanks for applying",
)
_SECURITY_CODE = (
    "security code",
    "verification code",
    "one-time code",
    "one time code",
    "confirm your email",
    "verify your email",
    "your code is",
    "enter this code",
)
_NEWSLETTER = (
    "job alert",
    "new jobs for you",
    "jobs for you",
    "weekly digest",
    "weekly newsletter",
    "recommended jobs",
    "new opportunities for you",
    "jobs matching your",
)

def _haystack(email: FetchedEmail) -> str:
    # Normalize the curly apostrophe (U+2019, what most mail clients emit) to a straight
    # one so contraction-bearing phrases ("won't be moving forward") match the keyword
    # list. Straight-apostrophe keywords (e.g. "we'd like to extend") are unaffected.
    return f"{email.subject}\n{email.body_text}".lower().replace("’", "'")


def _any(haystack: str, phrases: tuple[str, ...]) -> bool:
    return any(p in haystack for p in phrases)


def _detect_security_code(email: FetchedEmail) -> bool:
    """Cheap, standalone security-code check (used by both the deterministic and
    the fail-safe paths)."""
    return _any(_haystack(email), _SECURITY_CODE)


def _company_hint(email: FetchedEmail) -> str:
    """Best-effort company guess from the From display-name, else the addr domain.
    Conservative — never overreaches into the subject's prose."""
    name = (email.from_name or "").strip()
    # Drop common no-reply / ATS-team suffixes from a display name.
    for noise in (" careers", " recruiting", " talent", " team", " hiring", " hr", " jobs", " no-reply", " noreply"):
        low = name.lower()
        if low.endswith(noise):
            name = name[: len(name) - len(noise)].strip()
    if name and "@" not in name and not name.lower().startswith(("no-reply", "noreply", "do-not-reply")):
        return name
    # Fall back to the email domain's second-level label (acme in jobs@acme.com).
    addr = email.from_addr or ""
    if "@" in addr:
        domain = addr.split("@", 1)[1]
        # strip known ATS host suffixes so "greenhouse.io" doesn't become the company
        labels = domain.split(".")
        if len(labels) >= 2:
            label = labels[-2]
            if label not in ("greenhouse", "lever", "ashbyhq", "myworkday", "gmail", "outlook", "hotmail", "yahoo"):
                return label
    return ""


def classify_deterministic(email: FetchedEmail) -> EmailClass | None:
    """Keyword/sender rules. Returns an :class:`EmailClass` for a confident hit
    (or a confident non-job ignore); returns ``None`` when nothing fires so the
    caller can try the LLM.

    Order matters: offer > interview > rejection > response. Newsletters short-circuit
    to a confident ignore so clearly-non-job mail never reaches the LLM. The
    security-code flag is computed regardless and carried on every verdict.
    """
    hay = _haystack(email)
    sec = _detect_security_code(email)
    company = _company_hint(email)

    # Clear non-job bulk mail → confidently ignore (don't burn an LLM call).
    if _any(hay, _NEWSLETTER):
        return EmailClass(
            kind=None, company_hint=company, role_hint="",
            confidence=0.9, method="deterministic", security_code_flag=sec,
        )

    def hit(kind: OutcomeKind) -> EmailClass:
        return EmailClass(
            kind=kind, company_hint=company, role_hint="",
            confidence=0.9, method="deterministic", security_code_flag=sec,
        )

    if _any(hay, _OFFER):
        return hit(OutcomeKind.OFFER)
    if _any(hay, _INTERVIEW):
        return hit(OutcomeKind.INTERVIEW)
    if _any(hay, _REJECTION):
        return hit(OutcomeKind.REJECTION)
    if _any(hay, _RESPONSE):
        return hit(OutcomeKind.RESPONSE)

    # Pure security-code mail with no status keyword: flag it, but it's not an
    # outcome — kind stays None. Confident enough to skip the LLM.
    if sec:
        return EmailClass(
            kind=None, company_hint=company, role_hint="",
            confidence=0.9, method="deterministic", security_code_flag=True,
        )

    return None  # ambiguous → caller tries the LLM


# --------------------------------------------------------------- LLM fallback

_LLM_KIND_MAP: dict[str, OutcomeKind | None] = {
    "application_received": OutcomeKind.RESPONSE,
    "rejection": OutcomeKind.REJECTION,
    "interview": OutcomeKind.INTERVIEW,
    "offer": OutcomeKind.OFFER,
    "other": None,
}


def _clamp_confidence(value: object) -> float:
    try:
        c = float(value)
    except (TypeError, ValueError):
        return 0.5
    if c != c:  # NaN
        return 0.5
    return max(0.0, min(1.0, c))


def _coerce_llm(raw: dict, email: FetchedEmail) -> EmailClass:
    """Project the model's reply onto :class:`EmailClass`. Defensive: unknown kinds
    → None (ignore), garbage confidence → 0.5, missing strings → ""."""
    raw = raw if isinstance(raw, dict) else {}
    kind_str = str(raw.get("kind") or "").strip().lower()
    kind = _LLM_KIND_MAP.get(kind_str, None)
    company = str(raw.get("company") or "").strip() or _company_hint(email)
    role = str(raw.get("role") or "").strip()
    return EmailClass(
        kind=kind,
        company_hint=company,
        role_hint=role,
        confidence=_clamp_confidence(raw.get("confidence")),
        method="llm",
        security_code_flag=_detect_security_code(email),
    )


def _fail_safe(email: FetchedEmail) -> EmailClass:
    """The never-raise fallback: unknown, low confidence, route-to-review."""
    return EmailClass(
        kind=None, company_hint="", role_hint="",
        confidence=0.0, method="none",
        security_code_flag=_detect_security_code(email),
    )


async def classify(email: FetchedEmail, *, llm=None) -> EmailClass:
    """Classify one email. Deterministic-first, bounded-LLM fallback, fail-safe.

    Never raises: a missing LLM or ANY LLM error degrades to ``method="none"`` so
    the worker routes the message to review rather than acting on a guess.
    """
    det = classify_deterministic(email)
    if det is not None:
        return det
    if llm is None:
        return _fail_safe(email)
    try:
        prompt = CLASSIFY_JOB_EMAIL.format(
            from_addr=email.from_addr,
            subject=email.subject,
            body=email.body_text,
        )
        raw = await llm.complete_json(
            prompt, system=CLASSIFY_JOB_EMAIL.system, think=False, num_predict=512,
        )
    except Exception:  # noqa: BLE001 — fail safe to review, never surface to the worker
        return _fail_safe(email)
    return _coerce_llm(raw, email)
