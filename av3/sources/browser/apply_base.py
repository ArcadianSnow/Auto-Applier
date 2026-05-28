"""Shared primitives for ATS apply drivers (spec §8, research/ats-form-automation.md).

Greenhouse and Lever (and Ashby once its SPA driver is built) share the same shape:
classify CAPTCHA -> fill standard fields -> attach resume -> discover custom questions ->
**resolve answers (§8b)** -> branch by mode (dev dry-run / production assisted /
production auto). Per-ATS selectors, URL patterns, and parse-quirks live in the per-ATS
module; everything else lives here so adding a new ATS only adds selectors + one driver
function, not a copy of the dataclasses.

Why ``dry_run`` AND ``mode`` (not one knob):
  * ``dry_run`` is the dev-safe default for tests + manual smoketests — it never submits
    regardless of mode and never claims an APPLIED state. Keeps Phase 1 tests green.
  * ``mode`` distinguishes the two PRODUCTION postures the spec defines:
      - BROWSER_AUTO: bot fills and submits on a clean form (gated by invisible CAPTCHA
        passing and no validation error; visible challenge -> downgrade to assisted).
      - BROWSER_ASSISTED: bot fills, status=ASSISTED_PENDING, human clicks submit. The
        field-validated safe default (neonwatty / Simplify / LazyApply all stop here).
    Mode is only consulted when ``dry_run=False``.
"""

from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass, field

from av3.domain.state import ApplicationStatus, ApplyMode
from av3.sources.browser.detect import CaptchaResult, ConfirmationResult

__all__ = [
    "Applicant",
    "ApplyMode",
    "ApplyOutcome",
    "CustomQuestion",
    "any_required_unresolved",
    "fill_resolutions",
    "human_type",
]


@dataclass
class Applicant:
    """The bare-minimum identity fields every ATS asks for.

    Greenhouse takes first/last separately; Lever takes a single ``name``; Ashby asks for
    "legal name" (single). The driver picks whichever shape its form needs from these
    fields — keep this dataclass small and ATS-neutral.
    """

    first_name: str
    last_name: str
    email: str
    phone: str = ""

    @property
    def full_name(self) -> str:
        return f"{self.first_name} {self.last_name}".strip()

    @classmethod
    def from_contact(cls, contact) -> "Applicant":
        parts = (contact.name or "").split()
        first = parts[0] if parts else ""
        last = " ".join(parts[1:]) if len(parts) > 1 else ""
        return cls(first_name=first, last_name=last, email=contact.email, phone=contact.phone)


@dataclass
class CustomQuestion:
    """One employer-defined question discovered on the form at runtime.

    Identifiers are NOT stable across postings (research/ats-form-automation.md): GH uses
    ``#question_<numeric>``, Lever uses ``cards[<uuid>][field0]``, Ashby uses raw UUIDs. We
    always pair each input with its visible <label> text and let the resolver answer by
    intent, not selector.
    """

    field_id: str
    label: str
    required: bool
    kind: str  # "input" | "textarea" | "select"


@dataclass
class ApplyOutcome:
    """Outcome of one apply attempt — observable result, not a side-effect."""

    job_url: str
    captcha: CaptchaResult
    mode: ApplyMode = ApplyMode.BROWSER_AUTO
    filled: dict[str, bool] = field(default_factory=dict)
    custom_questions: list[CustomQuestion] = field(default_factory=list)
    #: Per-question resolutions in the same order as ``custom_questions``. Empty when no
    #: resolver was passed in (Phase-1 dry-runs / tests that only exercise the form
    #: skeleton). Carries the source (bank / inferred / sensitive / review) so the
    #: §8e feedback loop and §9 telemetry policy can see *why* each question resolved
    #: the way it did.
    resolutions: list = field(default_factory=list)
    submitted: bool = False
    confirmation: ConfirmationResult | None = None
    status: ApplicationStatus | None = None
    note: str = ""

    @property
    def auto_eligible(self) -> bool:
        """In a dry-run: would this have been eligible for an auto-submit attempt?

        No visible challenge AND we got far enough to read the form. This is NOT a claim
        that the invisible CAPTCHA would have passed — that needs a real submit. Used by
        the survey to estimate the auto-pass *ceiling*, not the actual rate.
        """
        return self.captcha.is_invisible or not self.captcha.present


async def human_type(page, selector: str, text: str) -> bool:
    """Fill a field with human-paced per-keystroke jitter (research §anti-detect).

    Returns False if the field is absent (caller decides whether absence is a hard fail
    or an optional-field skip). Click-then-type is intentional: focusing via click matches
    real user behavior better than direct .fill() and avoids the focus-related fingerprint.
    """
    el = await page.query_selector(selector)
    if el is None:
        return False
    await el.click()
    for ch in text:
        await el.type(ch)
        await asyncio.sleep(random.uniform(0.03, 0.12))
    return True


# --- resolver wiring (shared across ATSes) --------------------------------------

def _selector_for(question: CustomQuestion) -> str:
    """Build the most-portable selector for a discovered question.

    GH uses ``[name='job_application_answers[...]']`` and ``#question_<id>`` shapes;
    Lever uses ``[name=\"cards[<uuid>][field0]\"]``. The discovered ``field_id`` is the
    element's ``name`` (preferred — survives DOM reflows that move the wrapper) falling
    back to ``id``. We try ``[name='<id>']`` first, then ``#<id>`` — the per-ATS module
    can override via ``selector_for_question`` if a quirk demands it.
    """
    fid = (question.field_id or "").strip()
    if not fid:
        return ""
    # Heuristic: name-keyed (most ATSes use brackets in name) vs. id-keyed.
    if "[" in fid or "]" in fid:
        return f"[name='{fid}']"
    return f"#{fid}"


async def fill_resolutions(
    page,
    questions: list[CustomQuestion],
    resolutions: list,
    *,
    selector_for=None,
) -> dict[str, bool]:
    """Type/select each resolved answer onto its field. Returns ``{field_id: filled?}``.

    Skips:
      * any resolution with ``value is None`` / ``needs_review`` (driver will downgrade
        to assisted if it was required — see :func:`any_required_unresolved`).
      * any selector that doesn't resolve to an element on the page (mid-form break →
        caller decides; we just report False so the outcome is observable).
    """
    selector_for = selector_for or _selector_for
    filled: dict[str, bool] = {}
    for q, r in zip(questions, resolutions):
        if not getattr(r, "fills", False):
            filled[q.field_id] = False
            continue
        sel = selector_for(q)
        if not sel:
            filled[q.field_id] = False
            continue
        if q.kind == "select":
            el = await page.query_selector(sel)
            if el is None:
                filled[q.field_id] = False
                continue
            try:
                await page.select_option(sel, str(r.value))
                filled[q.field_id] = True
            except Exception:  # noqa: BLE001 — mid-form break -> fail closed
                filled[q.field_id] = False
        else:
            # Both <input> and <textarea> take typed text. Same human_type for both
            # keeps the behavioral signal uniform across ATSes.
            ok = await human_type(page, sel, str(r.value))
            filled[q.field_id] = ok
    return filled


def any_required_unresolved(questions: list[CustomQuestion], resolutions: list) -> bool:
    """True iff a REQUIRED question came back as REVIEW (no confident answer).

    The driver uses this to downgrade ``BROWSER_AUTO`` to ``ASSISTED_PENDING`` —
    auto-submitting a form with a missing required answer would either fail validation
    (FAILED) or, worse, submit a partial/garbled application. Optional REVIEWs are
    benign; we just don't fill them.
    """
    for q, r in zip(questions, resolutions):
        if q.required and getattr(r, "needs_review", False):
            return True
    return False
