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
import re
from dataclasses import dataclass, field

from auto_applier.domain.state import ApplicationStatus, ApplyMode
from auto_applier.sources.browser.detect import (
    CaptchaResult,
    ConfirmationResult,
    detect_login_wall,
)
from auto_applier.sources.health import mark_auth_required

__all__ = [
    "Applicant",
    "ApplyMode",
    "ApplyOutcome",
    "CustomQuestion",
    "any_drafted",
    "any_required_unresolved",
    "attach_cover_letter",
    "check_auth_wall",
    "fill_option_group",
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

    ``options`` carries the visible choice text for select/radio questions when the
    driver can scrape them (native ``<select>`` always; react-select comboboxes only
    once their menu is open, so it may be empty). It exists for ONE safety-critical
    reason: the human-attestation gate ("Which of the following best describes you?"
    → "I am an AI…" / "I am a human being") has a non-descriptive label, so it can only
    be recognised from its option PAIR. The resolver's ``classify_sensitive`` reads it.
    """

    field_id: str
    label: str
    required: bool
    kind: str  # "input" | "textarea" | "select"
    options: list[str] = field(default_factory=list)


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

    Returns False if the field is absent OR not clickable within the bounded
    timeout (caller decides whether that's a hard fail or an optional-field skip).
    Click-then-type is intentional: focusing via click matches real user behavior
    better than direct .fill() and avoids the focus-related fingerprint.

    The click is BOUNDED (8s, not Playwright's 30s default) and a timeout returns
    False instead of raising: observed live (2026-06-11), an open react-select
    menu intercepted pointer events over every later field — an unbounded click
    burned 30s per field and surfaced as a job-level error instead of an
    observable per-field skip.
    """
    el = await page.query_selector(selector)
    if el is None:
        return False
    try:
        await el.click(timeout=8000)
    except Exception:  # noqa: BLE001 — intercepted/unstable field -> observable skip
        return False
    for ch in text:
        await el.type(ch)
        await asyncio.sleep(random.uniform(0.03, 0.12))
    return True


async def attach_cover_letter(page, selector: str, path: str) -> bool:
    """Attach a cover-letter file to a native file input, defensively.

    Greenhouse renders a hidden ``input#cover_letter`` (``class="visually-hidden"``,
    ``accept=".pdf,.doc,.docx,.txt,.rtf"``) parallel to ``#resume`` and behind the visible
    "Attach" button — ``set_input_files`` works on it DIRECTLY, no click needed (same shape
    as the résumé upload; confirmed live on Hightouch 2026-06-13). A ``.docx`` cover letter
    is accepted as-is, so no PDF render is required.

    Returns True iff a file was attached. A missing input or a failed upload returns False
    (observable, never fatal): the cover letter is supplementary, so a failure must not break
    the apply — the worker still records ``filled["cover_letter"]=False`` and proceeds."""
    if not path:
        return False
    el = await page.query_selector(selector)
    if el is None:
        return False
    try:
        await el.set_input_files(path)
        return True
    except Exception:  # noqa: BLE001 — supplementary upload; failure is observable, not fatal
        return False


# Value-side safety backstop for the human-attestation gate (research/automated-apply-
# go-live.md, blocker A). The resolver should already bail any attestation question to
# REVIEW, but react-select gates with non-descriptive labels are slippery — so as a last
# line of defense the FILLER refuses to ever type/select a value that affirms being human.
# If this fires, classification missed a gate: the field is left unfilled (→ required-
# field validation will route the job to assisted/REVIEW on a real run, never a false
# attestation). A bot must never claim to be a human.
_AFFIRMS_HUMAN = re.compile(
    r"\b(i am (a )?human|i'm (a )?human|human being|a real (person|human)|"
    r"i am not (a )?(ro)?bot|not an? (automated|ai))\b",
    re.IGNORECASE,
)


def affirms_human(value: str) -> bool:
    """True iff ``value`` asserts the filler is a human (an attestation a bot must not make)."""
    return bool(_AFFIRMS_HUMAN.search(value or ""))


def normalize_phone(raw: str, *, default_cc: str = "1") -> str:
    """Normalize a phone to ``+<digits>`` (E.164-ish) for ATS tel fields.

    Greenhouse/Lever/Ashby render phone via **intl-tel-input**, and on the current
    Greenhouse layout the default country flag is the GLOBE — i.e. NO country selected
    (live 2026-06-12). Typing a national number like ``1-682-718-8130`` then leaves the
    widget without a dial code, producing an invalid/ambiguous submission. The reliable
    fix is to type the number with a leading ``+``: intl-tel-input auto-detects the
    country from the dial-code prefix and sets the flag, so we never have to drive its
    (search-dialog) country dropdown.

    Rules: a leading ``+`` is preserved (already international); a bare 10-digit number
    assumes the default country code (US ``1``); 11+ digits are assumed to already lead
    with a country code (``1-682-…`` → ``+16827188130``). Empty → ``""``. The US default
    matches the single-user audience ([[project_us_default_assumption]]); widen when the
    audience does.
    """
    s = (raw or "").strip()
    if not s:
        return ""
    has_plus = s.startswith("+")
    digits = re.sub(r"\D", "", s)
    if not digits:
        return ""
    if has_plus:
        return "+" + digits
    if len(digits) == 10:                # bare national number → assume US
        return "+" + default_cc + digits
    return "+" + digits                  # already carries a country code (e.g. 1-682-…)


# intl-tel-input exposes its instance via window.intlTelInput.getInstance (v18+) or
# window.intlTelInputGlobals.getInstance (v17). Its setNumber() sets the input value AND
# the country flag correctly in EVERY dial-code display mode — including separateDialCode /
# showSelectedDialCode, where typing a "+1…" string duplicates the prefix (live 2026-06-12,
# user saw "+1" both in the country selector and the number field). Falls back to typing.
_ITI_SET_NUMBER_JS = """
([sel, num]) => {
  const el = document.querySelector(sel);
  if (!el) return false;
  const get = (window.intlTelInput && window.intlTelInput.getInstance)
           || (window.intlTelInputGlobals && window.intlTelInputGlobals.getInstance);
  const inst = get ? get(el) : null;
  if (inst && typeof inst.setNumber === 'function') {
    inst.setNumber(num);
    el.dispatchEvent(new Event('input', {bubbles:true}));
    el.dispatchEvent(new Event('change', {bubbles:true}));
    el.dispatchEvent(new Event('blur', {bubbles:true}));
    return true;
  }
  return false;
}
"""


# Detect the intl-tel-input dial-code display mode + the selected dial code. In
# separate-dial-code / show-selected-dial-code mode the dial code is rendered OUTSIDE the
# text input, so the input must hold ONLY the national number — typing the +CC there doubles
# it (the "+1 … +1" the user saw). In the default inline mode the +CC belongs in the input.
_ITI_MODE_JS = """
(sel) => {
  const el = document.querySelector(sel);
  if (!el) return {mode: '', dial: ''};
  const wrap = el.closest('.iti');
  const cls = wrap ? wrap.className : '';
  const sep = /iti--separate-dial-code|iti--show-selected-dial-code/.test(cls);
  const dialEl = wrap ? wrap.querySelector('.iti__selected-dial-code') : null;
  const dial = dialEl ? (dialEl.textContent || '').trim() : '';
  return {mode: sep ? 'separate' : (wrap ? 'inline' : ''), dial};
}
"""


def _national_part(e164: str, dial: str) -> str:
    """Strip the dial code from a +E.164 number → national significant digits. Falls back
    to dropping a leading '1' (NANP) when the dial code isn't known."""
    digits = re.sub(r"\D", "", e164)
    dd = re.sub(r"\D", "", dial or "")
    if dd and digits.startswith(dd):
        return digits[len(dd):]
    if not dd and len(digits) == 11 and digits.startswith("1"):
        return digits[1:]
    return digits


async def fill_phone(page, selector: str, raw_phone: str) -> bool:
    """Fill an ATS phone field robustly across intl-tel-input dial-code modes.

    Order: (1) try the iti ``setNumber()`` API if the instance is exposed on ``window``
    (correct in every mode); (2) else read the widget's mode — in separate-dial-code mode
    type the NATIONAL number only (the dial code is shown separately), otherwise type the
    full ``+E.164`` so the ``+`` prefix selects the country (default flag is the globe).
    Returns whether anything was filled."""
    number = normalize_phone(raw_phone)
    if not number:
        return False
    try:
        if await page.evaluate(_ITI_SET_NUMBER_JS, [selector, number]):
            return True
    except Exception:  # noqa: BLE001 — instance not reachable; fall through to typing
        pass
    mode, dial = "inline", ""
    try:
        info = await page.evaluate(_ITI_MODE_JS, selector)
        if isinstance(info, dict):
            mode, dial = info.get("mode", "inline"), info.get("dial", "")
    except Exception:  # noqa: BLE001
        pass
    to_type = _national_part(number, dial) if mode == "separate" else number
    return await human_type(page, selector, to_type)


# React-select (the new job-boards.greenhouse.io layout renders dropdown questions
# as comboboxes, not <select>): typing opens a floating menu that stays open and
# intercepts pointer events over later fields until committed or dismissed.
_REACT_SELECT_MENU = ".select__menu"
_REACT_SELECT_OPTION = ".select__option"


# Decline-to-answer synonyms — the resolver yields "Prefer not to answer" for EEO, but the
# react-select option text varies ("Decline To Self Identify", "I don't wish to answer", …).
# EEO "decline" option text varies a lot: "Decline To Self Identify", "I don't wish to
# answer", "I do not want to answer", "Prefer not to say/disclose". Match: 'decline',
# 'prefer not', or any negation co-occurring with answer/say/disclose/identify/share (covers
# want/wish/choose-not-to). Disability's "I do not WANT to answer" was missed before.
#
# CONTRACTION FIX (live Tailscale 2026-06-13): the negation alternative MUST catch
# contractions ("don't", "won't", "doesn't"). The old `\b(?:not|n't|never)\b` never matched
# inside "don't" — there's no word boundary between "do" and "n't" — so the veteran option
# "I don't wish to answer" slipped through, the decline-only branch found no matching option,
# and veteran_status deterministically bailed (it only *looked* like an intermittent flake
# because the veteran decline wording differs per form). `n['’]t` (no leading \b) fixes it.
_DECLINE_SYNONYMS = re.compile(
    r"\bdecline\b|\bprefer not\b|"
    r"(?:\bnot\b|\bnever\b|n['’]t)[^.]{0,20}?\b(?:answer|say|disclose|identify|specify|share)\b",
    re.IGNORECASE,
)


def _word_in(needle: str, hay: str) -> bool:
    """Whole-word/phrase containment — ``no`` matches "no" but NOT "not"/"now" (the bug
    that committed 'No' for a 'prefer not to answer' value: 'no' is a substring of 'not')."""
    if not needle:
        return False
    return re.search(r"\b" + re.escape(needle) + r"\b", hay) is not None


async def _click_combobox_option(page, want: str) -> bool:
    """Click the react-select option matching ``want``, by PRIORITY (not loose substring):
      1. exact text match;
      2. if ``want`` is a decline/prefer-not value → a decline-synonym option ONLY (never
         fall through to a fuzzy match — that's how 'No' wrongly won for Hispanic/Latino);
      3. whole-word/phrase containment either way ('Dallas' in 'Dallas, Texas, US').
    Returns True on commit."""
    w = (want or "").strip().lower()
    if not w:
        return False
    opts = []
    for opt in await page.query_selector_all(_REACT_SELECT_OPTION):
        t = ((await opt.text_content()) or "").strip().lower()
        if t:
            opts.append((opt, t))

    async def _click(opt) -> bool:
        try:
            await opt.click(timeout=3000)
            await asyncio.sleep(0.2)   # let the menu fully close before the next field
            return True
        except Exception:  # noqa: BLE001
            return False

    for opt, t in opts:                       # 1. exact
        if t == w:
            return await _click(opt)
    if _DECLINE_SYNONYMS.search(w):           # 2. decline → decline option only
        for opt, t in opts:
            if _DECLINE_SYNONYMS.search(t):
                return await _click(opt)
        return False
    for opt, t in opts:                       # 3. whole-word containment
        if _word_in(t, w) or _word_in(w, t):
            return await _click(opt)
    return False


async def _open_menu(page, el) -> bool:
    """Click to open a react-select and wait for its menu to actually render (up to ~1.5s).
    Returns True once ``.select__menu`` is present — guards against matching a not-yet-open
    or still-closing menu (the last-field intercept that left Disability blank)."""
    try:
        # Below-the-fold comboboxes (the EEO block at the form bottom) flaked because the
        # click landed before the element scrolled into view — scroll first.
        await el.scroll_into_view_if_needed(timeout=3000)
    except Exception:  # noqa: BLE001
        pass
    try:
        await el.click(timeout=8000)
    except Exception:  # noqa: BLE001
        return False
    for _ in range(15):
        if await page.query_selector(_REACT_SELECT_MENU) is not None:
            return True
        await asyncio.sleep(0.1)
    return False


async def fill_combobox(page, selector: str, value: str) -> bool:
    """Fill a react-select combobox by OPENING it and clicking the matching option — the
    only reliable way (typing a prose value filters the menu to empty, and native
    ``select_option`` doesn't apply). Handles Yes/No, country, EEO prefer-not synonyms, and
    city/autocomplete (menu empty until you type). Waits for the menu and retries once (the
    last combobox on a form intermittently opened against a closing menu). Returns True only
    when an option is actually committed."""
    want = (value or "").strip()
    if not want:
        return False
    # Pre-clear: a previous combobox's menu may still be animating closed and would
    # intercept this open (intermittently left one EEO field blank). Escape + settle so
    # each field starts from a clean state.
    try:
        await page.keyboard.press("Escape")
        await asyncio.sleep(0.25)
    except Exception:  # noqa: BLE001
        pass
    for attempt in range(3):
        el = await page.query_selector(selector)
        if el is None:
            return False
        if not await _open_menu(page, el):
            await asyncio.sleep(0.35)
            continue
        await asyncio.sleep(0.2)
        # 1) Options already shown (Yes/No, country list, EEO choices) → click the match.
        if await _click_combobox_option(page, want):
            return True
        # 2) Empty/long menu (city autocomplete) → type a short query to load/filter, retry.
        query = (re.split(r"[\s,;:./]+", want)[0] or want)[:25]
        try:
            for ch in query:
                await el.type(ch)
                await asyncio.sleep(random.uniform(0.03, 0.09))
            await asyncio.sleep(0.5)
        except Exception:  # noqa: BLE001
            pass
        if await _click_combobox_option(page, want):
            return True
        try:
            await page.keyboard.press("Escape")  # dismiss so it can't block later fields
        except Exception:  # noqa: BLE001
            pass
        await asyncio.sleep(0.2)
    return False


# Option-group selection (Lever checkbox/radio cards + Ashby Yes/No <button> groups). The
# ATS field-coverage audit (2026-06-22) found these questions DISCOVER + RESOLVE fine (the
# fact bank knows "Yes" for work-auth, "No" for sponsorship) but never LANDED — fill_resolutions
# only typed text / opened react-selects. A radio/checkbox group is selected by CLICKING the
# option whose visible text matches; this one JS does it for both ATSes by anchoring on the
# field's container. The match is deliberately conservative: exact text, else a single
# unambiguous whole-word match — a bare "No" against several "No - I already…" options finds
# >1 and bails (returns false → the required field routes the job to assisted, never a guess).
_OPTION_GROUP_CLICK_JS = r"""
([fid, want]) => {
  const norm = s => (s || '').replace(/\s+/g,' ').trim().toLowerCase();
  const w = norm(want);
  if (!w) return false;
  // Locate the question container from any element carrying this name/id.
  let anchor = null;
  try { anchor = document.querySelector(`[name='${fid.replace(/'/g, "\\'")}']`); } catch(e) {}
  if (!anchor) { try { anchor = document.getElementById(fid); } catch(e) {} }
  const container = anchor
    ? anchor.closest('.application-question, .ashby-application-form-field-entry, fieldset, [role=radiogroup], [role=group]')
    : null;
  const scope = container || document;
  const cands = [];
  scope.querySelectorAll('button, label, [role=radio], [role=button]').forEach(el => {
    const t = norm(el.innerText || el.textContent);
    if (t) cands.push({el, t});
  });
  scope.querySelectorAll("input[type=radio], input[type=checkbox]").forEach(inp => {
    let t = norm(inp.getAttribute('aria-label'));
    if (!t) { const wl = inp.closest('label'); if (wl) t = norm(wl.innerText); }
    if (!t && inp.id) { try { const f = document.querySelector(`label[for='${CSS.escape(inp.id)}']`); if (f) t = norm(f.innerText); } catch(e) {} }
    if (t) cands.push({el: inp, t});
  });
  const click = (el) => { try { el.click(); return true; } catch(e) { return false; } };
  for (const c of cands) if (c.t === w) return click(c.el);   // 1. exact
  const esc = w.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  const re = new RegExp('\\b' + esc + '\\b');
  const hits = cands.filter(c => re.test(c.t));
  if (hits.length === 1) return click(hits[0].el);            // 2. one unambiguous whole-word hit
  return false;                                               // ambiguous / no match -> assisted
}
"""


async def fill_option_group(page, question, value: str) -> bool:
    """Select ``value`` in a radio/checkbox/button-group question (Lever cards, Ashby Yes/No).

    Anchors on the question's container via ``field_id`` and clicks the matching option;
    returns True only when an option is actually clicked. Conservative matching (exact, else a
    single whole-word hit) means a bare "No" against several "No - …" options bails (False), so
    a required field with no confident option routes the job to assisted rather than guessing.
    Defensive: any Playwright error is an observable False, never fatal (mid-form break policy)."""
    want = (value or "").strip()
    if not want:
        return False
    try:
        return bool(await page.evaluate(_OPTION_GROUP_CLICK_JS, [question.field_id, want]))
    except Exception:  # noqa: BLE001 — mid-form break -> observable skip
        return False


async def settle_open_dropdown(page, value: str) -> bool:
    """Commit or dismiss a combo-box menu left open by typing (react-select).

    Tries to click the menu option matching ``value`` (case-insensitive,
    substring either way) — which COMMITS the selection properly; otherwise
    presses Escape so the menu can't block later fields. Fully defensive: any
    failure is a no-op (returns False) — this is cleanup, never a new failure
    mode. Returns True only when an option was actually committed.
    """
    try:
        menu = await page.query_selector(_REACT_SELECT_MENU)
        if menu is None:
            return False
        want = (value or "").strip().lower()
        if want:
            for opt in await page.query_selector_all(_REACT_SELECT_OPTION):
                text = ((await opt.text_content()) or "").strip().lower()
                if text and (want == text or want in text or text in want):
                    await opt.click(timeout=3000)
                    return True
        await page.keyboard.press("Escape")
    except Exception:  # noqa: BLE001 — cleanup must never raise
        pass
    return False


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
    combobox_fill=None,
) -> dict[str, bool]:
    """Type/select each resolved answer onto its field. Returns ``{field_id: filled?}``.

    Skips:
      * any resolution with ``value is None`` / ``needs_review`` (driver will downgrade
        to assisted if it was required — see :func:`any_required_unresolved`).
      * any selector that doesn't resolve to an element on the page (mid-form break →
        caller decides; we just report False so the outcome is observable).

    ``combobox_fill`` overrides how ``kind=='combobox'`` fields are filled. The default is the
    react-select ``fill_combobox`` (Greenhouse). Ashby's id-less geocoder combobox needs a
    container-anchored filler that the synthetic-id selector can't reach, so its driver passes
    ``combobox_fill=fill_ashby_combobox`` (signature ``(page, question, selector, value)``).
    """
    selector_for = selector_for or _selector_for
    filled: dict[str, bool] = {}
    for q, r in zip(questions, resolutions):
        if not getattr(r, "fills", False):
            filled[q.field_id] = False
            continue
        # Last-line safety: never type/select a human-affirmation UNLESS it's the deliberate,
        # owner-opted-in attestation fill (a HUMAN_ATTESTATION resolution with a real value —
        # settings.attest_human). Any OTHER human-affirming value here means classification
        # missed a gate or an LLM misfired → leave it unfilled rather than falsely attest
        # (blocker A). The deliberate path is identified by its sensitive class, so a stray
        # "I am a human" from some other field is still blocked.
        if affirms_human(str(getattr(r, "value", "") or "")):
            _sens = getattr(getattr(r, "sensitive", None), "value", "")
            _deliberate = _sens == "human_attestation" and not getattr(r, "needs_review", True)
            if not _deliberate:
                filled[q.field_id] = False
                continue
        sel = selector_for(q)
        if not sel:
            filled[q.field_id] = False
            continue
        if q.kind == "radio":
            # Radio/checkbox/button option groups (Lever cards, Ashby Yes/No buttons): click
            # the option matching the resolved value within the question's container. The
            # selector_for() name/id won't drive these (a shared-name checkbox pair, or a hidden
            # carrier behind <button>s), so we anchor on field_id inside the helper instead.
            filled[q.field_id] = await fill_option_group(page, q, str(r.value))
        elif q.kind == "combobox":
            # react-select: open + click the matching option (typing prose filters to empty).
            # A per-ATS override drives non-react-select comboboxes (Ashby's id-less geocoder).
            if combobox_fill is not None:
                filled[q.field_id] = await combobox_fill(page, q, sel, str(r.value))
            else:
                filled[q.field_id] = await fill_combobox(page, sel, str(r.value))
        elif q.kind == "select":
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
            # Combo-box cleanup: if the typing opened a react-select menu (the new
            # GH layout), commit the matching option or dismiss it — an open menu
            # intercepts pointer events over every later field (live 2026-06-11).
            committed = await settle_open_dropdown(page, str(r.value))
            filled[q.field_id] = ok or committed
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


def any_drafted(resolutions: list) -> bool:
    """True iff any resolution is a freeform DRAFT (BUILD 6 Phase B).

    A drafted essay is pre-filled but UNVETTED, so the driver force-downgrades the job to
    assisted even when every *required* field resolved and the draft sat on an *optional*
    field — the bot must never auto-submit an AI-written essay (the §8b invariant). Without
    this, an optional drafted field would slip past :func:`any_required_unresolved` and the
    form could auto-submit with the draft in it.
    """
    return any(getattr(r, "draft", False) for r in resolutions)


async def check_auth_wall(page, source: str) -> str:
    """Did navigation land us on a login page? (spec §8b session expiry)

    Returns the auth-wall signal string when detected (non-empty truthy),
    empty string otherwise. On detect it ALSO marks the source AUTH_REQUIRED
    in the process-level health registry, which (a) pauses the source in the
    apply worker's per-job loop until the user re-logs in, and (b) emits a
    ``session_expiry`` event to the spine for the dashboard's "login needed"
    badge.

    Drivers call this *after* ``page.goto(apply_url, ...)`` and *before* trying
    to fill anything: if we're at a login form, filling apply fields would
    type into the wrong inputs and almost certainly fail the submit anyway.

    Defensive: a Playwright error during ``page.url`` / ``page.content()``
    propagates to the caller (the apply worker catches it as a per-job
    exception and routes to FAILED → REVIEW — the existing isolation path).
    We don't swallow it here because hiding navigation errors masks real
    driver bugs.
    """
    url = page.url or ""
    html = await page.content()
    result = detect_login_wall(url, html)
    if result.present:
        # Phase 4 (4/M): carry the URL through so the dashboard's "Log in"
        # button can drop the user back at exactly the page they need to
        # sign into. Empty when the navigation lost the URL — UI degrades
        # to a manual "Mark logged in" button.
        mark_auth_required(
            source,
            reason=f"login wall detected at {url}",
            login_url=url,
        )
        return result.signal
    return ""
