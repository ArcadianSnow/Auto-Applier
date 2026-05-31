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

    async def click(self):
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

    async def evaluate(self, js):
        return self._questions  # discover_custom_questions

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
