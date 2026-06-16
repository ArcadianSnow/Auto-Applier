"""Greenhouse hosted-form apply driver (spec §8, research/ats-form-automation.md).

100% of submits go through the browser (APIs can't submit, §6a). This drives the canonical
``job-boards.greenhouse.io/<token>/jobs/<id>`` form: classify the CAPTCHA, fill the standard
fields (stable element IDs), attach the résumé via the native file input, discover custom
questions at runtime by reading labels, then dispatch by mode (dev dry-run / production
assisted / production auto).

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

from auto_applier.domain.state import ApplicationStatus, ApplyMode
from auto_applier.sources.browser.apply_base import (
    Applicant,
    ApplyOutcome,
    CustomQuestion,
    any_drafted,
    any_required_unresolved,
    attach_cover_letter,
    check_auth_wall,
    fill_phone,
    fill_resolutions,
    human_type,
)
from auto_applier.sources.browser.detect import (
    ConfirmationOutcome,
    classify_captcha,
    detect_confirmation,
)
from auto_applier.sources.greenhouse import JobListing

__all__ = [
    "Applicant",
    "ApplyMode",
    "ApplyOutcome",
    "CustomQuestion",
    "discover_custom_questions",
    "prepare_application",
]

# Standard Greenhouse field selectors — stable across companies (research §Greenhouse).
_FIELD_SELECTORS = {
    "first_name": "#first_name",
    "last_name": "#last_name",
    "email": "#email",
    "phone": "#phone",
}
_RESUME_SELECTOR = "#resume"
#: Hidden file input parallel to #resume, behind the visible "Attach" button. Accepts
#: .pdf/.doc/.docx/.txt/.rtf; set_input_files works directly (confirmed live 2026-06-13).
_COVER_LETTER_SELECTOR = "#cover_letter"
_SUBMIT_SELECTOR = "button[type=submit]"


async def _collect_script_srcs(page) -> list[str]:
    return await page.eval_on_selector_all(
        "script[src]", "els => els.map(e => e.src)"
    )


async def discover_custom_questions(page) -> list[CustomQuestion]:
    """Walk Greenhouse custom-question controls and pair each with its visible label.

    The current ``job-boards.greenhouse.io`` React layout renders each question as a real
    control with ``id="question_<id>"`` (``<textarea>`` for essays, ``<input type=text>``
    for free text, and an ``<input role=combobox class="select__input">`` REACT-SELECT for
    dropdowns), wrapped by sibling ``question_<id>-label`` / ``-description`` elements. We
    must select the CONTROLS — selecting the ``-label``/``-description`` wrappers (the bug
    live 2026-06-13) makes the driver type answers into a non-input and nothing lands.
    So the selector is tag-qualified (input/textarea/select), which excludes the wrappers.
    react-select comboboxes are marked ``kind='input'`` so the fill path types-and-commits
    via ``settle_open_dropdown`` (native ``select_option`` doesn't work on react-select)."""
    raw = await page.evaluate(
        """
        () => {
          const out = [];
          // The standard fields the DRIVER fills directly (not via the resolver).
          const STD = new Set(['first_name','last_name','email','phone','resume']);
          // Skip non-question controls: standard fields, file/hidden/search/buttons, the
          // react-select hidden requiredInput carrier, the intl-tel-input search box, and
          // the reCAPTCHA textarea. Everything else WITH A LABEL is a real question — incl.
          // the semantic-id fields the old question_* selector missed (preferred_name,
          // country, candidate-location, gender, hispanic_ethnicity, veteran_status,
          // disability_status). (live 2026-06-13: those were all skipped → blank.)
          const skip = (el) => {
            const id = el.id || '', name = el.getAttribute('name') || '';
            const type = (el.getAttribute('type') || '').toLowerCase();
            const cls = (el.className || '').toString();
            if (STD.has(id) || STD.has(name)) return true;
            if (['hidden','file','search','submit','button','reset','image'].includes(type)) return true;
            if (/requiredInput|g-recaptcha|iti__|visually-hidden/.test(cls)) return true;
            if (id.startsWith('iti-')) return true;
            if (/recaptcha/i.test(id + ' ' + name)) return true;
            return false;
          };
          const labelFor = (el) => {
            let label = (el.getAttribute('aria-label') || '').trim();
            if (!label && el.id) { const l = document.getElementById(el.id + '-label'); if (l) label = (l.innerText||'').trim(); }
            if (!label && el.id) { const f = document.querySelector(`label[for='${el.id}']`); if (f) label = (f.innerText||'').trim(); }
            if (!label) { const w = el.closest('div,fieldset,li'); const l = w && w.querySelector('label'); if (l) label = (l.innerText||'').trim(); }
            return label;
          };
          document.querySelectorAll('input, textarea, select').forEach(el => {
            if (skip(el)) return;
            const id = el.id || el.getAttribute('name') || '';
            if (!id) return;
            const label = labelFor(el);
            if (!label) return;  // can't resolve a label-less field by intent
            const tag = el.tagName.toLowerCase();
            const isCombo = el.getAttribute('role') === 'combobox'
                         || (el.className || '').toString().includes('select__input');
            // react-select (combobox) is committed by opening + clicking an option, not by
            // typing or native select_option → its own 'combobox' kind (see fill_resolutions).
            const kind = tag === 'textarea' ? 'textarea'
                       : tag === 'select' ? 'select'
                       : isCombo ? 'combobox' : 'input';
            let options = [];
            if (tag === 'select') {
              options = Array.from(el.querySelectorAll('option'))
                .map(o => (o.textContent || '').trim()).filter(Boolean);
            }
            const lt = label.replace(/\\s+$/, '');
            const required = el.required || el.getAttribute('aria-required') === 'true'
                          || lt.endsWith('*');
            out.push({id, label, options, required, kind});
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
        qs.append(CustomQuestion(
            r["id"], r["label"], bool(r["required"]), r["kind"],
            options=list(r.get("options") or []),
        ))
    # Dedup by visible label as a belt-and-suspenders (an older layout rendered a combo
    # question as a visible input + a hidden value carrier sharing a label). Keep the FIRST
    # per label; empty labels are never deduped (we can't tell two label-less fields apart).
    seen_labels: set[str] = set()
    deduped: list[CustomQuestion] = []
    for q in qs:
        label_key = (q.label or "").strip().lower()
        if label_key:
            if label_key in seen_labels:
                continue
            seen_labels.add(label_key)
        deduped.append(q)
    return deduped


async def prepare_application(
    page,
    listing: JobListing,
    applicant: Applicant,
    resume_path: str,
    *,
    cover_letter_path: str = "",
    dry_run: bool = True,
    mode: ApplyMode = ApplyMode.BROWSER_AUTO,
    confirm_timeout_s: float = 20.0,
    resolver=None,
) -> ApplyOutcome:
    """Navigate, classify CAPTCHA, fill, attach résumé, discover + resolve custom
    questions, then dispatch by ``(dry_run, mode)``:

    * ``dry_run=True`` (default, dev-safe) → stop after fill; status=None; auto_eligible
      tells whether a real run *would* have tried to submit. Never sends an application.
    * ``dry_run=False, mode=BROWSER_ASSISTED`` → stop after fill; status=ASSISTED_PENDING;
      caller hands the open browser to the human, who reviews and clicks submit. The
      field-validated safe default.
    * ``dry_run=False, mode=BROWSER_AUTO`` → submit + confirm. A *visible* challenge always
      downgrades to ASSISTED_PENDING (never solved/retried — project invariant). APPLIED
      only on a positive confirmation signal.

    Resolver wiring (spec §8b): when ``resolver`` is supplied, each discovered question
    is run through it. Required questions that come back as REVIEW downgrade
    ``BROWSER_AUTO`` to ``ASSISTED_PENDING`` (we never submit a form with missing
    required answers). When omitted (Phase-1 tests), behavior is unchanged — discovery
    happens, no answers are typed.
    """
    await page.goto(listing.url, wait_until="domcontentloaded")
    await asyncio.sleep(random.uniform(1.0, 2.5))  # let scripts (recaptcha) load

    # Session-expiry check (spec §8b): if navigation kicked us to a login page,
    # pause the source in the health registry + fail fast to FAILED→REVIEW.
    # Other sources keep running; the user re-logs in when convenient.
    auth_signal = await check_auth_wall(page, "greenhouse")
    if auth_signal:
        outcome = ApplyOutcome(
            job_url=listing.url,
            captcha=classify_captcha("", []),  # synthetic NONE — we never saw the form
            mode=mode,
            status=ApplicationStatus.FAILED,
            note=f"auth required (greenhouse session expired): {auth_signal}",
        )
        return outcome

    html = await page.content()
    scripts = await _collect_script_srcs(page)
    captcha = classify_captcha(html, scripts)
    outcome = ApplyOutcome(job_url=listing.url, captcha=captcha, mode=mode)

    # Fill standard fields.
    outcome.filled["first_name"] = await human_type(page, _FIELD_SELECTORS["first_name"], applicant.first_name)
    outcome.filled["last_name"] = await human_type(page, _FIELD_SELECTORS["last_name"], applicant.last_name)
    outcome.filled["email"] = await human_type(page, _FIELD_SELECTORS["email"], applicant.email)
    if applicant.phone:
        # intl-tel-input: use its setNumber() API (correct in every dial-code mode) so the
        # country flag is set AND the dial code isn't doubled. See fill_phone.
        outcome.filled["phone"] = await fill_phone(page, _FIELD_SELECTORS["phone"], applicant.phone)

    # Attach résumé via the native file input.
    resume_el = await page.query_selector(_RESUME_SELECTOR)
    if resume_el is not None and resume_path:
        try:
            await resume_el.set_input_files(resume_path)
            outcome.filled["resume"] = True
        except Exception:  # noqa: BLE001 — mid-form break → fail fast to REVIEW
            outcome.filled["resume"] = False

    # Attach the cover letter (hidden #cover_letter file input, parallel to #resume).
    # Supplementary — a failure is observable, never fatal (see attach_cover_letter).
    if cover_letter_path:
        outcome.filled["cover_letter"] = await attach_cover_letter(
            page, _COVER_LETTER_SELECTOR, cover_letter_path
        )

    outcome.custom_questions = await discover_custom_questions(page)

    # Resolve + fill custom questions (spec §8b). Resolver is optional so existing
    # tests + the survey path don't change shape.
    if resolver is not None and outcome.custom_questions:
        outcome.resolutions = await resolver.resolve_all(outcome.custom_questions)
        custom_filled = await fill_resolutions(page, outcome.custom_questions, outcome.resolutions)
        for fid, ok in custom_filled.items():
            outcome.filled[f"q:{fid}"] = ok

    if dry_run:
        outcome.note = "dry-run: filled, not submitted (CAPTCHA presence surveyed, not pass-rate)"
        return outcome

    # --- production: branch by mode ---
    # If any REQUIRED custom question lacked a confident answer, downgrade BROWSER_AUTO
    # to assisted (a missing required answer would either fail validation or submit a
    # broken application). Optional unresolved questions are benign.
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

    # BROWSER_AUTO from here on.
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
