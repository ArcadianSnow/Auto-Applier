"""Ashby hosted-form apply driver (spec section 8, research/ats-form-automation.md §Ashby).

Ashby is a **React SPA** — the trickiest of the three to drive:

  * **No native `<form>` element.** Submit is an XHR (``api.ashbyhq.com/applicationForm.submit``)
    instead of a form POST. The submit button is still a ``<button type=submit>`` but lives
    outside a ``<form>``; we use that as the selector and rely on React's onClick handler.
  * **No URL transition on success.** The page renders an in-place "Application submitted"
    panel instead of redirecting; ``detect_confirmation`` already matches the success text
    via ``_SUCCESS_TEXT_RE``, so no per-ATS branch is needed there.
  * **Custom questions have raw-UUID names** (e.g. ``eeea6952-8ba0-...``). No human-readable
    prefix at all and per-form, not stable. Even "semi-standard" fields like phone can be
    UUID-named depending on form config — so we treat *only* name/email/resume as standard
    and let everything else flow through custom-Q discovery + the resolver.
  * **CAPTCHA: invisible reCAPTCHA** (behavioral-score). ``classify_captcha`` already
    handles it; the same invisible-vs-visible gate decides auto-vs-assisted.

Reliability invariants unchanged from the other ATSes: never retry through CAPTCHA;
visible challenge → assisted; mid-form break → fail fast; APPLIED only on positive
confirmation (here the in-place success panel matched by ``detect_confirmation``).

Dispatch is identical to Greenhouse/Lever — ``(dry_run, mode)`` decides whether to fill +
stop, hand to assisted, or auto-submit + confirm. See ``apply_base`` for the shared
required-question downgrade.
"""

from __future__ import annotations

import asyncio
import random

from auto_applier.domain.state import ApplicationStatus, ApplyMode
from auto_applier.sources.ashby import AshbyListing
from auto_applier.sources.browser.apply_base import (
    Applicant,
    ApplyOutcome,
    CustomQuestion,
    any_drafted,
    any_required_unresolved,
    check_auth_wall,
    fill_resolutions,
    human_type,
)
from auto_applier.sources.browser.detect import (
    ConfirmationOutcome,
    classify_captcha,
    detect_confirmation,
)

__all__ = [
    "Applicant",
    "ApplyMode",
    "ApplyOutcome",
    "CustomQuestion",
    "discover_custom_questions",
    "prepare_application",
]

# Stable system fields — same shape on every Ashby company (research §Ashby).
_FIELD_SELECTORS = {
    "name": "#_systemfield_name",
    "email": "#_systemfield_email",
}
_RESUME_SELECTOR = "#_systemfield_resume"
#: No native <form>, so we anchor on the button. Ashby renders one ``button[type=submit]``
#: on the apply form; if multiple exist on a future form variant, prefer the one with text
#: "Submit Application" (handled in driver code if needed).
_SUBMIT_SELECTOR = "button[type=submit]"
#: SPA render wait — until ``#_systemfield_name`` is in the DOM the React app hasn't
#: hydrated the form yet, and reading custom-Q state too early returns nothing.
_FORM_READY_SELECTOR = _FIELD_SELECTORS["name"]
_FORM_READY_TIMEOUT_MS = 8000


async def _collect_script_srcs(page) -> list[str]:
    return await page.eval_on_selector_all(
        "script[src]", "els => els.map(e => e.src)"
    )


async def _wait_for_form_ready(page) -> bool:
    """Block until the SPA has rendered the form, or give up after the timeout.

    Returns True if we observed the form-ready selector; False on timeout (we still
    proceed — the form may render late). Tolerant of FakePage stubs that don't implement
    ``wait_for_selector`` (returns True for them, so unit tests stay simple).
    """
    waiter = getattr(page, "wait_for_selector", None)
    if waiter is None:
        return True
    try:
        await waiter(_FORM_READY_SELECTOR, timeout=_FORM_READY_TIMEOUT_MS)
        return True
    except Exception:  # noqa: BLE001 — timeout / closed page → fall through gracefully
        return False


async def discover_custom_questions(page) -> list[CustomQuestion]:
    """Walk every form input/textarea/select that ISN'T a system field, pair with label.

    Ashby's custom questions use raw UUIDs (and sometimes even phone is UUID-named), so
    we discover by *exclusion* of the stable ``_systemfield_*`` ids — anything left is a
    custom question. CAPTCHA carriers (``g-recaptcha-response``, ``-response`` inputs) are
    skipped explicitly so they don't show up as "questions" with empty labels.
    """
    raw = await page.evaluate(
        """
        () => {
          const out = [];
          const els = document.querySelectorAll(
            "input:not([type=hidden]):not([type=file]), textarea, select"
          );
          const seen = new Set();
          els.forEach(el => {
            const id = el.id || el.name || '';
            if (!id) return;
            if (id.startsWith('_systemfield_')) return;
            if (id.includes('captcha') || id.endsWith('-response')) return;
            if (seen.has(id)) return;
            seen.add(id);
            let label = '';
            if (el.id) {
              const lab = document.querySelector(`label[for='${el.id}']`);
              if (lab) label = (lab.innerText || lab.textContent || '').trim();
            }
            if (!label) {
              const wrap = el.closest('div,fieldset,section');
              if (wrap) {
                const lab = wrap.querySelector(
                  "label, h4, .ashby-application-form-question-title"
                );
                if (lab) label = (lab.innerText || lab.textContent || '').trim();
              }
            }
            const tag = el.tagName.toLowerCase();
            out.push({
              id, label,
              required: el.required || el.getAttribute('aria-required') === 'true',
              kind: tag === 'textarea' ? 'textarea'
                   : tag === 'select' ? 'select' : 'input',
            });
          });
          return out;
        }
        """
    )
    seen: set[str] = set()
    out: list[CustomQuestion] = []
    for r in raw:
        key = r["id"]
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(CustomQuestion(r["id"], r["label"], bool(r["required"]), r["kind"]))
    return out


async def prepare_application(
    page,
    listing: AshbyListing,
    applicant: Applicant,
    resume_path: str,
    *,
    cover_letter_path: str = "",
    dry_run: bool = True,
    mode: ApplyMode = ApplyMode.BROWSER_AUTO,
    confirm_timeout_s: float = 20.0,
    resolver=None,
) -> ApplyOutcome:
    """Drive the Ashby SPA apply form, identical dispatch to the other ATSes.

    SPA-specific quirks layered in:
      1. Wait for the form to hydrate (``_wait_for_form_ready``) before reading state.
      2. Fill only the three stable system fields (name/email/resume). Everything else —
         phone included, since it can be UUID-named — comes through custom-Q discovery +
         the resolver.
      3. Submit is a non-form ``<button type=submit>``; we click it and let React's
         onClick handler issue the XHR.
      4. Confirmation: no URL transition. ``detect_confirmation`` matches the in-place
         "Application submitted" panel via the success-text regex.

    ``cover_letter_path`` is accepted for a uniform worker call but NOT wired: Ashby's
    cover-letter field varies per form config (often a custom UUID-named file question that
    flows through custom-Q discovery, not a stable system field), so it must be scoped on a
    live form before wiring (research/automated-apply-next-build.md). Greenhouse is wired today.
    """
    await page.goto(listing.apply_url, wait_until="domcontentloaded")
    # SPA needs render + a moment for invisible reCAPTCHA to attach.
    await _wait_for_form_ready(page)
    await asyncio.sleep(random.uniform(1.0, 2.5))

    # Session-expiry check (spec §8b): pause this source and fail fast if we
    # landed on a login page. Run AFTER _wait_for_form_ready so the SPA actually
    # rendered something - otherwise the check sees an empty document and
    # always concludes "no wall" even when there's one about to render.
    auth_signal = await check_auth_wall(page, "ashby")
    if auth_signal:
        outcome = ApplyOutcome(
            job_url=listing.apply_url,
            captcha=classify_captcha("", []),  # synthetic NONE - we never saw the form
            mode=mode,
            status=ApplicationStatus.FAILED,
            note=f"auth required (ashby session expired): {auth_signal}",
        )
        return outcome

    html = await page.content()
    scripts = await _collect_script_srcs(page)
    captcha = classify_captcha(html, scripts)
    outcome = ApplyOutcome(job_url=listing.apply_url, captcha=captcha, mode=mode)

    # Standard fields. Ashby's single legal-name field takes the full name (matches Lever).
    outcome.filled["name"] = await human_type(page, _FIELD_SELECTORS["name"], applicant.full_name)
    outcome.filled["email"] = await human_type(page, _FIELD_SELECTORS["email"], applicant.email)

    # Résumé via the native file input. There can be TWO file inputs on the page (a
    # generic one + the system résumé) — target the system one specifically.
    resume_el = await page.query_selector(_RESUME_SELECTOR)
    if resume_el is not None and resume_path:
        try:
            await resume_el.set_input_files(resume_path)
            outcome.filled["resume"] = True
        except Exception:  # noqa: BLE001 — mid-form break → fail fast to REVIEW
            outcome.filled["resume"] = False

    outcome.custom_questions = await discover_custom_questions(page)

    # Resolve + fill custom questions (spec §8b). Same shape as Lever/GH.
    if resolver is not None and outcome.custom_questions:
        outcome.resolutions = await resolver.resolve_all(outcome.custom_questions)
        custom_filled = await fill_resolutions(page, outcome.custom_questions, outcome.resolutions)
        for fid, ok in custom_filled.items():
            outcome.filled[f"q:{fid}"] = ok

    if dry_run:
        outcome.note = "dry-run: filled, not submitted (CAPTCHA presence surveyed, not pass-rate)"
        return outcome

    # --- production: branch by mode ---
    if mode is ApplyMode.BROWSER_AUTO and (
        any_required_unresolved(outcome.custom_questions, outcome.resolutions)
        or any_drafted(outcome.resolutions)
    ):
        outcome.status = ApplicationStatus.ASSISTED_PENDING
        outcome.note = (
            "required custom question unresolved or freeform draft pre-filled — "
            "downgraded to assisted (spec §8b)"
        )
        return outcome

    if mode is ApplyMode.BROWSER_ASSISTED:
        outcome.status = ApplicationStatus.ASSISTED_PENDING
        outcome.note = "assisted: pre-filled; human reviews and clicks submit"
        return outcome

    # BROWSER_AUTO from here.
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
    # SPA: the XHR fires after the click. networkidle is the right wait — no URL change
    # to gate on. detect_confirmation reads the post-submit DOM for the success panel.
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
