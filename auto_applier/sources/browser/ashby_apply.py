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
import re

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
    "fill_ashby_combobox",
    "prepare_application",
]

# Synthetic id discovery assigns id/name-less widgets (``ashby_q<n>``), where <n> is the
# 1-based position in ``.ashby-application-form-field-entry`` — see discover_custom_questions.
_SYNTHETIC_ID_RE = re.compile(r"^ashby_q(\d+)$")

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
    """Discover Ashby questions CONTAINER-FIRST, then sweep leftovers.

    Ashby is a React SPA: most questions live in a ``.ashby-application-form-field-entry``
    whose visible text is a ``.ashby-application-form-question-title``. The audit
    (2026-06-22) found the old "walk every input by id" approach failed Ashby two ways:
      * its dropdowns/date-pickers render an ``<input>`` with NO id/name → skipped entirely
        (Location combobox, "When can you start?" date), and
      * its Yes/No questions render a hidden empty-label checkbox + ``<button>Yes/No</button>``
        → discovered with an EMPTY label → the resolver couldn't classify them and bailed,
        even though work-auth / sponsorship are exactly what it answers from the fact bank.

    So PASS 1 anchors on the field-entry: the TITLE is the label (reliable), and the widget
    shape decides ``kind`` — ``radio`` for a Yes/No ``<button>`` group (options = the button
    texts; the filler clicks the matching button), ``combobox`` for a react-select, ``select``
    for a native dropdown, else ``input``/``textarea``. id-less widgets get a synthetic
    ``ashby_q<n>`` id so a required-but-unfillable one still routes the job to assisted rather
    than silently vanishing. PASS 2 sweeps any named input NOT already captured — standalone
    text fields + the consent/certification checkbox (kept only when its label reads as a
    consent gate, so a multi-select question's per-option checkboxes don't each become a
    bogus 'question'). Options carry through for the §8d attestation option-pair check."""
    raw = await page.evaluate(
        r"""
        () => {
          const out = [];
          const captured = new Set();
          const CONSENTISH = /\b(i\s+(confirm|agree|consent|certify|acknowledge|understand|have\s+read)|privacy\s+(policy|notice|statement)|terms\b|i\s+hereby)\b/i;
          const labelFallback = (el) => {
            let t = (el.getAttribute('aria-label') || '').trim();
            if (!t && el.id) { try { const f = document.querySelector(`label[for='${CSS.escape(el.id)}']`); if (f) t = (f.innerText || '').trim(); } catch(e) {} }
            if (!t) { const w = el.closest('div,fieldset,section'); const l = w && w.querySelector('label, .ashby-application-form-question-title, h4'); if (l) t = (l.innerText || '').trim(); }
            return (t || '').replace(/\s+/g,' ').trim();
          };
          // PASS 1 — one question per .ashby-application-form-field-entry.
          let idx = 0;
          document.querySelectorAll('.ashby-application-form-field-entry').forEach(entry => {
            idx++;
            const titleEl = entry.querySelector('.ashby-application-form-question-title');
            const label = titleEl ? (titleEl.innerText || titleEl.textContent || '').replace(/\s+/g,' ').trim() : '';
            if (!label) return;
            const inputs = [...entry.querySelectorAll('input,textarea,select')].filter(el => {
              const t = (el.getAttribute('type') || '').toLowerCase();
              if (t === 'hidden' || t === 'file') return false;
              const id = el.id || el.name || '';
              if (id.startsWith('_systemfield_')) return false;
              if (id.includes('captcha') || id.endsWith('-response')) return false;
              return true;
            });
            const buttons = [...entry.querySelectorAll('button')]
              .map(b => (b.innerText || '').replace(/\s+/g,' ').trim())
              .filter(b => b && b.length < 60 && !/^(upload|add|\+|delete|remove|browse|choose file)/i.test(b));
            const combo = entry.querySelector('input[role=combobox], [role=combobox]');
            const nativeSelect = entry.querySelector('select');
            const textarea = entry.querySelector('textarea');
            const named = inputs.find(el => el.id || el.name);
            let id = '', kind = 'input', options = [];
            if (textarea) { kind = 'textarea'; id = textarea.id || textarea.name || ('ashby_q'+idx); }
            else if (nativeSelect) { kind = 'select'; id = nativeSelect.id || nativeSelect.name || ('ashby_q'+idx);
              options = [...nativeSelect.querySelectorAll('option')].map(o => (o.textContent || '').trim()).filter(Boolean); }
            else if (buttons.length >= 2 || (named && (named.type === 'checkbox' || named.type === 'radio') && buttons.length)) {
              kind = 'radio'; options = buttons; id = (named && (named.name || named.id)) || ('ashby_q'+idx); }
            else if (combo) { kind = 'combobox'; id = combo.id || combo.name || ('ashby_q'+idx); }
            else if (named) { kind = 'input'; id = named.name || named.id; }
            else if (inputs.length) { kind = 'input'; id = inputs[0].id || inputs[0].name || ('ashby_q'+idx); }
            else return;
            inputs.forEach(el => { if (el.id) captured.add(el.id); if (el.name) captured.add(el.name); });
            const required = inputs.some(el => el.required || el.getAttribute('aria-required') === 'true')
                          || /[*✱]\s*$/.test(label);
            out.push({id, label, options, required, kind});
          });
          // PASS 2 — leftover named inputs outside any field-entry (consent checkbox, strays).
          document.querySelectorAll("input:not([type=hidden]):not([type=file]), textarea, select").forEach(el => {
            const id = el.id || el.name || '';
            if (!id || captured.has(el.id) || captured.has(el.name)) return;
            if (id.startsWith('_systemfield_') || id.includes('captcha') || id.endsWith('-response')) return;
            const t = (el.getAttribute('type') || '').toLowerCase();
            const tag = el.tagName.toLowerCase();
            const isChoice = t === 'checkbox' || t === 'radio';
            const label = labelFallback(el);
            if (!label) return;
            // A bare option checkbox (multi-select question) is NOT its own question — only keep
            // a stray checkbox that reads as a consent/agreement gate (→ honesty bail downstream).
            if (isChoice && !CONSENTISH.test(label)) return;
            captured.add(id);
            out.push({
              id, label, options: [],
              required: !!(el.required || el.getAttribute('aria-required') === 'true'),
              kind: tag === 'textarea' ? 'textarea' : tag === 'select' ? 'select' : isChoice ? 'radio' : 'input',
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
        out.append(CustomQuestion(
            r["id"], r["label"], bool(r["required"]), r["kind"],
            options=list(r.get("options") or []),
        ))
    return out


# Ashby combobox option DOM (live-probed on a Ramp form 2026-06-24). Ashby's id/name-less
# ``input[role=combobox]`` is a geocoder-style autocomplete: it stays ``aria-expanded=false``
# until you TYPE, then opens a portaled ``[role=listbox]`` (``_floatingContainer_*``) of
# ``[role=option]`` (``_result_*``) place suggestions, the first auto-highlighted (``_active_``).
# The react-select ``fill_combobox`` can't drive it (wrong DOM; the synthetic ``ashby_q<n>`` id
# selects nothing), so Ashby gets this container-anchored filler. The matcher clicks the option
# best overlapping the wanted value but ONLY when the leading (city) token is present — a missing
# match returns False so a required combobox routes the job to assisted rather than a wrong place.
_ASHBY_COMBO_PICK_JS = r"""
(want) => {
  const norm = s => (s || '').replace(/\s+/g,' ').trim().toLowerCase();
  const w = norm(want);
  if (!w) return false;
  const tokens = w.split(/[,\/]+/).map(t => t.trim()).filter(Boolean);
  const city = tokens[0] || w;
  const opts = [...document.querySelectorAll('[role=option]')]
    .map(el => ({el, t: norm(el.innerText || el.textContent)})).filter(o => o.t);
  if (!opts.length) return false;
  const click = el => { try { el.click(); return true; } catch(e) { return false; } };
  for (const o of opts) if (o.t === w) return click(o.el);          // 1. exact
  let best = null, bestScore = 0;                                   // 2. best overlap incl. city
  for (const o of opts) {
    if (!o.t.includes(city)) continue;
    let score = 0;
    for (const tk of tokens) if (tk && o.t.includes(tk)) score++;
    if (score > bestScore) { bestScore = score; best = o; }
  }
  if (best) return click(best.el);
  return false;                                                     // 3. city absent -> don't guess
}
"""


async def _locate_ashby_combobox(page, question, selector: str):
    """Return the ElementHandle for ``question``'s combobox input, or None.

    Ashby's React comboboxes render an ``<input>`` with NEITHER id nor name, so discovery gives
    them a synthetic ``ashby_q<n>`` id keyed to the field-entry position. We re-derive the entry by
    that index and read the ``input[role=combobox]`` inside it. A combobox that DID carry a real
    id/name falls back to the passed selector."""
    fid = (getattr(question, "field_id", "") or "").strip()
    m = _SYNTHETIC_ID_RE.match(fid)
    if m:
        try:
            entries = await page.query_selector_all(".ashby-application-form-field-entry")
        except Exception:  # noqa: BLE001
            return None
        i = int(m.group(1)) - 1
        if 0 <= i < len(entries):
            return await entries[i].query_selector("input[role=combobox], [role=combobox]")
        return None
    if selector:
        el = await page.query_selector(selector)
        if el is not None:
            return el
    if fid:
        return await page.query_selector(f"[name='{fid}']")
    return None


async def fill_ashby_combobox(page, question, selector: str, value: str) -> bool:
    """Fill an Ashby geocoder combobox by typing a query and clicking the matching suggestion.

    Anchors on the field-entry (the synthetic ``ashby_q<n>`` id selects nothing), TYPES the
    leading token of ``value`` to open the autocomplete (the menu is empty until you type), waits
    for the ``[role=option]`` list, then clicks the best match (city token required). Returns True
    only when an option is committed; on no-match it Escapes the menu (so it can't intercept later
    fields) and returns False → a required field then routes the job to assisted, never a guess.
    Fully defensive: any Playwright error is an observable False (mid-form-break policy)."""
    want = (value or "").strip()
    if not want:
        return False
    try:
        el = await _locate_ashby_combobox(page, question, selector)
        if el is None:
            return False
        try:
            await el.scroll_into_view_if_needed(timeout=3000)
        except Exception:  # noqa: BLE001
            pass
        try:
            await el.click(timeout=8000)
        except Exception:  # noqa: BLE001 — intercepted/unstable field -> observable skip
            return False
        # Clear any prefill, then type the leading token (a full address over-filters the geocoder).
        try:
            await el.fill("")
        except Exception:  # noqa: BLE001
            pass
        query = (re.split(r"[\s,;:./]+", want)[0] or want)[:40]
        for ch in query:
            await el.type(ch)
            await asyncio.sleep(random.uniform(0.03, 0.10))
        # Wait for the autocomplete list to render (portaled; empty until typing settles).
        for _ in range(20):
            if await page.query_selector("[role=option]") is not None:
                break
            await asyncio.sleep(0.1)
        if await page.evaluate(_ASHBY_COMBO_PICK_JS, want):
            await asyncio.sleep(0.2)   # let the menu close before the next field
            return True
        try:
            await page.keyboard.press("Escape")  # dismiss so it can't block later fields
        except Exception:  # noqa: BLE001
            pass
        return False
    except Exception:  # noqa: BLE001 — mid-form break -> observable skip, never fatal
        return False


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
        custom_filled = await fill_resolutions(
            page, outcome.custom_questions, outcome.resolutions,
            combobox_fill=fill_ashby_combobox)
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
