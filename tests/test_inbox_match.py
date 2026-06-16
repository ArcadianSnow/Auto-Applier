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
