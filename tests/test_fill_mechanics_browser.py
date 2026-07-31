"""Fill mechanics executed in a REAL browser against LOCAL DOM fixtures (no network).

`pytest -m browser` — deselected by default (needs Chromium; `av3 install-browser`).

## Why this tier exists

The fake-page tests elsewhere stub `page.evaluate`, so they never execute the fill or read-back
JavaScript — they only assert that Python called it. That is exactly how the Round 3 defects
stayed invisible while their unit tests passed:

  * `fill_option_group` widened its search to the whole `document` when a `field_id` matched
    nothing, so one question's answer clicked ANOTHER question's option — and reported success;
  * `human_type` CLICKS to focus, so typing "No" into a checkbox CHECKED it;
  * `settle_open_dropdown` committed "Nope, never used it" for the value "No".

Live `smoke` tests would catch these, but they need the network and a real posting, so they
can't gate a commit. This tier is the middle: the real JS, a real engine, zero network.

The fixtures below are not invented — each mirrors a DOM contract captured read-only from a
live form (Greenhouse/Monzo, Ashby/Vanta, 2026-07-31). See
`.claude/skills/auto-applier/research/fill-mechanics-hardening.md`.

Nothing here ever loads a real ATS page or submits anything.
"""

from __future__ import annotations

import pytest
import pytest_asyncio

from auto_applier.resume.answer_resolver import Resolution, ResolutionSource
from auto_applier.sources.browser.apply_base import (
    CustomQuestion,
    any_required_unfilled,
    fill_option_group,
    fill_resolutions,
    read_back_fills,
    settle_open_dropdown,
    verify_fills,
)

pytestmark = [pytest.mark.browser, pytest.mark.asyncio]

Q_AUTH = "Are you legally authorized to work in the location where this role is based?"
Q_SPON = "Will you now or in the future require visa sponsorship?"
Q_LOC = "Location"


@pytest_asyncio.fixture
async def page():
    """A blank page in headless Chromium. Skips (not fails) when no browser is installed."""
    try:
        from patchright.async_api import async_playwright
    except ImportError:  # pragma: no cover
        pytest.skip("patchright not installed")
    async with async_playwright() as pw:
        try:
            browser = await pw.chromium.launch(headless=True)
        except Exception as exc:  # pragma: no cover — browser not fetched yet
            pytest.skip(f"no Chromium available ({exc}); run `av3 install-browser`")
        pg = await browser.new_page()
        try:
            yield pg
        finally:
            await browser.close()


def _q(fid, label, kind, required=False, options=None):
    return CustomQuestion(fid, label, required, kind, options=list(options or []))


def _r(q, value):
    return Resolution(question=q, value=value, source=ResolutionSource.FACT_BANK)


# Ashby Yes/No: two <button>s + a NAMED type=checkbox carrier. No aria-pressed anywhere —
# selection is marked by an extra (build-hashed) class on the chosen button. (live: Vanta)
_ASHBY_YESNO = """
<div class="ashby-application-form-field-entry">
  <div class="ashby-application-form-question-title">{title}</div>
  <div class="_container_1svni_28 _yesno_1e3gg_148">
    <button type="button" data-q="{key}" data-opt="Yes">Yes</button>
    <button type="button" data-q="{key}" data-opt="No">No</button>
    <input type="hidden" value="">
  </div>
</div>
"""

_ARM_CLICK_RECORDER = """
() => {
  window.__clicks = [];
  document.querySelectorAll('button[data-q], div.select__option').forEach(b =>
    b.addEventListener('click', () =>
      window.__clicks.push(b.dataset.q ? (b.dataset.q + '=' + b.dataset.opt)
                                       : ('option:' + b.textContent.trim()))));
  return true;
}
"""


# --------------------------------------------------------------- F1 fail-closed scoping

async def test_option_click_is_scoped_to_its_own_question(page):
    """Two Ashby-style Yes/No questions with SYNTHETIC ids (the id-less widget case).

    Regression for the headline Round 3 bug: work-auth "Yes" then sponsorship "No" used to
    produce clicks [workauth=Yes, workauth=No] — the applicant recorded as NOT authorized to
    work, with both fills reported as successful.
    """
    await page.set_content(
        _ASHBY_YESNO.format(title=Q_AUTH, key="workauth")
        + _ASHBY_YESNO.format(title=Q_SPON, key="sponsorship")
    )
    await page.evaluate(_ARM_CLICK_RECORDER)
    q_auth = _q("ashby_q1", Q_AUTH, "radio", True, ["Yes", "No"])
    q_spon = _q("ashby_q2", Q_SPON, "radio", True, ["Yes", "No"])

    assert await fill_option_group(page, q_auth, "Yes") is True
    assert await fill_option_group(page, q_spon, "No") is True
    assert await page.evaluate("() => window.__clicks") == ["workauth=Yes", "sponsorship=No"]


async def test_option_click_bails_when_the_container_cannot_be_identified(page):
    """No container ⇒ no click. Never widen the search to the document."""
    await page.set_content(_ASHBY_YESNO.format(title=Q_AUTH, key="workauth"))
    await page.evaluate(_ARM_CLICK_RECORDER)
    ghost = _q("ashby_q9", "A question that is not on this form", "radio", True, ["Yes", "No"])

    assert await fill_option_group(page, ghost, "Yes") is False
    assert await page.evaluate("() => window.__clicks") == []


# --------------------------------------------------------------- F2 checkbox safety

async def test_a_checkbox_is_never_typed_into(page):
    """`human_type` clicks to focus, so typing into a checkbox TOGGLES it — a "No" answer
    would tick the box. Greenhouse discovers checkboxes as kind='input', and the one checkbox
    on its current layout is a GDPR consent gate."""
    await page.set_content(
        '<div><label for="question_1">Do you consent to X?</label>'
        '<input type="checkbox" id="question_1" name="question_1"></div>'
    )
    q = _q("question_1", "Do you consent to X?", "input", True)
    filled = await fill_resolutions(page, [q], [_r(q, "No")])

    assert await page.evaluate("() => document.getElementById('question_1').checked") is False
    assert filled["question_1"] is False      # unfillable -> assisted, never a wrong tick


# --------------------------------------------------------------- F3 conservative combobox

async def test_open_dropdown_does_not_commit_a_loose_substring_match(page):
    """"No" must not commit "Nope, never used it"."""
    await page.set_content(
        '<div class="select__menu">'
        '<div class="select__option">Nope, never used it</div>'
        '<div class="select__option">No</div></div>'
    )
    await page.evaluate(_ARM_CLICK_RECORDER)

    assert await settle_open_dropdown(page, "No") is True
    assert await page.evaluate("() => window.__clicks") == ["option:No"]


# --------------------------------------------------------------- F4 option snapping

async def test_select_snaps_a_canonical_answer_onto_the_forms_own_prose(page):
    """`select_option` needs an EXACT value/label match, so "Yes" alone never lands on an
    option worded "Yes, I am authorized to work in the US"."""
    await page.set_content(
        '<label for="question_9">Authorized?</label>'
        '<select id="question_9" name="question_9">'
        '<option value="">-- select --</option>'
        '<option value="7001">Yes, I am authorized to work in the US</option>'
        '<option value="7002">No, I require sponsorship</option></select>'
    )
    opts = ["-- select --", "Yes, I am authorized to work in the US", "No, I require sponsorship"]
    q = _q("question_9", "Authorized?", "select", True, opts)

    filled = await fill_resolutions(page, [q], [_r(q, "Yes")])
    assert filled["question_9"] is True
    assert await page.evaluate("() => document.getElementById('question_9').value") == "7001"


# --------------------------------------------------------------- read-back verification

_READBACK_FIXTURE = f"""
<div class="ashby-application-form-field-entry">
  <label class="ashby-application-form-question-title" for="uuid-auth">{Q_AUTH}</label>
  <div class="_container_1svni_28 _yesno_1e3gg_148">
    <button class="_container_pjyt6_1 _option_1svni_32 _selected_1svni_44">Yes</button>
    <button class="_container_pjyt6_1 _option_1svni_32">No</button>
    <input type="checkbox" class="_input_1svni_78" tabindex="-1" name="uuid-auth">
  </div>
</div>
<div class="ashby-application-form-field-entry">
  <label class="ashby-application-form-question-title">{Q_LOC}</label>
  <div><input class="_input_d7ago_28" role="combobox" value="Dallas, Texas, United States"></div>
</div>
<div class="ashby-application-form-field-entry">
  <label class="ashby-application-form-question-title" for="uuid-phone">Phone Number</label>
  <input id="uuid-phone" name="uuid-phone" type="text" value="">
</div>
<div class="application-question">
  <label class="application-label" for="question_7">How many years of SQL?</label>
  <div class="select__control"><div class="select__single-value">6 to 9 years</div></div>
  <input id="question_7" name="question_7" class="select__input" role="combobox" value="">
</div>
<div class="application-question">
  <label class="application-label" for="opaque_1">Opaque widget</label>
  <div id="opaque_1"></div>
</div>
"""


def _readback_case():
    q_auth = _q("uuid-auth", Q_AUTH, "radio", True, ["Yes", "No"])
    q_loc = _q("ashby_q2", Q_LOC, "combobox")
    q_phone = _q("uuid-phone", "Phone Number", "input", True)
    q_years = _q("question_7", "How many years of SQL?", "combobox")
    q_opaque = _q("opaque_1", "Opaque widget", "input")
    questions = [q_auth, q_loc, q_phone, q_years, q_opaque]
    resolutions = [
        _r(q_auth, "Yes"),
        _r(q_loc, "Dallas, Texas, United States"),
        _r(q_phone, "+16827188130"),
        _r(q_years, "6"),
        _r(q_opaque, "whatever"),
    ]
    return questions, resolutions


async def test_read_back_reads_each_live_widget_contract(page):
    """One assertion per selected-state contract captured from live forms."""
    await page.set_content(_READBACK_FIXTURE)
    questions, _ = _readback_case()
    rb = await read_back_fills(page, questions)

    # Ashby Yes/No: no ARIA state — found via the extra class on the selected button.
    assert rb["uuid-auth"] == {"known": True, "value": "Yes"}
    # Ashby geocoder combobox: the committed place text lives in the input value.
    assert rb["ashby_q2"]["value"] == "Dallas, Texas, United States"
    # A react-select CLEARS its text <input> on commit; the value is in .select__single-value.
    # Reading only the addressable element would demote every filled Greenhouse combobox.
    assert rb["question_7"] == {"known": True, "value": "6 to 9 years"}
    # An empty control is positively empty.
    assert rb["uuid-phone"] == {"known": True, "value": ""}
    # An unreadable widget is UNKNOWN, never "empty".
    assert rb["opaque_1"]["known"] is False


async def test_verification_demotes_only_what_the_page_contradicts(page):
    await page.set_content(_READBACK_FIXTURE)
    questions, resolutions = _readback_case()
    claimed = {q.field_id: True for q in questions}      # the filler's own word

    verified = await verify_fills(page, questions, resolutions, claimed)

    assert verified["uuid-phone"] is False   # empty -> demoted
    assert verified["uuid-auth"] is True     # really selected
    assert verified["ashby_q2"] is True      # really committed
    assert verified["question_7"] is True    # react-select really committed
    assert verified["opaque_1"] is True      # unreadable -> claim kept, never a false negative


async def test_required_fill_gate_fires_then_clears(page):
    """A REQUIRED answer that didn't land must downgrade the auto-submit to assisted; once it
    lands the gate clears — including when the widget REFORMATS what we typed."""
    await page.set_content(_READBACK_FIXTURE)
    questions, resolutions = _readback_case()
    claimed = {q.field_id: True for q in questions}

    verified = await verify_fills(page, questions, resolutions, claimed)
    assert any_required_unfilled(questions, resolutions, verified) is True

    # intl-tel-input renders +16827188130 as "+1 682 718 8130" — still the same number.
    await page.evaluate(
        "() => document.getElementById('uuid-phone').value = '+1 682 718 8130'"
    )
    verified = await verify_fills(page, questions, resolutions, claimed)
    assert verified["uuid-phone"] is True
    assert any_required_unfilled(questions, resolutions, verified) is False
