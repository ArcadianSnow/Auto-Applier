"""Canonical taxonomy of form-field label fragments and predicates.

Three call sites in the codebase need to decide "is this label real, or is
it page chrome / compliance boilerplate / a leaked prompt body?". Before
this module they each maintained their own near-duplicate fragment list,
which drifted over time:

  * ``selector_utils._is_phantom_label`` — used by the form-field
    detector (``find_form_fields`` -> ``_classify_element``) to drop
    page-chrome labels at *detection* time so the LLM is never asked to
    answer a heading or a "Drag and drop" affordance.
  * ``selector_utils.should_skip_unanswered`` — used by
    ``_record_unanswered`` (and the rule-based LLM backend) to keep
    junk out of ``unanswered.json`` (the wizard's teach-it-this-one
    queue).
  * ``form_filler._is_skill_shaped`` — used by ``_record_gap`` (and
    ``storage.integrity``) to keep compliance/eligibility/personal-info
    questions out of ``skill_gaps.csv``, which feeds ``cli refine`` and
    ``cli trends``.

The lists overlapped heavily. Drift between them produced real bugs:
labels were phantom-filtered at form-fill time but still recorded as a
"skill gap", or rejected from the unanswered queue while being accepted
as a skill. Consolidating here keeps all three call sites in lock-step.

Taxonomy is split by SHAPE (what the label looks like), not by use-site
(who consumes it). Each call site composes the predicates it needs:

  * :func:`is_phantom_label` — page chrome, never a real form question.
    Used at form-field detection (rejected for everyone) and as one of
    the unanswered-queue rejection rules.
  * :func:`is_non_skill_label` — phantom OR compliance OR personal-info
    OR salary OR comms/source. Real questions are allowed through; only
    "skill / experience" - shaped questions feed the gap pipeline.
  * :func:`is_prompt_leak` — substring markers that only appear in LLM
    prompt bodies, never in real form-field labels. Defensive cap so a
    misbehaving upstream caller can't pollute ``unanswered.json``.

Behaviour MUST stay byte-for-byte identical to the pre-consolidation
selector_utils + form_filler implementations; the consuming call sites
import the public names below and nothing else.
"""

from __future__ import annotations

import re


# ---------------------------------------------------------------------------
# 1. Phantom labels — page chrome, never a real form question.
# ---------------------------------------------------------------------------
# Substring-matched (after lowercasing). Includes section headings,
# upload affordance text, and accessibility helper text we should never
# try to LLM-answer. The first three ("voluntary self identification"
# variants) were called out from a dry-run audit; the rest were observed
# in run logs as "filled" with junk answers.
PHANTOM_LABEL_PATTERNS: tuple[str, ...] = (
    "voluntary self identification",
    "voluntary self-identification",
    "self-identification questions",
    "self identification questions",
    "upload a file",
    "drag and drop",
    "drag & drop",
    "drop your file here",
    "current page",
    "page navigation",
    "powered by",
    "click to upload",
    "browse files",
    "choose file",
    "no file chosen",
    "accepted formats",
    "max size",
    "maximum file size",
)


# ---------------------------------------------------------------------------
# 2. Compliance / eligibility / EEO — real questions but never "skills".
# ---------------------------------------------------------------------------
# These ARE legitimate form fields (the user must answer them to submit).
# They route through ``answers.json`` / personal_info / smart_fallback,
# never the skill-gap -> resume-bullet pipeline.
COMPLIANCE_LABEL_FRAGMENTS: tuple[str, ...] = (
    # EEO / demographics
    "race", "ethnicity", "gender", "sex",
    "veteran", "military",
    "disability", "disabled",
    "pronoun",
    "lgbt",
    "sexual orientation",
    "marital status",
    # Age compliance
    "18 years of age",
    "at least 18",
    "minimum age",
    # Authorization / sponsorship — handled by work_auth + answers.json
    "work authorization",
    "authorized to work",
    "require sponsorship", "visa sponsorship",
    "visa",
    "us citizen", "u.s. citizen",
    "permanent resident",
    # Education credentialing — degree-checks aren't skills
    "bachelor's degree", "bachelors degree",
    "master's degree", "masters degree",
    "ph.d", "phd",
    "diploma",
    # Employment-status compliance — not a skill
    "employed with", "currently employed",
    # Compliance yes/no boilerplate
    "background check",
    "drug test",
    "criminal", "felony", "convicted",
    "non-compete", "ndas", "non-disclosure",
    "debarred", "excluded",
    "physical requirements",
    "certify that the information",
)


# ---------------------------------------------------------------------------
# 3. Personal info / addressing — handled by personal_info, not gaps.
# ---------------------------------------------------------------------------
PERSONAL_INFO_LABEL_FRAGMENTS: tuple[str, ...] = (
    "street address", "mailing address", "home address",
    "city ", "city,", "city*", " city",
    "zip code", "postal code",
    "phone number", "mobile number",
)


# ---------------------------------------------------------------------------
# 4. Salary / compensation — handled by smart_fallback or personal_info.
# ---------------------------------------------------------------------------
SALARY_LABEL_FRAGMENTS: tuple[str, ...] = (
    "desired salary", "expected salary",
    "salary expectation",
    "hourly rate",
)


# ---------------------------------------------------------------------------
# 5. Communications opt-in / source attribution.
# ---------------------------------------------------------------------------
# Text-message / email opt-ins (handled elsewhere) and "how did you hear
# about this?" referral-source questions (resolved at run time by
# form_filler via SOURCE_QUESTION_KEYWORDS using the platform display
# name; not gaps the user can usefully fill in advance).
COMMS_AND_SOURCE_FRAGMENTS: tuple[str, ...] = (
    "text messages", "sms",
    "receive emails", "email updates",
    "how did you hear",
    "where did you hear",
    "referred by",
)


# ---------------------------------------------------------------------------
# 6. EEO heading variants used ONLY by the non-skill predicate.
# ---------------------------------------------------------------------------
# The phantom list already rejects the full-phrase heading variants
# ("voluntary self identification questions"). The non-skill predicate
# also needs to reject the bare stems ("voluntary self identif" /
# "self-identification") because those slip past PHANTOM via labels
# that lack the "questions" suffix yet still aren't skill-shaped.
_NON_SKILL_EEO_STEM_FRAGMENTS: tuple[str, ...] = (
    "voluntary self identif",
    "self-identification",
)


# ---------------------------------------------------------------------------
# 7. User-rejected source-attribution variants used ONLY by the
#    unanswered-queue gate. Stricter than COMMS_AND_SOURCE_FRAGMENTS:
#    these are referral / "how did you find" stems we don't even want
#    written to unanswered.json (form_filler handles them at run time).
# ---------------------------------------------------------------------------
USER_REJECTED_FRAGMENTS: tuple[str, ...] = (
    "referred by an employee",
    "referred by a current",
    "employee referral",
    "how did you hear about",
    "where did you hear about",
    "how did you find this",
    "where did you find this",
    "referral source",
)


# ---------------------------------------------------------------------------
# 8. Prompt-leak markers — only appear in LLM prompt bodies.
# ---------------------------------------------------------------------------
# Live audit 2026-05-02 found a 1900-char "question" in unanswered.json
# containing "Resume:" + the candidate's full resume + "Job description:"
# + part of a JD; an upstream caller had passed the LLM prompt instead
# of the field label to ``_record_unanswered``. Defensive cap at the
# storage layer so the bug can't pollute data even if a future caller
# mis-passes again.
PROMPT_LEAK_MARKERS: tuple[str, ...] = (
    "resume:\n", "resume:\r",
    "job description:\n", "job description:\r",
    "candidate profile:",
    "applicant resume:",
    "you are answering",
    "you are filling",
    "respond only with",
    "respond ONLY with",
    "json shape",
    "available options",
    "system_prompt",
    "candidate's resume",
)


# ---------------------------------------------------------------------------
# 9. Non-skill prefixes — anchored to the start of the (lowered, stripped)
#    label. Substring matching is too greedy here ("do you" appears in
#    every "Do you have experience with X?" skill question).
# ---------------------------------------------------------------------------
NON_SKILL_LABEL_PREFIXES: tuple[str, ...] = (
    "are you currently",
    "do you have a ",
)


# ---------------------------------------------------------------------------
# Composite tuple used by ``is_non_skill_label`` — order-agnostic
# (substring scan), but kept in the same order as the original
# ``_NON_SKILL_LABEL_FRAGMENTS`` tuple in form_filler.py for grep-ability.
# ---------------------------------------------------------------------------
_NON_SKILL_LABEL_FRAGMENTS: tuple[str, ...] = (
    *_NON_SKILL_EEO_STEM_FRAGMENTS,
    # EEO / demographics
    "race", "ethnicity", "gender", "sex",
    "veteran", "military",
    "disability", "disabled",
    "pronoun",
    "lgbt",
    "sexual orientation",
    "marital status",
    # Age compliance
    "18 years of age",
    "at least 18",
    "minimum age",
    # Address / contact (handled by personal_info)
    *PERSONAL_INFO_LABEL_FRAGMENTS,
    # Authorization / sponsorship — handled by work_auth + answers.json
    "work authorization",
    "authorized to work",
    "require sponsorship", "visa sponsorship",
    "visa",
    "us citizen", "u.s. citizen",
    "permanent resident",
    # Education credentialing — degree-checks aren't skills
    "bachelor's degree", "bachelors degree",
    "master's degree", "masters degree",
    "ph.d", "phd",
    "diploma",
    # Employment-status compliance — not a skill
    "employed with", "currently employed",
    # Salary / compensation — handled by smart_fallback
    *SALARY_LABEL_FRAGMENTS,
    # Compliance yes/no boilerplate
    "background check",
    "drug test",
    "criminal", "felony", "convicted",
    "non-compete", "ndas", "non-disclosure",
    "debarred", "excluded",
    "physical requirements",
    "certify that the information",
    # Communications opt-in
    "text messages", "sms",
    "receive emails", "email updates",
    # Source attribution
    "how did you hear",
    "where did you hear",
    "referred by",
)


# ---------------------------------------------------------------------------
# Predicates.
# ---------------------------------------------------------------------------

def is_phantom_label(label: str) -> bool:
    """Return True for labels that look like form chrome, not a question.

    Conservative — only patterns we've seen produce nonsense answers.
    Real questions occasionally contain phrases like ``upload`` (e.g.
    "Upload your resume — required"), so we only block when the label
    is *dominated* by chrome text. The two-pronged rule:

      1. The label substring-matches a phantom pattern, AND
      2. The label is short (<= 80 chars) — long labels usually wrap a
         real question around any upload-flavoured noise.

    Empty / whitespace-only / pure-punctuation labels are also phantom.
    """
    if not label:
        return True
    s = label.strip().lower()
    if not s:
        return True
    if len(s) > 80:
        return False
    # Pure punctuation / digits-only stays out of the form filler.
    if not re.search(r"[a-z]", s):
        return True
    for pat in PHANTOM_LABEL_PATTERNS:
        if pat in s:
            return True
    return False


def is_non_skill_label(label: str) -> bool:
    """Return True if ``label`` is a real question but not a skill question.

    Used by the skill-gap pipeline: EEO / demographic / compliance /
    personal-info / salary / comms / source-attribution questions are
    genuine fields the user must answer, but they should never surface
    in ``cli refine`` / ``cli trends`` as "skills you should add to
    your resume".

    Negative signal beats positive signal: anything matching the prefix
    or fragment lists below is not a skill, no matter what other words
    appear. Mirrors the pre-consolidation FormFiller._is_skill_shaped
    semantics — returns ``True`` when the original returned ``False``.
    """
    lower = label.lower()
    stripped = lower.lstrip()
    for prefix in NON_SKILL_LABEL_PREFIXES:
        if stripped.startswith(prefix):
            return True
    for frag in _NON_SKILL_LABEL_FRAGMENTS:
        if frag in lower:
            return True
    return False


def is_prompt_leak(label: str) -> bool:
    """Return True if ``label`` contains an LLM-prompt-body marker.

    Substring scan against :data:`PROMPT_LEAK_MARKERS` against the
    lower-cased label. NOTE: markers in :data:`PROMPT_LEAK_MARKERS`
    that contain upper-case characters (e.g. ``"respond ONLY with"``)
    are by design unreachable — they mirror the original tuple verbatim
    so behaviour is identical to the pre-consolidation implementation.
    Don't "fix" them by lowering the needle; leave the casing alone.
    """
    if not label:
        return False
    lowered = label.lower()
    for marker in PROMPT_LEAK_MARKERS:
        if marker in lowered:
            return True
    return False


__all__ = [
    "PHANTOM_LABEL_PATTERNS",
    "COMPLIANCE_LABEL_FRAGMENTS",
    "PERSONAL_INFO_LABEL_FRAGMENTS",
    "SALARY_LABEL_FRAGMENTS",
    "COMMS_AND_SOURCE_FRAGMENTS",
    "USER_REJECTED_FRAGMENTS",
    "PROMPT_LEAK_MARKERS",
    "NON_SKILL_LABEL_PREFIXES",
    "is_phantom_label",
    "is_non_skill_label",
    "is_prompt_leak",
]
