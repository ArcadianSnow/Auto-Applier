"""Greenhouse apply driver wiring (spec §8) — fake-page test, no real browser.

Verifies the dry-run path classifies CAPTCHA, fills standard fields, attaches the résumé,
discovers custom questions, and never submits. Plus survey aggregation."""

from __future__ import annotations

import asyncio

from auto_applier.domain.state import ApplicationStatus, ApplyMode
from auto_applier.sources.browser.greenhouse_apply import Applicant, prepare_application
from auto_applier.sources.browser.survey import SurveyRow, summarize_survey
from auto_applier.sources.greenhouse import JobListing


class FakeElement:
    def __init__(self):
        self.typed = ""
        self.files = None
        self.clicked = False

    async def click(self, **kw):  # mirrors ElementHandle.click(timeout=...)
        self.clicked = True

    async def type(self, ch):
        self.typed += ch

    async def set_input_files(self, path):
        self.files = path


class FakePage:
    """Minimal async stand-in for a Playwright page (dry-run path only)."""

    def __init__(self, html: str, scripts: list[str], questions: list[dict]):
        self._html = html
        self._scripts = scripts
        self._questions = questions
        self.url = ""
        self.elements: dict[str, FakeElement] = {}
        self.goto_called_with = None
        self.submit_clicked = False

    async def goto(self, url, **kw):
        self.goto_called_with = url
        self.url = url

    async def content(self):
        return self._html

    async def eval_on_selector_all(self, selector, js):
        return self._scripts  # only used for script[src]

    async def evaluate(self, js, arg=None):
        # Two-arg call = fill_phone's intl-tel-input setNumber probe; the fake has no iti
        # instance, so return False to exercise the type-the-+E.164 fallback. One-arg call
        # = discover_custom_questions.
        if arg is not None:
            return False
        return self._questions

    async def query_selector(self, selector):
        if selector == "button[type=submit]":
            self.submit_clicked = False
            return None  # not used in dry-run
        el = self.elements.setdefault(selector, FakeElement())
        return el

    async def select_option(self, selector, value):
        # Mirror Playwright's page.select_option: records the chosen value on the
        # element so test assertions can verify the resolver wrote it through.
        el = self.elements.setdefault(selector, FakeElement())
        el.selected = value


def _listing() -> JobListing:
    return JobListing(
        source_job_id="1", title="Senior Data Analyst", company="Acme",
        location="Remote", url="https://job-boards.greenhouse.io/acme/jobs/1",
        board_token="acme",
    )


def test_dry_run_classifies_fills_and_does_not_submit():
    html = '<textarea id="g-recaptcha-response"></textarea><form>...</form>'
    scripts = ["https://www.gstatic.com/recaptcha/releases/x/recaptcha__en.js"]
    questions = [
        {"id": "question_111", "label": "Why do you want to work here?", "required": True, "kind": "textarea"},
        {"id": "question_222", "label": "Work authorized?", "required": True, "kind": "select"},
    ]
    page = FakePage(html, scripts, questions)
    applicant = Applicant("Pat", "Doe", "pat@example.com", "555-1234")

    outcome = asyncio.run(
        prepare_application(page, _listing(), applicant, resume_path="/tmp/resume.pdf", dry_run=True)
    )

    # navigated to the canonical URL
    assert page.goto_called_with == "https://job-boards.greenhouse.io/acme/jobs/1"
    # CAPTCHA classified as invisible reCAPTCHA (the common Greenhouse case)
    assert outcome.captcha.type.value == "recaptcha_invisible"
    assert outcome.captcha.is_invisible
    # standard fields filled with human typing
    assert page.elements["#first_name"].typed == "Pat"
    assert page.elements["#email"].typed == "pat@example.com"
    # résumé attached via native input
    assert outcome.filled["resume"] is True
    assert page.elements["#resume"].files == "/tmp/resume.pdf"
    # custom questions discovered by label
    assert len(outcome.custom_questions) == 2
    assert outcome.custom_questions[0].label == "Why do you want to work here?"
    # NEVER submitted in dry-run
    assert outcome.submitted is False
    assert outcome.status is None
    # would be auto-eligible (invisible challenge, fields filled) — not a pass-rate claim
    assert outcome.auto_eligible is True


def test_dry_run_visible_challenge_not_auto_eligible():
    html = '<iframe title="recaptcha challenge expires in two minutes"></iframe>'
    page = FakePage(html, scripts=[], questions=[])
    outcome = asyncio.run(
        prepare_application(page, _listing(), Applicant("A", "B", "a@b.com"), "", dry_run=True)
    )
    assert outcome.captcha.type.value == "visible_challenge"
    assert outcome.auto_eligible is False  # visible → would route to assisted


def test_browser_assisted_fills_and_stops_with_status():
    """Production assisted: bot pre-fills, status=ASSISTED_PENDING, never clicks submit.
    Validates the v3 safe-default posture on Greenhouse (which leans assisted anyway given
    100% reCAPTCHA Enterprise in the survey)."""
    html = '<textarea id="g-recaptcha-response"></textarea><form>...</form>'
    scripts = ["https://www.gstatic.com/recaptcha/enterprise.js"]
    page = FakePage(html, scripts, questions=[])
    outcome = asyncio.run(
        prepare_application(
            page, _listing(), Applicant("Pat", "Doe", "pat@example.com", "555"),
            resume_path="/tmp/r.pdf",
            dry_run=False, mode=ApplyMode.BROWSER_ASSISTED,
        )
    )
    assert outcome.status is ApplicationStatus.ASSISTED_PENDING
    assert outcome.submitted is False
    assert page.submit_clicked is False
    assert page.elements["#first_name"].typed == "Pat"
    assert outcome.mode is ApplyMode.BROWSER_ASSISTED


# ----------------------------------------------------------- resolver wiring (§8b)

class _FakeResolver:
    """Returns pre-canned Resolution objects by field_id. Lets the driver test the
    wiring without dragging in the real AnswerResolver (which has its own tests)."""

    def __init__(self, table: dict):
        self.table = table

    async def resolve_all(self, questions):
        return [self.table[q.field_id] for q in questions]


def _resolved(question, value):
    from auto_applier.resume.answer_resolver import Resolution, ResolutionSource
    return Resolution(question=question, value=value, source=ResolutionSource.BANK)


def _review(question):
    from auto_applier.resume.answer_resolver import Resolution, ResolutionSource
    return Resolution(question=question, value=None, source=ResolutionSource.REVIEW,
                      needs_review=True)


def test_resolver_fills_questions_and_records_resolutions():
    html = '<form>...</form>'
    questions = [
        {"id": "question_111", "label": "Why us?", "required": True, "kind": "textarea"},
        {"id": "question_222", "label": "Work authorized?", "required": True, "kind": "select"},
    ]
    page = FakePage(html, scripts=[], questions=questions)
    # Pre-build CustomQuestion objects matching what the driver will discover, then map
    # them to canned resolutions in the fake resolver.
    from auto_applier.sources.browser.apply_base import CustomQuestion
    q1 = CustomQuestion("question_111", "Why us?", True, "textarea")
    q2 = CustomQuestion("question_222", "Work authorized?", True, "select")
    resolver = _FakeResolver({
        "question_111": _resolved(q1, "I love your mission."),
        "question_222": _resolved(q2, "Yes"),
    })

    outcome = asyncio.run(
        prepare_application(
            page, _listing(), Applicant("Pat", "Doe", "pat@example.com"), "/tmp/r.pdf",
            dry_run=True, resolver=resolver,
        )
    )

    assert len(outcome.resolutions) == 2
    # Textarea typed via human_type
    assert page.elements["#question_111"].typed == "I love your mission."
    # Select hit page.select_option
    assert page.elements["#question_222"].selected == "Yes"
    # filled dict records per-question outcomes
    assert outcome.filled["q:question_111"] is True
    assert outcome.filled["q:question_222"] is True


def test_required_question_unresolved_downgrades_auto_to_assisted():
    """A required custom question that bails to REVIEW must NEVER auto-submit (§8b).
    The driver downgrades to ASSISTED_PENDING so the human answers + submits."""
    html = '<textarea id="g-recaptcha-response"></textarea><form><button type="submit"></button></form>'
    scripts = ["https://www.gstatic.com/recaptcha/releases/x/recaptcha__en.js"]
    questions = [
        {"id": "question_999", "label": "Describe your worst failure", "required": True, "kind": "textarea"},
    ]
    page = FakePage(html, scripts, questions)
    from auto_applier.sources.browser.apply_base import CustomQuestion
    q = CustomQuestion("question_999", "Describe your worst failure", True, "textarea")
    resolver = _FakeResolver({"question_999": _review(q)})

    outcome = asyncio.run(
        prepare_application(
            page, _listing(), Applicant("A", "B", "a@b.com"), "/tmp/r.pdf",
            dry_run=False, mode=ApplyMode.BROWSER_AUTO, resolver=resolver,
        )
    )
    assert outcome.status is ApplicationStatus.ASSISTED_PENDING
    assert outcome.submitted is False
    assert "unresolved" in outcome.note


def test_human_attestation_gate_downgrades_auto_to_assisted_end_to_end():
    """Blocker A, full chain: a native-select "Which best describes you?" whose options
    are AI-vs-human is discovered WITH options, the REAL resolver classifies it
    HUMAN_ATTESTATION → REVIEW (the bot must never attest to being human), and because
    it is required the driver downgrades BROWSER_AUTO → ASSISTED_PENDING. The human then
    truthfully attests when they submit. (research/automated-apply-go-live.md, blocker A)"""
    from auto_applier.db import init_app_db
    from auto_applier.db.repositories import AnswerRepo
    from auto_applier.resume.answer_resolver import (
        AnswerResolver, ResolutionSource, SensitiveClass,
    )
    from auto_applier.resume.factbank import Contact, FactBank

    html = '<textarea id="g-recaptcha-response"></textarea><form><button type="submit"></button></form>'
    scripts = ["https://www.gstatic.com/recaptcha/releases/x/recaptcha__en.js"]
    # Non-descriptive label — caught only by the option PAIR captured at discovery.
    questions = [{
        "id": "question_777",
        "label": "Which of the following best describes you?",
        "required": True, "kind": "select",
        "options": ["I am an AI or automated program", "I am a human being"],
    }]
    page = FakePage(html, scripts, questions)

    import tempfile, os
    db_path = os.path.join(tempfile.mkdtemp(), "app.db")
    bank = FactBank(contact=Contact(name="Pat Doe", email="pat@x.com", location="Remote"),
                    work_authorization="US citizen")
    resolver = AnswerResolver(bank, answer_repo=AnswerRepo(init_app_db(db_path)))

    outcome = asyncio.run(
        prepare_application(
            page, _listing(), Applicant("Pat", "Doe", "pat@x.com"), "/tmp/r.pdf",
            dry_run=False, mode=ApplyMode.BROWSER_AUTO, resolver=resolver,
        )
    )
    # The option pair was captured through discovery onto the CustomQuestion.
    assert outcome.custom_questions[0].options == [
        "I am an AI or automated program", "I am a human being"]
    # Resolver flagged it as the attestation gate and bailed to REVIEW.
    assert outcome.resolutions[0].sensitive is SensitiveClass.HUMAN_ATTESTATION
    assert outcome.resolutions[0].source is ResolutionSource.REVIEW
    # Required + unresolved → never auto-submitted; handed to the human.
    assert outcome.status is ApplicationStatus.ASSISTED_PENDING
    assert outcome.submitted is False


def test_normalize_phone_for_intl_tel_input():
    """Phone must be typed as +E.164 so intl-tel-input auto-selects the country (its
    default flag is the globe = no country). Verified live on Greenhouse 2026-06-12."""
    from auto_applier.sources.browser.apply_base import normalize_phone
    assert normalize_phone("1-682-718-8130") == "+16827188130"   # his number (cc + 10)
    assert normalize_phone("682-718-8130") == "+16827188130"     # bare US national
    assert normalize_phone("(682) 718 8130") == "+16827188130"
    assert normalize_phone("+44 20 7946 0958") == "+442079460958"  # already international
    assert normalize_phone("") == ""
    assert normalize_phone("  ") == ""


def test_greenhouse_phone_filled_as_e164():
    """The driver normalizes before filling — the form must receive +E.164, not 1-682-…
    (here the fake has no iti instance, so fill_phone falls back to typing)."""
    html = '<form></form>'
    page = FakePage(html, scripts=[], questions=[])
    applicant = Applicant("Pat", "Doe", "pat@example.com", "1-682-718-8130")
    asyncio.run(prepare_application(page, _listing(), applicant, "/tmp/r.pdf", dry_run=True))
    assert page.elements["#phone"].typed == "+16827188130"


def test_fill_phone_prefers_iti_setnumber_api():
    """When intl-tel-input IS present, fill_phone calls setNumber via evaluate (no typing) —
    this is what fixes the doubled-+1 in separate-dial-code mode."""
    from auto_applier.sources.browser.apply_base import fill_phone

    class ItiPage:
        def __init__(self): self.arg = None
        async def evaluate(self, js, arg=None):
            self.arg = arg
            return True  # iti instance present, setNumber succeeded
        async def query_selector(self, sel):
            raise AssertionError("must not fall back to typing when iti is present")

    p = ItiPage()
    assert asyncio.run(fill_phone(p, "#phone", "1-682-718-8130")) is True
    assert p.arg == ["#phone", "+16827188130"]  # normalized E.164 passed to setNumber


def test_fill_phone_separate_dial_code_types_national_only():
    """In separate-dial-code mode the dial code is shown OUTSIDE the input, so typing the
    +CC doubles it (the bug the user saw). fill_phone types the national digits only."""
    from auto_applier.sources.browser.apply_base import fill_phone

    class SepPage:
        def __init__(self): self.typed = ""
        async def evaluate(self, js, arg=None):
            if isinstance(arg, list):
                return False                       # no iti instance on window
            return {"mode": "separate", "dial": "+1"}  # separate-dial-code mode probe
        async def query_selector(self, sel):
            outer = self
            class El:
                async def click(self, **k): pass
                async def type(self, ch): outer.typed += ch
            return El()

    p = SepPage()
    assert asyncio.run(fill_phone(p, "#phone", "1-682-718-8130")) is True
    assert p.typed == "6827188130"  # national significant number, NO leading +1


def test_fill_refuses_to_type_a_human_affirmation():
    """Value-side backstop (blocker A): even if classification ever slips and a resolver
    hands back "I am a human being", the FILLER must never type it. Field left unfilled."""
    from auto_applier.sources.browser.apply_base import (
        CustomQuestion, affirms_human, fill_resolutions,
    )
    from auto_applier.resume.answer_resolver import Resolution, ResolutionSource

    assert affirms_human("I am a human being") is True
    assert affirms_human("I am not a robot") is True
    assert affirms_human("United States") is False

    q = CustomQuestion("question_att", "Which best describes you?", True, "select")
    # A (hypothetical) bad resolution that slipped through as a fillable value.
    bad = Resolution(question=q, value="I am a human being", source=ResolutionSource.INFERRED)
    page = FakePage("<form></form>", scripts=[], questions=[])
    filled = asyncio.run(fill_resolutions(page, [q], [bad]))
    assert filled["question_att"] is False
    # Nothing was selected on the element.
    assert not hasattr(page.elements.get(f"#question_att", FakeElement()), "selected") or \
        getattr(page.elements.get("#question_att"), "selected", None) is None


def test_optional_unresolved_does_not_block_auto_submit():
    """Optional REVIEWs are benign — driver still attempts auto path (other checks may
    still bounce it, but resolver state shouldn't)."""
    html = '<textarea id="g-recaptcha-response"></textarea><form></form>'
    page = FakePage(html, scripts=["https://www.gstatic.com/recaptcha/releases/x/recaptcha__en.js"],
                    questions=[{"id": "question_x", "label": "Anything else?", "required": False, "kind": "textarea"}])
    from auto_applier.sources.browser.apply_base import CustomQuestion
    q = CustomQuestion("question_x", "Anything else?", False, "textarea")
    resolver = _FakeResolver({"question_x": _review(q)})

    outcome = asyncio.run(
        prepare_application(
            page, _listing(), Applicant("A", "B", "a@b.com"), "/tmp/r.pdf",
            dry_run=False, mode=ApplyMode.BROWSER_AUTO, resolver=resolver,
        )
    )
    # Submit selector returns None in this FakePage -> FAILED (mid-form break, not
    # a resolver-driven downgrade). What we're verifying is that we did NOT
    # short-circuit to ASSISTED_PENDING just because an OPTIONAL Q reviewed.
    assert outcome.status is ApplicationStatus.FAILED
    assert "submit button" in outcome.note


def test_summarize_survey():
    rows = [
        SurveyRow("a", "u1", "t", "recaptcha_invisible", True, False, 3, True, form_present=True),
        SurveyRow("b", "u2", "t", "recaptcha_enterprise", True, True, 5, True, form_present=True),
        SurveyRow("c", "u3", "t", "visible_challenge", False, False, 0, False, form_present=True),
        SurveyRow("d", "u4", "t", "none", False, False, 0, False, form_present=False),  # wrapper
    ]
    s = summarize_survey(rows)
    assert s["n"] == 4
    assert s["forms_present"] == 3  # the wrapper-redirect row excluded from form stats
    assert s["pct_invisible_of_forms"] == round(100 * 2 / 3, 1)
    assert s["pct_enterprise_of_forms"] == round(100 * 1 / 3, 1)
    assert s["pct_auto_eligible_of_forms"] == round(100 * 2 / 3, 1)
    assert s["by_captcha_type"]["recaptcha_invisible"] == 1


def test_summarize_empty():
    assert summarize_survey([]) == {"n": 0}


# ---------------------------------------------------------------------------
# settle_open_dropdown — react-select cleanup (live 2026-06-11: an open
# "No options" menu intercepted pointer events over every later field).
# ---------------------------------------------------------------------------

from auto_applier.sources.browser.apply_base import settle_open_dropdown


class _MenuOption:
    def __init__(self, text):
        self._text = text
        self.clicked = False

    async def text_content(self):
        return self._text

    async def click(self, **kw):
        self.clicked = True


class _Keyboard:
    def __init__(self):
        self.pressed = []

    async def press(self, key):
        self.pressed.append(key)


class _MenuPage:
    """Fake page with an open react-select menu and a real keyboard."""

    def __init__(self, menu_open=True, options=()):
        self._menu_open = menu_open
        self.options = list(options)
        self.keyboard = _Keyboard()

    async def query_selector(self, selector):
        if selector == ".select__menu":
            return object() if self._menu_open else None
        return None

    async def query_selector_all(self, selector):
        return self.options if selector == ".select__option" else []


def test_settle_no_menu_is_noop():
    page = _MenuPage(menu_open=False)
    assert asyncio.run(settle_open_dropdown(page, "Yes")) is False
    assert page.keyboard.pressed == []


def test_settle_commits_matching_option():
    yes, no = _MenuOption("Yes"), _MenuOption("No")
    page = _MenuPage(options=[no, yes])
    assert asyncio.run(settle_open_dropdown(page, "yes")) is True
    assert yes.clicked and not no.clicked
    assert page.keyboard.pressed == []  # committed, no Escape needed


def test_settle_escapes_when_no_option_matches():
    page = _MenuPage(options=[_MenuOption("Acme Office"), _MenuOption("Remote")])
    assert asyncio.run(settle_open_dropdown(page, "totally unrelated")) is False
    assert page.keyboard.pressed == ["Escape"]


def test_settle_swallows_broken_pages():
    class _Broken:
        async def query_selector(self, selector):
            raise RuntimeError("boom")

    assert asyncio.run(settle_open_dropdown(_Broken(), "Yes")) is False
