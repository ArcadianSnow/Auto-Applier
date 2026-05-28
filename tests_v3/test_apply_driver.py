"""Greenhouse apply driver wiring (spec §8) — fake-page test, no real browser.

Verifies the dry-run path classifies CAPTCHA, fills standard fields, attaches the résumé,
discovers custom questions, and never submits. Plus survey aggregation."""

from __future__ import annotations

import asyncio

from av3.domain.state import ApplicationStatus, ApplyMode
from av3.sources.browser.greenhouse_apply import Applicant, prepare_application
from av3.sources.browser.survey import SurveyRow, summarize_survey
from av3.sources.greenhouse import JobListing


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
