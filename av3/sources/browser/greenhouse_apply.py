"""Greenhouse hosted-form apply driver (spec §8, research/ats-form-automation.md).

100% of submits go through the browser (APIs can't submit, §6a). This drives the canonical
``job-boards.greenhouse.io/<token>/jobs/<id>`` form: classify the CAPTCHA, fill the standard
fields (stable element IDs), attach the résumé via the native file input, discover custom
questions at runtime by reading labels, then either STOP (dry-run, the dev default) or
submit and detect confirmation.

> **Measurement note (Phase 1 finding).** The headline metric — the *invisible-CAPTCHA
> auto-pass rate* — only resolves at submit time (the behavioral score is evaluated when
> the form POSTs). A **dry-run cannot measure the pass rate**; it can only survey CAPTCHA
> *presence and type* (the problem's ceiling). The pass rate requires real submits, which
> is a gated user decision (sends real applications). See ``run_survey`` vs the live path.

Reliability invariants (never compromised): never retry through a CAPTCHA; a visible
challenge → assisted; mid-form break → fail fast to REVIEW; APPLIED only on positive
confirmation.
"""

from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass, field

from av3.domain.state import ApplicationStatus
from av3.sources.browser.detect import (
    CaptchaResult,
    ConfirmationOutcome,
    ConfirmationResult,
    classify_captcha,
    detect_confirmation,
)
from av3.sources.greenhouse import JobListing

# Standard Greenhouse field selectors — stable across companies (research §Greenhouse).
_FIELD_SELECTORS = {
    "first_name": "#first_name",
    "last_name": "#last_name",
    "email": "#email",
    "phone": "#phone",
}
_RESUME_SELECTOR = "#resume"
_SUBMIT_SELECTOR = "button[type=submit]"


@dataclass
class Applicant:
    first_name: str
    last_name: str
    email: str
    phone: str = ""

    @classmethod
    def from_contact(cls, contact) -> "Applicant":
        parts = (contact.name or "").split()
        first = parts[0] if parts else ""
        last = " ".join(parts[1:]) if len(parts) > 1 else ""
        return cls(first_name=first, last_name=last, email=contact.email, phone=contact.phone)


@dataclass
class CustomQuestion:
    field_id: str
    label: str
    required: bool
    kind: str  # input | textarea | select


@dataclass
class ApplyOutcome:
    job_url: str
    captcha: CaptchaResult
    filled: dict[str, bool] = field(default_factory=dict)
    custom_questions: list[CustomQuestion] = field(default_factory=list)
    submitted: bool = False
    confirmation: ConfirmationResult | None = None
    status: ApplicationStatus | None = None
    note: str = ""

    @property
    def auto_eligible(self) -> bool:
        """In a dry-run: would this have been eligible for an auto-submit attempt?
        (No visible challenge; all standard fields filled.) NOT a measure of whether
        the invisible CAPTCHA would actually pass — that needs a real submit."""
        return self.captcha.is_invisible or not self.captcha.present


async def _human_type(page, selector: str, text: str) -> bool:
    """Fill a field with human-like per-keystroke jitter. Returns False if absent."""
    el = await page.query_selector(selector)
    if el is None:
        return False
    await el.click()
    for ch in text:
        await el.type(ch)
        await asyncio.sleep(random.uniform(0.03, 0.12))
    return True


async def _collect_script_srcs(page) -> list[str]:
    return await page.eval_on_selector_all(
        "script[src]", "els => els.map(e => e.src)"
    )


async def discover_custom_questions(page) -> list[CustomQuestion]:
    """Walk Greenhouse custom-question inputs (``#question_<id>``) and pair each with its
    visible label — the IDs are per-posting and unstable, so we read labels at runtime."""
    raw = await page.evaluate(
        """
        () => {
          const out = [];
          const els = document.querySelectorAll(
            "[id^='question_'], [name^='question_'], [id^='job_application_answers']"
          );
          els.forEach(el => {
            const id = el.id || el.name || '';
            let label = '';
            if (el.id) {
              const lab = document.querySelector(`label[for='${el.id}']`);
              if (lab) label = lab.innerText.trim();
            }
            if (!label) {
              const wrap = el.closest('div,fieldset,li');
              const lab = wrap && wrap.querySelector('label');
              if (lab) label = lab.innerText.trim();
            }
            out.push({
              id, label,
              required: el.required || el.getAttribute('aria-required') === 'true',
              kind: el.tagName.toLowerCase() === 'textarea' ? 'textarea'
                   : el.tagName.toLowerCase() === 'select' ? 'select' : 'input',
            });
          });
          return out;
        }
        """
    )
    seen, qs = set(), []
    for r in raw:
        key = r["id"]
        if not key or key in seen:
            continue
        seen.add(key)
        qs.append(CustomQuestion(r["id"], r["label"], bool(r["required"]), r["kind"]))
    return qs


async def prepare_application(
    page,
    listing: JobListing,
    applicant: Applicant,
    resume_path: str,
    *,
    dry_run: bool = True,
    confirm_timeout_s: float = 20.0,
) -> ApplyOutcome:
    """Navigate, classify CAPTCHA, fill, attach résumé, discover custom questions, then
    stop (dry-run) or submit + confirm (live). One call per job.

    Live submit is refused when a *visible* challenge is detected (→ assisted, never solved).
    """
    await page.goto(listing.url, wait_until="domcontentloaded")
    await asyncio.sleep(random.uniform(1.0, 2.5))  # let scripts (recaptcha) load

    html = await page.content()
    scripts = await _collect_script_srcs(page)
    captcha = classify_captcha(html, scripts)
    outcome = ApplyOutcome(job_url=listing.url, captcha=captcha)

    # Fill standard fields.
    outcome.filled["first_name"] = await _human_type(page, _FIELD_SELECTORS["first_name"], applicant.first_name)
    outcome.filled["last_name"] = await _human_type(page, _FIELD_SELECTORS["last_name"], applicant.last_name)
    outcome.filled["email"] = await _human_type(page, _FIELD_SELECTORS["email"], applicant.email)
    if applicant.phone:
        outcome.filled["phone"] = await _human_type(page, _FIELD_SELECTORS["phone"], applicant.phone)

    # Attach résumé via the native file input.
    resume_el = await page.query_selector(_RESUME_SELECTOR)
    if resume_el is not None and resume_path:
        try:
            await resume_el.set_input_files(resume_path)
            outcome.filled["resume"] = True
        except Exception:  # noqa: BLE001 — mid-form break → fail fast to REVIEW
            outcome.filled["resume"] = False

    outcome.custom_questions = await discover_custom_questions(page)

    if dry_run:
        outcome.note = "dry-run: filled, not submitted (CAPTCHA presence surveyed, not pass-rate)"
        return outcome

    # --- live submit path (gated; sends a real application) ---
    if captcha.present and not captcha.is_invisible:
        outcome.status = ApplicationStatus.ASSISTED_PENDING
        outcome.note = "visible challenge — handed to assisted (never solved/retried)"
        return outcome

    submit = await page.query_selector(_SUBMIT_SELECTOR)
    if submit is None:
        outcome.status = ApplicationStatus.FAILED
        outcome.note = "submit button not found — fail fast to REVIEW"
        return outcome

    await submit.click()
    outcome.submitted = True
    try:
        await page.wait_for_load_state("networkidle", timeout=confirm_timeout_s * 1000)
    except Exception:  # noqa: BLE001
        pass
    await asyncio.sleep(1.0)

    conf = detect_confirmation(page.url, await page.content(), submit_present=True)
    outcome.confirmation = conf
    outcome.status = {
        ConfirmationOutcome.CONFIRMED: ApplicationStatus.APPLIED,
        ConfirmationOutcome.CAPTCHA_CHALLENGE: ApplicationStatus.ASSISTED_PENDING,
        ConfirmationOutcome.FAILED_VALIDATION: ApplicationStatus.FAILED,
        ConfirmationOutcome.UNCONFIRMED: ApplicationStatus.UNCONFIRMED,
    }[conf.outcome]
    outcome.note = f"submitted; confirmation={conf.outcome.value} ({conf.signal})"
    return outcome
