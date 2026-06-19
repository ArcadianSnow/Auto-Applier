"""Email → applied-job matching (email-outcome-loop Phase A) — pure / offline.

url-hit > company+role > company-only > no-match precedence against an in-memory
list[Job].
"""

from __future__ import annotations

from auto_applier.domain.models import Job
from auto_applier.domain.state import OutcomeKind
from auto_applier.inbox.classify import EmailClass
from auto_applier.inbox.match import match_email
from auto_applier.inbox.parse import FetchedEmail


def _job(**kw) -> Job:
    base = dict(source="greenhouse", source_job_id="x", title="Data Engineer", company="Acme")
    base.update(kw)
    return Job(**base)


def _cls(company="", role="", kind=OutcomeKind.RESPONSE) -> EmailClass:
    return EmailClass(
        kind=kind, company_hint=company, role_hint=role,
        confidence=0.9, method="deterministic", security_code_flag=False,
    )


def _email(body="") -> FetchedEmail:
    return FetchedEmail(
        uid="1", message_id="<m@x>", subject="s", from_addr="a@b.com",
        from_name="", body_text=body, date_iso="", raw_size=len(body),
    )


def test_url_hit_wins():
    job = _job(id="J1", url="https://boards.greenhouse.io/acme/jobs/999")
    other = _job(id="J2", company="Acme", title="Data Engineer", url="")
    body = "Track your application at https://boards.greenhouse.io/acme/jobs/999 today."
    res = match_email(_cls(company="Acme", role="Data Engineer"), _email(body), [other, job])
    assert res.job_id == "J1"
    assert res.reason == "url"
    assert res.confidence == 0.95


def test_company_plus_role():
    job = _job(id="J1", company="Acme Inc", title="Senior Data Engineer", url="")
    res = match_email(
        _cls(company="Acme Inc", role="Data Engineer"), _email("no url here"), [job]
    )
    assert res.job_id == "J1"
    assert res.reason == "company+role"
    assert res.confidence == 0.80


def test_company_only_when_role_does_not_overlap():
    job = _job(id="J1", company="Acme", title="Marketing Manager", url="")
    res = match_email(
        _cls(company="Acme", role="Data Engineer"), _email("body"), [job]
    )
    assert res.job_id == "J1"
    assert res.reason == "company"
    assert res.confidence == 0.60


def test_company_only_when_no_role_hint():
    job = _job(id="J1", company="Acme", title="Data Engineer", url="")
    res = match_email(_cls(company="Acme", role=""), _email("body"), [job])
    assert res.job_id == "J1"
    assert res.reason == "company"


def test_no_match():
    job = _job(id="J1", company="Globex", title="Backend Engineer", url="https://x/1")
    res = match_email(
        _cls(company="Acme", role="Data Engineer"), _email("unrelated body"), [job]
    )
    assert res.job_id is None
    assert res.reason == "none"
    assert res.confidence == 0.0


def test_company_normalization():
    # "Acme, Inc." (classifier hint) normalizes to match "acme inc" (job.company)
    job = _job(id="J1", company="acme inc", title="Data Engineer", url="")
    res = match_email(
        _cls(company="Acme, Inc.", role="Data Engineer"), _email("body"), [job]
    )
    assert res.job_id == "J1"
    assert res.reason == "company+role"


def test_empty_url_not_matched_as_substring():
    # A job with url="" must not match just because "" is a substring of any body.
    job = _job(id="J1", company="Zzz", title="Engineer", url="")
    res = match_email(_cls(company="", role=""), _email("any body"), [job])
    assert res.job_id is None
    assert res.reason == "none"


# ---------------------------------------------- multi-role same company (Phase C precision)

def _monzo_jobs() -> list:
    lead = _job(id="LEAD", company="Monzo", title="Lead Analytics Engineer, Borrowing", url="")
    ml = _job(id="ML", company="Monzo", title="Machine Learning Platform Engineer", url="")
    return [lead, ml]


def test_multi_role_resolved_by_role_named_in_body():
    """Generic subject, role named in the BODY (the real Monzo rejection shape) → the
    EXACT sibling is chosen, not just the first one listed."""
    body = ("Thanks for applying to Monzo. Unfortunately we won't be moving forward "
            "with your application for the Machine Learning Platform Engineer position.")
    res = match_email(
        _cls(company="Monzo", role="", kind=OutcomeKind.REJECTION), _email(body), _monzo_jobs()
    )
    assert res.job_id == "ML"
    assert res.reason == "company+role"
    assert res.confidence == 0.80


def test_multi_role_resolves_the_other_sibling():
    body = ("Unfortunately we won't be moving forward with your application for the "
            "Lead Analytics Engineer, Borrowing position this time.")
    res = match_email(_cls(company="Monzo", role=""), _email(body), _monzo_jobs())
    assert res.job_id == "LEAD"


def test_multi_role_prefers_longest_title():
    """The real Dataiku case: "Data Engineer II" must win when the email says "II",
    and "Data Engineer" must win when it does NOT (its sibling is a superstring)."""
    de = _job(id="DE", company="Dataiku", title="Data Engineer", url="")
    de2 = _job(id="DE2", company="Dataiku", title="Data Engineer II", url="")
    jobs = [de, de2]

    body_ii = "Thank you for your interest in the Data Engineer II opening at Dataiku."
    assert match_email(_cls(company="Dataiku"), _email(body_ii), jobs).job_id == "DE2"

    body_de = "Thank you for your interest in the Data Engineer opening at Dataiku."
    assert match_email(_cls(company="Dataiku"), _email(body_de), jobs).job_id == "DE"


def test_multi_role_ambiguous_fails_to_review():
    """Several applied roles at one company + a GENERIC email naming none → no guess;
    fail closed to review (job_id None, sub-floor confidence), never a wrong sibling."""
    res = match_email(
        _cls(company="Monzo", role=""),
        _email("Your application to Monzo. We won't be moving forward at this time."),
        _monzo_jobs(),
    )
    assert res.job_id is None
    assert res.reason == "company-ambiguous"
    assert res.confidence == 0.50


def test_company_legal_suffix_stripped():
    """The email's domain-derived hint ("Gusto") matches the ATS legal name
    ("Gusto, Inc.") — the real reason a Gusto rejection couldn't bind to its job."""
    job = _job(id="G", company="Gusto, Inc.", title="Senior Data Engineer", url="")
    res = match_email(
        _cls(company="Gusto", role="", kind=OutcomeKind.REJECTION),
        _email("After reviewing your application, we won't be moving forward."), [job],
    )
    assert res.job_id == "G"
    assert res.reason == "company"


def test_multi_role_url_still_wins_outright():
    """A url hit short-circuits before any role disambiguation."""
    jobs = _monzo_jobs()
    jobs[1] = _job(id="ML", company="Monzo", title="Machine Learning Platform Engineer",
                   url="https://job-boards.greenhouse.io/monzo/jobs/7118972")
    body = "View your application at https://job-boards.greenhouse.io/monzo/jobs/7118972"
    res = match_email(_cls(company="Monzo"), _email(body), jobs)
    assert res.job_id == "ML"
    assert res.reason == "url"
