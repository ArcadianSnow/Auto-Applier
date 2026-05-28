"""Lever apply driver wiring (spec §8) — fake-page tests, no real browser.

Mirrors ``test_apply_driver.py`` for the Lever path: classify CAPTCHA (hCaptcha), fill
Lever's name-keyed standard fields, attach résumé via ``#resume-upload-input``, wait for
the async résumé-parse signal (``resumeStorageId``), discover ``cards[<uuid>]`` custom
questions, and dispatch by ``(dry_run, mode)``.
"""

from __future__ import annotations

import asyncio

from av3.domain.state import ApplicationStatus, ApplyMode
from av3.sources.browser.apply_base import Applicant
from av3.sources.browser.lever_apply import prepare_application
from av3.sources.lever import LeverListing


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
    """Minimal async stand-in for a Playwright page. Tracks URL transitions across goto
    and submit so the auto path's confirmation detector can observe a real /thanks URL."""

    def __init__(
        self,
        html: str,
        scripts: list[str],
        questions: list[dict],
        *,
        resume_storage_id: str = "rsi_xyz",
        post_submit_url: str | None = None,
        post_submit_html: str | None = None,
        submit_element=True,
    ):
        self._html = html
        self._scripts = scripts
        self._questions = questions
        self._resume_storage_id = resume_storage_id
        self._post_submit_url = post_submit_url
        self._post_submit_html = post_submit_html
        self._submit_element_exists = submit_element
        self.url = ""
        self.elements: dict[str, FakeElement] = {}
        self.goto_called_with = None
        self.submit_clicked = False
        self._submit_el: FakeElement | None = None

    async def goto(self, url, **kw):
        self.goto_called_with = url
        self.url = url

    async def content(self):
        return self._html

    async def wait_for_load_state(self, *_a, **_kw):
        # Simulate the post-submit URL transition Lever performs on success.
        if self.submit_clicked and self._post_submit_url is not None:
            self.url = self._post_submit_url
            if self._post_submit_html is not None:
                self._html = self._post_submit_html

    async def eval_on_selector_all(self, selector, js):
        return self._scripts  # only used for script[src]

    async def evaluate(self, js):
        # The driver issues two evaluate() calls: the resumeStorageId poll (returns a
        # string or null) and discover_custom_questions (returns a list). Discriminate by
        # whether the JS source references resumeStorageId.
        if "resumeStorageId" in js:
            return self._resume_storage_id
        return self._questions

    async def query_selector(self, selector):
        if selector == "#btn-submit":
            if not self._submit_element_exists:
                return None

            async def click():
                self.submit_clicked = True
                if self._submit_el is not None:
                    self._submit_el.clicked = True

            if self._submit_el is None:
                self._submit_el = FakeElement()
                self._submit_el.click = click  # type: ignore[assignment]
            return self._submit_el
        el = self.elements.setdefault(selector, FakeElement())
        return el


def _listing() -> LeverListing:
    return LeverListing(
        source_job_id="abc-123",
        title="Senior Data Analyst",
        company="acmeco",
        location="Remote",
        url="https://jobs.lever.co/acmeco/abc-123",
        apply_url="https://jobs.lever.co/acmeco/abc-123/apply",
    )


# ----------------------------------------------------------------- dry-run path
def test_dry_run_classifies_hcaptcha_fills_and_does_not_submit():
    html = '<input name="h-captcha-response" id="hcaptchaResponseInput"><form>...</form>'
    scripts = ["https://hcaptcha.com/1/api.js"]
    questions = [
        {"id": "cards[uuid-1][field0]", "label": "Why Lever?", "required": True, "kind": "textarea"},
        {"id": "eeo[gender]", "label": "Gender", "required": False, "kind": "select"},
    ]
    page = FakePage(html, scripts, questions)
    applicant = Applicant("Pat", "Doe", "pat@example.com", "555-1234")

    outcome = asyncio.run(
        prepare_application(page, _listing(), applicant, resume_path="/tmp/resume.pdf", dry_run=True)
    )

    # Drives the /apply URL, not the hosted detail URL.
    assert page.goto_called_with == "https://jobs.lever.co/acmeco/abc-123/apply"
    # hCaptcha classified as invisible (Lever's default).
    assert outcome.captcha.type.value == "hcaptcha"
    assert outcome.captcha.is_invisible
    # Lever's single 'name' field gets the full name.
    assert page.elements["input[name='name']"].typed == "Pat Doe"
    assert page.elements["input[name='email']"].typed == "pat@example.com"
    assert page.elements["input[name='phone']"].typed == "555-1234"
    # Résumé attached + parse-wait observed the storage id.
    assert outcome.filled["resume"] is True
    assert outcome.filled["resume_parsed"] is True
    assert page.elements["#resume-upload-input"].files == "/tmp/resume.pdf"
    # Custom questions discovered by label.
    assert len(outcome.custom_questions) == 2
    assert outcome.custom_questions[0].label == "Why Lever?"
    # Never submitted in dry-run.
    assert outcome.submitted is False
    assert outcome.status is None
    # Auto-eligible (invisible challenge, fields filled) — not a pass-rate claim.
    assert outcome.auto_eligible is True


def test_dry_run_skips_phone_when_absent():
    page = FakePage(html="<form></form>", scripts=[], questions=[])
    outcome = asyncio.run(
        prepare_application(
            page, _listing(), Applicant("Pat", "Doe", "pat@example.com"), "/tmp/r.pdf", dry_run=True
        )
    )
    # No phone supplied → no phone input touched.
    assert "input[name='phone']" not in page.elements
    assert outcome.filled.get("phone") is None


# ------------------------------------------------------ production assisted path
def test_browser_assisted_fills_and_stops_with_status():
    """BROWSER_ASSISTED: bot pre-fills everything, status=ASSISTED_PENDING; never clicks
    submit. Validates v3's field-aligned safe default (neonwatty/Simplify posture)."""
    page = FakePage(html="<form></form>", scripts=[], questions=[])
    outcome = asyncio.run(
        prepare_application(
            page, _listing(), Applicant("A", "B", "a@b.com", "555"), "/tmp/r.pdf",
            dry_run=False, mode=ApplyMode.BROWSER_ASSISTED,
        )
    )
    assert outcome.status is ApplicationStatus.ASSISTED_PENDING
    assert outcome.submitted is False
    assert page.submit_clicked is False
    assert page.elements["input[name='name']"].typed == "A B"
    assert outcome.mode is ApplyMode.BROWSER_ASSISTED


# ---------------------------------------------------------- production auto path
def test_browser_auto_visible_challenge_downgrades_to_assisted():
    """Visible challenge -> ASSISTED_PENDING, never solved/retried (project invariant)."""
    html = '<iframe title="recaptcha challenge expires in two minutes"></iframe>'
    page = FakePage(html, scripts=[], questions=[])
    outcome = asyncio.run(
        prepare_application(
            page, _listing(), Applicant("A", "B", "a@b.com"), "",
            dry_run=False, mode=ApplyMode.BROWSER_AUTO,
        )
    )
    assert outcome.captcha.type.value == "visible_challenge"
    assert outcome.status is ApplicationStatus.ASSISTED_PENDING
    assert page.submit_clicked is False


def test_browser_auto_submits_and_confirms_via_thanks_url():
    """Auto path: invisible hCaptcha + clean fill -> submit -> /thanks URL -> APPLIED."""
    html = '<input name="h-captcha-response"><form><button id="btn-submit"></button></form>'
    page = FakePage(
        html, scripts=["https://hcaptcha.com/1/api.js"], questions=[],
        post_submit_url="https://jobs.lever.co/acmeco/abc-123/thanks",
        post_submit_html='<h1>Thank you for applying!</h1>',
    )
    outcome = asyncio.run(
        prepare_application(
            page, _listing(), Applicant("Pat", "Doe", "pat@example.com", "555"), "/tmp/r.pdf",
            dry_run=False, mode=ApplyMode.BROWSER_AUTO,
        )
    )
    assert outcome.submitted is True
    assert page.submit_clicked is True
    assert outcome.confirmation is not None
    assert outcome.confirmation.outcome.value == "confirmed"
    assert outcome.confirmation.signal == "url:/thanks"
    assert outcome.status is ApplicationStatus.APPLIED


def test_browser_auto_submit_missing_fails_fast():
    """If the submit button isn't on the page (selector drift or wrapper), fail fast to
    FAILED (which routes to REVIEW) — never blind-retry."""
    page = FakePage(html="<form></form>", scripts=[], questions=[], submit_element=False)
    outcome = asyncio.run(
        prepare_application(
            page, _listing(), Applicant("A", "B", "a@b.com"), "/tmp/r.pdf",
            dry_run=False, mode=ApplyMode.BROWSER_AUTO,
        )
    )
    assert outcome.status is ApplicationStatus.FAILED
    assert page.submit_clicked is False


def test_browser_auto_unconfirmed_when_no_positive_signal():
    """Submit fires but neither /thanks URL nor success text appears -> UNCONFIRMED
    (retry-safe via dedup keying off APPLIED job-state, not attempts)."""
    html = '<input name="h-captcha-response"><form><button id="btn-submit"></button></form>'
    page = FakePage(
        html, scripts=["https://hcaptcha.com/1/api.js"], questions=[],
        post_submit_url="https://jobs.lever.co/acmeco/abc-123/apply",  # no transition
        post_submit_html="<p>Loading...</p>",
    )
    outcome = asyncio.run(
        prepare_application(
            page, _listing(), Applicant("A", "B", "a@b.com"), "/tmp/r.pdf",
            dry_run=False, mode=ApplyMode.BROWSER_AUTO,
        )
    )
    assert outcome.submitted is True
    assert outcome.status is ApplicationStatus.UNCONFIRMED
