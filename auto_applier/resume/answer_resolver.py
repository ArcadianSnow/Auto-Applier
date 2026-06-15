"""Two-tier answer resolver for ATS custom questions (spec §8b, §8d).

The driver discovers ~20 custom questions per Greenhouse form on average; until those
are answered, even ``BROWSER_ASSISTED`` makes the human re-type everything. This module
is the answer pipeline:

  ┌───────────────────────────────────────────────────────────────────────────┐
  │  resolve(question, fact_bank) ->                                          │
  │                                                                           │
  │  Tier 0 — Sensitive-field policy (deterministic, §8d):                    │
  │    EEO / demographics  -> user self-ID OR "prefer not to answer"          │
  │    Work auth / sponsor -> explicit fact-bank field; no silent default     │
  │    Salary              -> v3.0 simple fill from user config range         │
  │  Tier 1 — Semantic match against AnswerRepo (embedding, cosine ≥ τ):     │
  │    high-similarity hit -> stored answer (source=user|inferred)            │
  │  Tier 2 — Confidence-gated LLM backup (only when bank misses):           │
  │    confidence ≥ θ -> propose answer, flag as 'inferred'                   │
  │    confidence <  θ -> bail to REVIEW                                      │
  └───────────────────────────────────────────────────────────────────────────┘

Reliability invariants this honors:
  * Fail closed. Any ambiguous / unanswerable required question bails to REVIEW; the
    apply driver downgrades the outcome to ``ASSISTED_PENDING`` (never auto-submits a
    form with a wrong required answer).
  * No silent defaulting on sensitive fields. v2's "authorized = Yes" assumption is
    explicitly retired here ([[project_us_default_assumption]]); work-auth answers come
    from the fact bank or REVIEW.
  * EEO values stay local. They flow into the answer, but ``Resolution.flag`` carries
    the sensitive-class marker so telemetry can refuse to mirror them (spec §9).
  * Inferred answers are flagged (``source='inferred'``) so the §8e feedback loop can
    promote frequently-correct inferences to canonical bank entries.

Why a Resolution dataclass instead of just ``str | None``: the driver needs to know
*which questions need a human* to decide whether to submit, *which were inferred* (worth
flagging for review-after-submit), and *which were sensitive* (telemetry mirror policy).
A bare string discards all three signals.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum

from auto_applier.domain.models import Answer, utcnow_iso
from auto_applier.llm.embed import EmbeddingClient, bytes_to_vec, cosine, vec_to_bytes
from auto_applier.resume.factbank import FactBank
from auto_applier.sources.browser.apply_base import CustomQuestion

__all__ = [
    "AnswerResolver",
    "ProfileField",
    "Resolution",
    "ResolutionSource",
    "SensitiveClass",
    "classify_profile_field",
    "classify_sensitive",
    "is_open_ended",
]


# ----------------------------------------------------------------- result types

class ResolutionSource(str, Enum):
    """Where the answer came from. Drives the §8e feedback loop + telemetry policy."""

    BANK = "bank"               # exact or semantic match against the answer repo
    INFERRED = "inferred"       # LLM tier-3 answer, confidence-gated, flagged for review
    SENSITIVE_DEFAULT = "sensitive_default"  # EEO blank -> "prefer not to answer"
    FACT_BANK = "fact_bank"     # work-auth pulled from the fact bank directly
    USER_CONFIG = "user_config"  # e.g. salary expectation from user_config.json
    PROFILE = "profile"         # contact/profile field (LinkedIn, city, name) from the bank
    REVIEW = "review"           # bailed; human must answer


class SensitiveClass(str, Enum):
    """Sensitive-field classifications drive policy (spec §8d) AND telemetry scrubbing."""

    NONE = "none"
    EEO = "eeo"                   # race / gender / veteran / disability / orientation
    WORK_AUTHORIZATION = "work_authorization"
    SPONSORSHIP = "sponsorship"
    SALARY = "salary"
    HUMAN_ATTESTATION = "human_attestation"  # "are you a human / an automated program?"
    CONSENT = "consent"           # "I have read and agree to the privacy policy / terms / AI guidelines"


@dataclass
class Resolution:
    """The output of :meth:`AnswerResolver.resolve`. ``value=None`` ⇔ REVIEW bail."""

    question: CustomQuestion
    value: str | None
    source: ResolutionSource
    confidence: float = 1.0    # 1.0 = bank/policy; <1 only for inferred
    sensitive: SensitiveClass = SensitiveClass.NONE
    needs_review: bool = False
    note: str = ""

    @property
    def fills(self) -> bool:
        return self.value is not None and not self.needs_review


# ------------------------------------------------------ sensitive-field classifier

# These patterns are deliberate broad-strokes; the cost of a false positive (treating
# a non-EEO question as EEO and answering "prefer not to answer") is low — the driver
# still REVIEWs anything required and unsupported. The cost of a false negative on a
# sensitive field is the v2 US-yes bug, which we're correcting here.

_EEO_PATTERNS = [
    r"\b(gender|sex)\b",
    r"\brace\b",
    r"\bethnic(ity)?\b",
    r"\bhispanic|latin[xao]\b",
    r"\bveteran\b",
    r"\bdisab(led|ility|ilities)\b",
    r"\bsexual orientation\b",
    r"\bLGBT[Q]?[I]?[A]?\+?\b",
    r"\btransgender\b",
    r"\bpronouns?\b",
]
_WORK_AUTH_PATTERNS = [
    r"\bauthor(ized|ization|ised|isation)\b.*\b(work|employment)\b",
    r"\b(work|employment).*\bauthor(ized|ization|ised|isation)\b",
    r"\bright to work\b",
    r"\blegally\b.*\b(work|employ)\b",
    # "Are you currently eligible to work in your country of residence?" — seen live on
    # Grafana 2026-06-12, missed by the older patterns and auto-answered by the LLM
    # instead of the deterministic fact-bank policy. "eligible to work" is work-auth.
    r"\beligib(le|ility)\b.*\b(work|employ)\b",
    r"\b(work|employ)\w*\b.*\beligib(le|ility)\b",
    r"\b(work|employment)\s+permit\b",
    r"\bcitizen(ship)?\s+status\b",
]
_SPONSORSHIP_PATTERNS = [
    r"\bsponsor(ship)?\b",
    r"\bvisa\b",
    r"\bH-?1B\b",
    r"\bgreen\s+card\b",
    r"\bimmigration\b",
]
_SALARY_PATTERNS = [
    r"\bsalary\b",
    r"\bcompensation\b",
    r"\bdesired pay\b",
    r"\b(expected|expectation).*\b(pay|salary|compensation)\b",
    r"\bpay range\b",
]

# Human-attestation gate (a knockout for an automated submitter). Seen live on a real
# Solutions form 2026-06-12: a "Which of the following best describes you?" dropdown
# whose options were "I am an AI or automated program" / "I am a human being". The bot
# is an automated program; auto-ticking "human being" would be a FALSE attestation, and
# these gates exist precisely to catch bots. The only correct behavior is to NOT answer
# it and hand the form to the human (who then truthfully attests as the human reviewing
# + submitting). Two detection layers because the label is often non-descriptive:
#   (1) label patterns — catch the descriptive phrasings;
#   (2) option-pair — catch "describes you?" style labels by their human-vs-AI options.
# Deliberately NOT matched: the reCAPTCHA "I'm not a robot" *widget* (handled by the
# CAPTCHA classifier, not the resolver) — these patterns target a real radio/select.
_HUMAN_ATTESTATION_LABEL_PATTERNS = [
    r"\bare you (an? )?(human|robot|bot|ai)\b",
    r"\bhuman being\b",
    r"\bautomated (program|agent|system|tool|script)\b",
    r"\b(a |an )?(ro)?bot\b.*\b(human|person)\b",
    r"\b(human|person)\b.*\b(a |an )?(ro)?bot\b",
    r"\bare you a real (person|human)\b",
]
# The live Grafana gate (2026-06-12) was a react-select labelled exactly "Which of the
# following best describes you?" — its options ("I am an AI…" / "I am a human being")
# are NOT in the DOM until the menu opens, so option-pair detection can't see them and
# the bare label matches none of the patterns above. So we ALSO treat a generic
# "best describes you" select as the attestation gate — UNLESS the label carries a
# demographic noun (then it's a self-ID question → EEO, handled below). Both branches
# fail safe; this just routes the common case to the right bail.
_DESCRIBES_YOU = re.compile(r"\bbest describes you\b", re.IGNORECASE)

# Consent / acknowledgment gates ("I have read and understand the Candidate Privacy
# Policy and AI Guidelines…", "I agree to the terms…"). A bot must not knowingly consent
# on the user's behalf — especially to AI-tool-use-in-hiring policies. Always bail to the
# human, who agrees (or not) when they review + submit. (Live Tailscale form 2026-06-13.)
_CONSENT_PATTERNS = [
    r"\bi (have read|acknowledge|agree|consent|certify|confirm|understand)\b",
    r"\bprivacy (policy|notice|statement)\b",
    r"\bterms (and conditions|of service|of use)\b",
    r"\bconsent to\b",
    r"\bai guidelines\b",
]
# An option counts as a "human" affirmation or an "AI/automated" disclosure. The gate
# fires only when BOTH appear among the options — that pairing is the signature.
_ATTEST_HUMAN_OPTION = re.compile(
    r"\b(human being|i am (a )?human|real (person|human)|a person)\b", re.IGNORECASE
)
_ATTEST_AI_OPTION = re.compile(
    r"\b(an? )?(ai|a\.i\.|automated|bot|robot|machine|program|agent)\b", re.IGNORECASE
)


def _matches_any(text: str, patterns: list[str]) -> bool:
    for p in patterns:
        if re.search(p, text, re.IGNORECASE):
            return True
    return False


def _is_attestation_option_pair(options: list[str] | None) -> bool:
    """True iff the options include BOTH a human affirmation AND an AI/automated option.

    This is how we catch the non-descriptive "Which of the following best describes you?"
    label — by the human-vs-AI shape of its choices. A normal select (country, years of
    experience, a yes/no) won't have both markers, so the false-positive cost is ~nil.
    """
    if not options:
        return False
    has_human = any(_ATTEST_HUMAN_OPTION.search(o or "") for o in options)
    has_ai = any(_ATTEST_AI_OPTION.search(o or "") for o in options)
    return has_human and has_ai


def classify_sensitive(label: str, options: list[str] | None = None) -> SensitiveClass:
    """Classify a question by sensitivity (spec §8d) from its label (+ optional options).

    Order matters. HUMAN_ATTESTATION is checked FIRST because it is a hard safety gate
    (a bot must never attest to being human) and must win over any weaker overlap.
    SPONSORSHIP then wins over WORK_AUTHORIZATION because "do you require sponsorship?"
    matches both patterns but the answer comes from a different fact-bank field. Salary
    is checked before EEO because compensation language occasionally overlaps with
    demographic surveys.
    """
    s = label or ""
    if _matches_any(s, _HUMAN_ATTESTATION_LABEL_PATTERNS) or _is_attestation_option_pair(options):
        return SensitiveClass.HUMAN_ATTESTATION
    # Generic "…best describes you?" with no demographic noun = the bot-check gate
    # (react-select hides its options). With a demographic noun it's self-ID → EEO.
    if _DESCRIBES_YOU.search(s) and not _matches_any(s, _EEO_PATTERNS):
        return SensitiveClass.HUMAN_ATTESTATION
    if _matches_any(s, _CONSENT_PATTERNS):
        return SensitiveClass.CONSENT
    if _matches_any(s, _SPONSORSHIP_PATTERNS):
        return SensitiveClass.SPONSORSHIP
    if _matches_any(s, _WORK_AUTH_PATTERNS):
        return SensitiveClass.WORK_AUTHORIZATION
    if _matches_any(s, _SALARY_PATTERNS):
        return SensitiveClass.SALARY
    if _matches_any(s, _EEO_PATTERNS):
        return SensitiveClass.EEO
    return SensitiveClass.NONE


# ----------------------------------------------------- profile/contact-field classifier
#
# These map to the fact bank's contact block (name / location / links). They were reaching
# the LLM tier and getting yes/no'd ("Website" → "No, I have not built a website"; "LinkedIn
# Profile" → "I don't have one"). They are deterministic lookups, not questions. A field
# with NO bank value BAILS to assisted (blank) — never a negation (live finding 2026-06-12).

class ProfileField(str, Enum):
    NONE = "none"
    LINKEDIN = "linkedin"
    GITHUB = "github"
    WEBSITE = "website"          # personal site / portfolio (NOT github)
    PREFERRED_FIRST_NAME = "preferred_first_name"
    PREFERRED_LAST_NAME = "preferred_last_name"
    CITY = "city"
    COUNTRY = "country"
    COUNTRY_TIMEZONE = "country_timezone"
    LOCATION = "location"        # full "City, State, Country"


# Order matters: most-specific first. GitHub before generic website; preferred-name before
# a bare "name"; country+timezone before bare country; city/location before country.
_PROFILE_PATTERNS: list[tuple[ProfileField, list[str]]] = [
    (ProfileField.LINKEDIN, [r"\blinked\s?in\b"]),
    (ProfileField.GITHUB, [r"\bgit\s?hub\b"]),
    (ProfileField.PREFERRED_LAST_NAME,
     [r"\bpreferred (last|family|sur)\s?name\b",
      r"\b(last|family|sur)\s?name you (go by|prefer)\b"]),
    (ProfileField.PREFERRED_FIRST_NAME,
     [r"\bpreferred (first )?name\b", r"\bnick\s?name\b",
      r"\bwhat should we call you\b", r"\bgoes by\b"]),
    (ProfileField.COUNTRY_TIMEZONE,
     [r"\bcountry and time\s?zone\b", r"\btime\s?zone and country\b",
      r"\btime\s?zone\b"]),
    (ProfileField.CITY,
     [r"\bcity\b", r"\blocation \(city\)\b", r"\bwhat city\b"]),
    (ProfileField.WEBSITE,
     [r"\b(website|web site|portfolio|personal site|personal web)\b"]),
    (ProfileField.COUNTRY,
     [r"\bwhat country\b", r"\bcountry of residence\b", r"\bcountry you (are|'re) (in|based)\b",
      r"^\s*country\b"]),
    (ProfileField.LOCATION,
     [r"\bwhere are you (located|based)\b", r"\bcurrent location\b",
      r"\byour location\b", r"\bcity[,/ ]+state\b"]),
]


def classify_profile_field(label: str) -> ProfileField:
    """Classify a question as a contact/profile field (or NONE). Deterministic lookup."""
    s = label or ""
    for field_kind, patterns in _PROFILE_PATTERNS:
        if _matches_any(s, patterns):
            return field_kind
    return ProfileField.NONE


# --------------------------------------------------------- open-ended (essay) detection
#
# The §8f copilot is built for yes/no SCREENERS. On an open-ended "Why/Describe/Tell us"
# prompt it manufactures a yes/no and fills the NEGATION — live 2026-06-12 it wrote "Not
# interested in a Solutions Engineer role" on an SE application. So open-ended prompts must
# come from the answer BANK (a prepared, seeded answer) or BAIL to assisted — the LLM is
# never allowed to free-write an essay from scratch. Better a blank the human fills than a
# confident wrong answer submitted under their name.
_OPEN_ENDED_PATTERNS = [
    r"\bwhy\b",
    r"\bdescribe\b",
    r"\btell us\b", r"\btell me\b",
    r"\bwhat (is|are|was|were|makes|motivates|interests|excites|draws|attracts)\b",
    r"\bexplain\b", r"\belaborate\b", r"\bin your own words\b",
    r"\bshare (a|an|your|some)\b",
    r"\bwhat.{0,30}\bexperience\b",
    r"\bhow (would|do|did|have) you\b",
    r"\bwalk (us|me) through\b",
    r"\bgive (an|us an|me an) example\b",
    r"\binterested in\b", r"\bmotivat", r"\bexcites you\b",
]


def is_open_ended(label: str, kind: str = "") -> bool:
    """True for essay/motivation prompts: a textarea, or a 'why/describe/tell us' label.

    A textarea is treated as open-ended regardless of wording — it's a prose field, and we
    never want the LLM inventing prose for it without a prepared answer.
    """
    if (kind or "").lower() == "textarea":
        return True
    return _matches_any(label or "", _OPEN_ENDED_PATTERNS)


# A minimal US state → timezone map for the "what country and time zone?" field. Best-effort;
# unknown states fall back to just the country. Keys are lowercased state names.
_US_TIMEZONE = {
    "texas": "Central Time", "illinois": "Central Time", "missouri": "Central Time",
    "california": "Pacific Time", "washington": "Pacific Time", "oregon": "Pacific Time",
    "new york": "Eastern Time", "florida": "Eastern Time", "georgia": "Eastern Time",
    "massachusetts": "Eastern Time", "virginia": "Eastern Time", "pennsylvania": "Eastern Time",
    "colorado": "Mountain Time", "arizona": "Mountain Time", "utah": "Mountain Time",
}


_AUTHORIZED_HINTS = re.compile(
    r"\b(citizen|authoriz|authoris|permanent resident|green ?card|eligible|"
    r"indefinite leave|settled status|no sponsorship)\b", re.IGNORECASE
)


def _is_authorized(bank) -> bool:
    """Confidently authorized to work in their own country? Used to answer yes/no
    eligibility questions. True when sponsorship is explicitly not required, or the
    work-auth status reads as citizen/authorized/permanent. Conservative: unknown → False
    (falls back to the status string → unmatched select → assisted, which is safe)."""
    if getattr(bank, "requires_sponsorship", None) is False:
        return True
    return bool(_AUTHORIZED_HINTS.search(bank.work_authorization or ""))


# "How did you hear about us?" — a referral-source PICKER (select/combobox), NOT an essay.
# It matches the open-ended "how did you…" pattern, so without a dedicated route it bails to
# assisted (live dbt Labs 2026-06-14: "How did you hear about us?" bailed while Grafana's
# "…about this opportunity" matched the seeded answer). Route it to the banked channel.
_HOW_HEARD_PATTERNS = [
    r"\bhear about\b",
    r"\bhow did you (hear|find|learn|come across)\b",
    r"\bhow.{0,15}\bhear\b",
    r"\breferral source\b",
    r"\bhow were you referred\b",
]


def _is_how_heard(label: str) -> bool:
    return _matches_any(label or "", _HOW_HEARD_PATTERNS)


# "Are you located in or willing to relocate to <country>?" — a yes/no the bank answered
# with prose that never committed to the react-select (live Tailscale 2026-06-14). Handle it
# directly: "Yes" for the residence/authorized country (a certainty), bail for any other
# (a personal relocation decision, not ours to auto-answer).
_RELOCATION_PATTERNS = [
    r"\bwilling to relocate\b",
    r"\b(open|able) to relocat",
    r"\brelocate to\b",
    r"\bwilling to (move|relocate)\b",
    r"\blocated in .* relocate\b",
]


def _is_relocation_question(label: str) -> bool:
    return _matches_any(label or "", _RELOCATION_PATTERNS)


# Work-authorization geography (live 2026-06-14). A US citizen is authorized in the US but
# NOT abroad — so "eligible to work in Australia?" is "No" and "require sponsorship in
# Canada?" is "Yes". The old code answered off the home-country status and got both wrong.
# We act ONLY on an EXPLICITLY named country; a generic "your country of residence" / "the
# country of this role" keeps the residence-context behaviour. Bare "us"/"US" is excluded
# (it collides with the pronoun "hear about us"); a missed US match just falls through to the
# already-correct residence-context logic. Dotted "U.S." is omitted (the trailing-dot \b is
# unreliable) for the same reason — harmless, since US falls through correctly.
_COUNTRY_ALIASES = {
    "united states of america": "united states", "united states": "united states",
    "usa": "united states", "america": "united states",
    "canada": "canada", "australia": "australia", "new zealand": "new zealand",
    "united kingdom": "united kingdom", "england": "united kingdom",
    "scotland": "united kingdom", "ireland": "ireland", "germany": "germany",
    "france": "france", "netherlands": "netherlands", "singapore": "singapore",
    "india": "india", "japan": "japan", "spain": "spain", "italy": "italy",
    "sweden": "sweden", "switzerland": "switzerland", "mexico": "mexico",
    "brazil": "brazil", "poland": "poland", "portugal": "portugal",
}


def _detect_country(label: str) -> str:
    """The country a question EXPLICITLY names → canonical name, or "" if none. Longest
    alias first so 'united states of america' wins over 'america'."""
    s = (label or "").lower()
    for alias in sorted(_COUNTRY_ALIASES, key=len, reverse=True):
        if re.search(r"\b" + re.escape(alias) + r"\b", s):
            return _COUNTRY_ALIASES[alias]
    return ""


def _canon_country(name: str) -> str:
    """Canonicalize a fact-bank country name (relocation prefs) the same way labels resolve,
    so 'Netherlands' / 'netherlands' / 'NL'-aliases all compare equal to a detected country."""
    n = (name or "").strip().lower()
    return _COUNTRY_ALIASES.get(n, n)


def _authorized_countries(bank) -> set:
    """Countries the user can work in without sponsorship — DERIVED, never defaulted: the
    residence country (from contact.location) plus the US when work_authorization reads as
    US citizen/national. Empty when nothing is known — and the callers only downgrade a named
    country to "foreign" when this set is NON-empty, so an unknown bank can't misfire."""
    out: set = set()
    contact = getattr(bank, "contact", None)
    _, _, residence = _split_location(getattr(contact, "location", "") or "")
    rc = _COUNTRY_ALIASES.get(residence.strip().lower())
    if rc:
        out.add(rc)
    wa = (getattr(bank, "work_authorization", "") or "").lower()
    if re.search(r"\b(usa|u\.?s\.?a?|american|united states)\b", wa):
        out.add("united states")
    return out


def _is_yes_no_question(question: CustomQuestion) -> bool:
    """Heuristic: does this question want a Yes/No (not a status string)? True when the
    options are a yes/no pair, or the label is phrased as a yes/no ("Are you…/Do you…")
    and isn't asking for a status/what/which."""
    opts = [o.strip().lower() for o in (getattr(question, "options", None) or []) if o.strip()]
    if opts and set(opts) <= {"yes", "no", "y", "n"}:
        return True
    label = (question.label or "").strip().lower()
    if re.match(r"^(are|do|does|can|will|have|has|is)\b", label) and not _matches_any(
        label, [r"\bstatus\b", r"\bwhat\b", r"\bwhich\b"]
    ):
        return True
    return False


def _split_location(location: str) -> tuple[str, str, str]:
    """Parse "City, State, Country" → (city, state/region, country). Tolerant of 1–3 parts:
    one part → country; two → (city, country); three+ → (city, region, country)."""
    parts = [p.strip() for p in (location or "").split(",") if p.strip()]
    if not parts:
        return "", "", ""
    if len(parts) == 1:
        return "", "", parts[0]
    if len(parts) == 2:
        return parts[0], "", parts[1]
    return parts[0], parts[1], parts[-1]


# --------------------------------------------------------------------- resolver


@dataclass
class _ResolverConfig:
    """Tuning knobs — surfaced so a settings change can re-tune without code edits."""

    semantic_match_threshold: float = 0.78
    llm_confidence_threshold: float = 0.7


class AnswerResolver:
    """Two-tier resolver. Construct once per apply run; call :meth:`resolve` per Q.

    The class is async because the embedding + LLM calls are; sensitive-field policy
    paths are sync internally but the public surface stays async-uniform.
    """

    def __init__(
        self,
        fact_bank: FactBank,
        answer_repo,
        embed_client: EmbeddingClient | None = None,
        llm_client=None,
        salary_expectation: str = "",
        config: _ResolverConfig | None = None,
        attest_human: bool = False,
    ):
        self.fact_bank = fact_bank
        self.answer_repo = answer_repo
        self.embed_client = embed_client
        self.llm_client = llm_client
        self.salary_expectation = salary_expectation
        self.config = config or _ResolverConfig()
        # Owner opt-in (default OFF): fill the human option on a STATIC "which best describes
        # you? [human/AI]" self-ID FORM FIELD. The applicant is a human, and such a field is
        # not a behavioural/risk-scored anti-bot challenge (those are CAPTCHA/fingerprint,
        # classified separately and never automated). OFF → the safe bail-to-assisted default.
        self.attest_human = attest_human

    # ---- public ----------------------------------------------------------

    async def resolve(self, question: CustomQuestion) -> Resolution:
        sensitivity = classify_sensitive(
            question.label, getattr(question, "options", None)
        )
        if sensitivity is not SensitiveClass.NONE:
            return self._resolve_sensitive(question, sensitivity)
        # Profile/contact fields (LinkedIn, city, preferred name…) are deterministic bank
        # lookups, NOT questions — resolve them before the LLM can yes/no them.
        profile = classify_profile_field(question.label)
        if profile is not ProfileField.NONE:
            return self._resolve_profile(question, profile)
        # "How did you hear about us?" is a referral-source PICKER, not an essay — but it
        # matches the open-ended "how did you…" pattern, so without this it bails. Route it
        # to the banked channel before the essay/LLM tiers can (live dbt Labs 2026-06-14).
        if _is_how_heard(question.label):
            return self._resolve_how_heard(question)
        # "located in / willing to relocate to <country>?" — Yes only for the residence/
        # authorized country (a certainty); any other country is a personal call (assisted).
        if _is_relocation_question(question.label):
            return self._resolve_relocation(question)
        bank_hit = await self._resolve_from_bank(question)
        if bank_hit is not None:
            return bank_hit
        # Open-ended essays must come from the bank (a prepared answer) or BAIL — the LLM
        # is never allowed to free-write prose (it produced wrong negations live). Binary
        # screeners still go to the copilot-audited tier below.
        if is_open_ended(question.label, getattr(question, "kind", "")):
            return self._review(
                question,
                note="open-ended prompt, no prepared/bank answer — bailed to assisted "
                     "(LLM essay invention disabled; seed the answer to auto-fill it)",
            )
        if self.llm_client is not None:
            llm_hit = await self._resolve_via_llm(question)
            if llm_hit is not None:
                return llm_hit
        return self._review(question, note="no bank match and LLM unavailable/low-confidence")

    async def resolve_all(self, questions: list[CustomQuestion]) -> list[Resolution]:
        """Resolve a list, preserving order. Driver-friendly batch entry point."""
        out: list[Resolution] = []
        for q in questions:
            out.append(await self.resolve(q))
        return out

    # ---- sensitive (spec §8d) -------------------------------------------

    def _resolve_sensitive(
        self, question: CustomQuestion, sensitivity: SensitiveClass
    ) -> Resolution:
        bank = self.fact_bank
        if sensitivity is SensitiveClass.HUMAN_ATTESTATION:
            # Default: REVIEW — never a value. The bot must never FALSELY attest to being
            # human; the human attests truthfully when they review + submit (assisted).
            # OWNER OPT-IN (settings.attest_human, user-directed 2026-06-14): a static "which
            # best describes you? [human/AI]" FORM FIELD that every visitor sees is NOT a
            # behavioural/risk-scored anti-bot challenge (those are CAPTCHA/fingerprint —
            # classified separately, never automated). It's a self-ID question, and the
            # applicant IS a human, so filling the human option is truthful. When opted in we
            # fill it; the real anti-bot defences stay sacrosanct. (NEVER reach here for a
            # CAPTCHA — that's a different classifier.)
            if self.attest_human:
                opts = getattr(question, "options", None) or []
                human = next((o for o in opts if _ATTEST_HUMAN_OPTION.search(o or "")), "")
                return Resolution(
                    question=question,
                    value=human or "I am a human being",
                    source=ResolutionSource.USER_CONFIG,
                    sensitive=SensitiveClass.HUMAN_ATTESTATION,
                    note="human self-ID: owner opted in (attest_human) — filled the human "
                         "option truthfully (static form field, not a bot-detection challenge)",
                )
            return self._review(
                question,
                note="human-attestation gate — a bot must never attest to being human; "
                     "handed to the human to answer truthfully (assisted)",
                sensitivity=SensitiveClass.HUMAN_ATTESTATION,
            )
        if sensitivity is SensitiveClass.CONSENT:
            # A bot must not knowingly consent on the user's behalf (privacy policy / terms /
            # AI-tool-use-in-hiring guidelines). Always bail → assisted; the human agrees.
            return self._review(
                question,
                note="consent/acknowledgment gate — the human must knowingly agree (assisted)",
                sensitivity=SensitiveClass.CONSENT,
            )
        if sensitivity is SensitiveClass.EEO:
            value = self._pick_eeo(question.label, bank.eeo)
            return Resolution(
                question=question,
                value=value,
                source=ResolutionSource.SENSITIVE_DEFAULT if value == "Prefer not to answer"
                else ResolutionSource.BANK,
                sensitive=SensitiveClass.EEO,
                note="EEO: user self-ID or prefer-not-to-answer",
            )
        if sensitivity is SensitiveClass.WORK_AUTHORIZATION:
            if not bank.work_authorization:
                return self._review(
                    question,
                    note="work authorization not captured in fact bank — no silent default",
                    sensitivity=SensitiveClass.WORK_AUTHORIZATION,
                )
            # A yes/no eligibility question ("Are you authorized/eligible to work…?") needs
            # "Yes", not the raw status string "US Citizen" (which won't match a Yes/No
            # select — live 2026-06-12 the dropdown was left blank). Map to Yes/No when the
            # question is yes/no AND we can confidently determine authorization; otherwise
            # return the status string (status selects/free-text take that).
            # Country-aware (live 2026-06-14): a US citizen is authorized in the US but NOT
            # abroad. When the question NAMES a country the user isn't authorized in ("eligible
            # to work in Australia?"), the honest yes/no is "No" — the old code answered "Yes"
            # off the home-country status. Only fires with a KNOWN authorized set (so an
            # unspecified bank can't misfire); a generic/residence question keeps "Yes".
            country = _detect_country(question.label)
            authorized = _authorized_countries(bank)
            if country and authorized and country not in authorized and _is_yes_no_question(question):
                return Resolution(
                    question=question,
                    value="No",
                    source=ResolutionSource.FACT_BANK,
                    sensitive=SensitiveClass.WORK_AUTHORIZATION,
                    note=f"eligibility in {country} → No (authorized only in {sorted(authorized)})",
                )
            if _is_yes_no_question(question) and _is_authorized(bank):
                value = "Yes"
                note = "work-auth eligibility → Yes (authorized; from fact bank)"
            else:
                value = bank.work_authorization
                note = "work authorization status from fact bank (explicit, never defaulted)"
            return Resolution(
                question=question,
                value=value,
                source=ResolutionSource.FACT_BANK,
                sensitive=SensitiveClass.WORK_AUTHORIZATION,
                note=note,
            )
        if sensitivity is SensitiveClass.SPONSORSHIP:
            # Country-aware (live 2026-06-14): the user WILL need sponsorship anywhere they're
            # not already authorized. When the question names such a country ("require visa
            # sponsorship to work in Canada?") the answer is "Yes" regardless of the home-country
            # requires_sponsorship flag (which only speaks to the residence country). Only fires
            # with a KNOWN authorized set, so an unspecified bank can't misfire.
            country = _detect_country(question.label)
            authorized = _authorized_countries(bank)
            if country and authorized and country not in authorized:
                return Resolution(
                    question=question,
                    value="Yes",
                    source=ResolutionSource.FACT_BANK,
                    sensitive=SensitiveClass.SPONSORSHIP,
                    note=f"sponsorship in {country} → Yes (not authorized there)",
                )
            if bank.requires_sponsorship is None:
                return self._review(
                    question,
                    note="sponsorship status not captured — no silent default",
                    sensitivity=SensitiveClass.SPONSORSHIP,
                )
            return Resolution(
                question=question,
                value="Yes" if bank.requires_sponsorship else "No",
                source=ResolutionSource.FACT_BANK,
                sensitive=SensitiveClass.SPONSORSHIP,
                note="sponsorship from fact bank (explicit)",
            )
        # SALARY — v3.0 minimum: fill from user config; the intelligent comp module is v3.1.
        if not self.salary_expectation:
            return self._review(
                question,
                note="no salary expectation configured — v3.0 fills from user_config",
                sensitivity=SensitiveClass.SALARY,
            )
        return Resolution(
            question=question,
            value=self.salary_expectation,
            source=ResolutionSource.USER_CONFIG,
            sensitive=SensitiveClass.SALARY,
            note="salary expectation from user config (v3.0); intelligence layer is v3.1",
        )

    @staticmethod
    def _pick_eeo(label: str, eeo: dict[str, str]) -> str:
        """Match an EEO label to a self-ID key. Blank or no match -> prefer-not."""
        lowered = label.lower()
        for key, val in eeo.items():
            if key.lower() in lowered:
                return val or "Prefer not to answer"
        return "Prefer not to answer"

    # ---- profile/contact fields (deterministic bank lookups) -------------

    def _resolve_profile(self, question: CustomQuestion, field: ProfileField) -> Resolution:
        """Fill a contact/profile field from the fact bank. Missing value → BAIL (assisted),
        never a negation. A blank the human fills beats "No, I have not built a website"."""
        contact = self.fact_bank.contact
        value = self._profile_value(field, contact)
        if not value:
            return self._review(
                question,
                note=f"profile field '{field.value}' not in fact bank — bailed to assisted "
                     f"(add it to the bank to auto-fill)",
            )
        return Resolution(
            question=question,
            value=value,
            source=ResolutionSource.PROFILE,
            note=f"profile field '{field.value}' from fact bank",
        )

    def _resolve_how_heard(self, question: CustomQuestion) -> Resolution:
        """'How did you hear about us?' → the banked referral channel (a picker, not an
        essay). Reads the seeded answer; bails if none is banked (never invents a channel)."""
        for key in ("How did you hear about this opportunity?", question.label):
            banked = self.answer_repo.get(key)
            if banked is not None and banked.answer:
                return Resolution(
                    question=question,
                    value=banked.answer,
                    source=ResolutionSource.BANK,
                    note="how-heard referral channel from answer bank",
                )
        return self._review(
            question,
            note="how-heard: no referral channel banked — seed one to auto-fill",
        )

    def _resolve_relocation(self, question: CustomQuestion) -> Resolution:
        """'Are you located in / willing to relocate to <country>?' — answered from the user's
        declared preferences (fact-bank ``relocation``): the residence/authorized country and
        any ``willing`` country → 'Yes'; an ``unwilling`` country → 'No'; anything undeclared
        bails to the human (a personal decision we never guess)."""
        country = _detect_country(question.label)
        if not country:
            return self._review(
                question,
                note="relocation: no country named — handed to the human (assisted)",
            )
        prefs = getattr(self.fact_bank, "relocation", {}) or {}
        willing = {_canon_country(c) for c in prefs.get("willing", [])}
        unwilling = {_canon_country(c) for c in prefs.get("unwilling", [])}
        if country in _authorized_countries(self.fact_bank) or country in willing:
            return Resolution(
                question=question, value="Yes", source=ResolutionSource.FACT_BANK,
                note=f"relocation to {country}: residence/authorized or declared willing → Yes",
            )
        if country in unwilling:
            return Resolution(
                question=question, value="No", source=ResolutionSource.FACT_BANK,
                note=f"relocation to {country}: declared unwilling → No",
            )
        return self._review(
            question,
            note=f"relocation to {country} — not in declared preferences; "
                 f"handed to the human (assisted)",
        )

    @staticmethod
    def _profile_value(field: ProfileField, contact) -> str:
        links = {k.lower(): v for k, v in (contact.links or {}).items()}
        city, region, country = _split_location(contact.location)
        if field is ProfileField.LINKEDIN:
            return links.get("linkedin", "")
        if field is ProfileField.GITHUB:
            return links.get("github", "")
        if field is ProfileField.WEBSITE:
            # personal site / portfolio; fall back to GitHub — these fields are typically
            # labelled "Portfolio (i.e. website, github, blogs, etc)", so GitHub is a valid
            # answer when there's no dedicated personal site.
            for k in ("website", "portfolio", "personal", "site", "blog", "github"):
                if k in links:
                    return links[k]
            return ""
        if field is ProfileField.PREFERRED_FIRST_NAME:
            return (contact.name or "").split()[0] if contact.name else ""
        if field is ProfileField.PREFERRED_LAST_NAME:
            # The form itself says "if your legal last name and preferred last name are the
            # same, input your legal last name" — so the legal last name (last token) is the
            # right fill. A single-token name has no distinct surname → "" → bails (never guess).
            parts = (contact.name or "").split()
            return parts[-1] if len(parts) > 1 else ""
        if field is ProfileField.CITY:
            return city
        if field is ProfileField.COUNTRY:
            return country
        if field is ProfileField.LOCATION:
            return contact.location or ""
        if field is ProfileField.COUNTRY_TIMEZONE:
            tz = _US_TIMEZONE.get(region.lower()) if country.lower() in ("united states", "usa", "us") else ""
            if country and tz:
                return f"{country} ({tz})"
            return contact.location or country
        return ""

    # ---- semantic bank match (Tier 1) -----------------------------------

    async def _resolve_from_bank(self, question: CustomQuestion) -> Resolution | None:
        # Fast path: exact question text match (common when v2's flat answers.json was
        # seeded as-is). Skips the embedding round-trip. Also try the label with the trailing
        # required-marker stripped — live Tailscale labels keep the "*" ("…before?*"), which
        # broke an exact match against the seeded "…before?" (2026-06-15).
        label = question.label or ""
        keys = [label]
        stripped = label.rstrip(" *\t")
        if stripped and stripped != label:
            keys.append(stripped)
        for key in keys:
            exact = self.answer_repo.get(key)
            if exact is not None and exact.answer:
                return Resolution(
                    question=question,
                    value=exact.answer,
                    source=ResolutionSource.BANK,
                    note="exact question-text match",
                )
        if self.embed_client is None:
            return None
        # Semantic match: embed the question, cosine vs each stored vector. Bank is
        # bounded (dozens-low-hundreds) so a Python loop is fine — no ANN index.
        try:
            q_vec = await self.embed_client.embed(question.label)
        except Exception as exc:  # noqa: BLE001 — embed failure -> drop to next tier
            return None
        best_score = 0.0
        best: Answer | None = None
        for stored in self.answer_repo.all():
            v = bytes_to_vec(stored.embedding)
            score = cosine(q_vec, v)
            if score > best_score:
                best_score = score
                best = stored
        if best is None or best_score < self.config.semantic_match_threshold:
            return None
        return Resolution(
            question=question,
            value=best.answer,
            source=ResolutionSource.BANK,
            confidence=best_score,
            note=f"semantic match: '{best.question}' (cosine={best_score:.2f})",
        )

    # ---- LLM backup (Tier 2) — copilot-audited (spec §8f) ----------------

    async def _resolve_via_llm(self, question: CustomQuestion) -> Resolution | None:
        """Tier-3 inference, routed through the §8f copilot so the answer passes
        the deterministic EVIDENCE AUDIT before it can fill a form.

        The previous tier-3 gated on the model's SELF-reported confidence — and
        a live dry-run (2026-06-11, GitLab screeners) showed qwen3:8b reporting
        0.95 on "do you have production Kubernetes/Go experience?" judgment
        calls whose honest answer was No. Self-reported confidence is exactly
        the overclaim trap; the copilot's audit (a yes/partial verdict must
        cite bank facts that deterministically check out, else review) is the
        backstop. Anything the audit fails returns None → the caller bails to
        REVIEW and the driver downgrades to assisted. Never raises.
        """
        from auto_applier.copilot import Copilot  # lazy — copilot imports this module

        answer = await Copilot(self.llm_client).answer(
            question.label or "", self.fact_bank,
            salary_ask=self.salary_expectation,
        )
        if answer.needs_review or answer.overclaim_risk == "high":
            return None
        value = (answer.short_answer or answer.long_answer).strip()
        if not value:
            return None
        # Audited answers carry a structural confidence, not a self-report:
        # clean audit + no self-flagged stretch = 0.9; a "low" stretch = 0.75.
        confidence = 0.9 if answer.overclaim_risk == "none" else 0.75
        if confidence < self.config.llm_confidence_threshold:
            return None
        return Resolution(
            question=question,
            value=value,
            source=ResolutionSource.INFERRED,
            confidence=confidence,
            note=(
                f"copilot-audited (verdict={answer.verdict}, "
                f"risk={answer.overclaim_risk}); flag for §8e feedback loop"
            ),
        )

    # ---- review bail ----------------------------------------------------

    @staticmethod
    def _review(
        question: CustomQuestion,
        *,
        note: str,
        sensitivity: SensitiveClass = SensitiveClass.NONE,
    ) -> Resolution:
        return Resolution(
            question=question,
            value=None,
            source=ResolutionSource.REVIEW,
            confidence=0.0,
            sensitive=sensitivity,
            needs_review=True,
            note=note,
        )


# ---- helper used by the answer-bank seeder + the resolve-time learn loop ----

async def store_answer(
    answer_repo,
    embed_client: EmbeddingClient | None,
    question: str,
    answer: str,
    source: str = "user",
) -> None:
    """UPSERT an answer with its embedding (if a client is available).

    Used at:
      * onboarding seed of v2's flat answers.json (source='user')
      * resolver feedback loop for inferred answers the user later confirms (source='inferred')
    """
    embedding: bytes | None = None
    if embed_client is not None:
        try:
            vec = await embed_client.embed(question)
            embedding = vec_to_bytes(vec)
        except Exception:  # noqa: BLE001 — store without vector; resolver still hits via exact match
            embedding = None
    answer_repo.upsert(
        Answer(
            question=question,
            answer=answer,
            source=source,
            embedding=embedding,
            updated_at=utcnow_iso(),
        )
    )
