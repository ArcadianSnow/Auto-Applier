"""Lever hosted-form apply driver (spec §8, research/ats-form-automation.md §Lever).

Lever is v3's PRIMARY auto-apply target (Phase-2 finding): invisible **hCaptcha** only
(zero reCAPTCHA Enterprise in our 16-form survey), server-rendered single page with a
real ``<form method="post">``, and the most selector-stable standard fields of the three
ATSes — name-keyed inputs (``input[name='name']``, ``[name='email']``, etc.) consistent
across all Lever companies.

Apply URL pattern: ``https://jobs.lever.co/<company>/<uuid>/apply`` (computed by
``LeverSource`` as ``apply_url``; falls back to ``{hostedUrl}/apply``).

Behavior identical to the Greenhouse driver (``greenhouse_apply.py``) — same
``(dry_run, mode)`` dispatch, same reliability invariants (never retry through CAPTCHA;
visible challenge → assisted; mid-form break → fail fast; APPLIED only on positive
confirmation, here ``/thanks``). Differences live in the per-ATS quirks below.

Lever-specific quirks (research §Lever):
  * Standard text fields are name-keyed, not id-keyed.
  * Single ``name`` field (not first/last) — we send ``applicant.full_name``.
  * Résumé upload triggers an **async parse-and-prefill** server-side (``resumeStorageId``
    is populated when parsing settles). We wait for it before reading custom-question
    state so we don't race against Lever's prefill writing into the same DOM.
  * Custom questions are "cards" keyed by UUID: ``textarea[name="cards[<uuid>][field0]"]``.
    UUIDs are per-posting and unstable — discover by walking, pair with <label> text.
  * Submit button: ``#btn-submit``.
  * Success URL: ``/thanks`` (handled by the shared ``detect_confirmation``).
"""

from __future__ import annotations

import asyncio
import random

from av3.domain.state import ApplicationStatus, ApplyMode
from av3.sources.browser.apply_base import (
    Applicant,
    ApplyOutcome,
    CustomQuestion,
    human_type,
)
from av3.sources.browser.detect import (
    ConfirmationOutcome,
    classify_captcha,
    detect_confirmation,
)
from av3.sources.lever import LeverListing

__all__ = [
    "Applicant",
    "ApplyMode",
    "ApplyOutcome",
    "CustomQuestion",
    "discover_custom_questions",
    "prepare_application",
]

# Standard Lever field selectors — stable across all Lever companies (research §Lever).
_FIELD_SELECTORS = {
    "name": "input[name='name']",
    "email": "input[name='email']",
    "phone": "input[name='phone']",
    "org": "input[name='org']",  # current company; optional
}
_RESUME_SELECTOR = "#resume-upload-input"
_RESUME_STORAGE_ID_SELECTOR = "input[name='resumeStorageId']"
_SUBMIT_SELECTOR = "#btn-submit"


async def _collect_script_srcs(page) -> list[str]:
    return await page.eval_on_selector_all(
        "script[src]", "els => els.map(e => e.src)"
    )


async def _wait_for_resume_parse(page, timeout_s: float = 8.0) -> bool:
    """Poll for ``resumeStorageId`` to be populated — Lever's async résumé parse signal.

    Lever's server-side parse writes a non-empty storage id into a hidden input; until then
    the form may be re-writing prefilled name/company fields. Returns True if we observed
    a value within ``timeout_s``, False on timeout (we still proceed — submit may still
    work; this is a best-effort race-avoidance).
    """
    waited = 0.0
    while waited < timeout_s:
        try:
            val = await page.evaluate(
                f"() => {{ const el = document.querySelector(\"{_RESUME_STORAGE_ID_SELECTOR}\");"
                f" return el ? el.value : null; }}"
            )
        except Exception:  # noqa: BLE001
            val = None
        if val:
            return True
        await asyncio.sleep(0.5)
        waited += 0.5
    return False


async def discover_custom_questions(page) -> list[CustomQuestion]:
    """Walk Lever custom-question inputs (``cards[<uuid>]`` family) and pair each with its
    visible label. UUIDs are per-posting and unstable, so we discover, never hard-code."""
    raw = await page.evaluate(
        """
        () => {
          const out = [];
          const els = document.querySelectorAll(
            "[name^='cards['], [name^='eeo['], select[name='pronouns'], input[name='pronouns']"
          );
          const seen = new Set();
          els.forEach(el => {
            const id = el.name || el.id || '';
            // baseTemplate is a hidden carrier, not a candidate question
            if (!id || id.endsWith('[baseTemplate]')) return;
            if (seen.has(id)) return;
            seen.add(id);
            let label = '';
            const wrap = el.closest('div,fieldset,li,section');
            if (wrap) {
              const lab = wrap.querySelector('label, .application-label, h4');
              if (lab) label = (lab.innerText || lab.textContent || '').trim();
            }
            if (!label && el.id) {
              const lab = document.querySelector(`label[for='${el.id}']`);
              if (lab) label = (lab.innerText || lab.textContent || '').trim();
            }
            const tag = el.tagName.toLowerCase();
            out.push({
              id, label,
              required: el.required || el.getAttribute('aria-required') === 'true',
              kind: tag === 'textarea' ? 'textarea' : tag === 'select' ? 'select' : 'input',
            });
          });
          return out;
        }
        """
    )
    out: list[CustomQuestion] = []
    seen: set[str] = set()
    for r in raw:
        key = r["id"]
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(CustomQuestion(r["id"], r["label"], bool(r["required"]), r["kind"]))
    return out


async def prepare_application(
    page,
    listing: LeverListing,
    applicant: Applicant,
    resume_path: str,
    *,
    dry_run: bool = True,
    mode: ApplyMode = ApplyMode.BROWSER_AUTO,
    confirm_timeout_s: float = 20.0,
) -> ApplyOutcome:
    """Navigate to the Lever ``/apply`` URL, classify the (h)CAPTCHA, fill standard fields,
    attach the résumé (waiting for the parse to settle), discover custom questions, then
    dispatch by ``(dry_run, mode)`` exactly like the Greenhouse driver. See module docstring
    for the dispatch semantics; identical to ``greenhouse_apply.prepare_application``.
    """
    await page.goto(listing.apply_url, wait_until="domcontentloaded")
    await asyncio.sleep(random.uniform(1.0, 2.5))  # let scripts (hcaptcha) load

    html = await page.content()
    scripts = await _collect_script_srcs(page)
    captcha = classify_captcha(html, scripts)
    outcome = ApplyOutcome(job_url=listing.apply_url, captcha=captcha, mode=mode)

    # Fill standard fields. Lever's single 'name' field takes the full name.
    outcome.filled["name"] = await human_type(page, _FIELD_SELECTORS["name"], applicant.full_name)
    outcome.filled["email"] = await human_type(page, _FIELD_SELECTORS["email"], applicant.email)
    if applicant.phone:
        outcome.filled["phone"] = await human_type(page, _FIELD_SELECTORS["phone"], applicant.phone)

    # Attach résumé via the native file input, then wait for the async parse to settle so
    # custom-Q discovery doesn't race Lever's prefill writes (research §Lever).
    resume_el = await page.query_selector(_RESUME_SELECTOR)
    if resume_el is not None and resume_path:
        try:
            await resume_el.set_input_files(resume_path)
            outcome.filled["resume"] = True
            outcome.filled["resume_parsed"] = await _wait_for_resume_parse(page)
        except Exception:  # noqa: BLE001 — mid-form break → fail fast to REVIEW
            outcome.filled["resume"] = False

    outcome.custom_questions = await discover_custom_questions(page)

    if dry_run:
        outcome.note = "dry-run: filled, not submitted (CAPTCHA presence surveyed, not pass-rate)"
        return outcome

    # --- production: branch by mode ---
    if mode is ApplyMode.BROWSER_ASSISTED:
        outcome.status = ApplicationStatus.ASSISTED_PENDING
        outcome.note = "assisted: pre-filled; human reviews and clicks submit"
        return outcome

    # BROWSER_AUTO from here on. Lever's hCaptcha is invisible-by-default; only escalates
    # on a poor behavioral score (visible challenge -> assisted, per project invariant).
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
