"""Fabrication guard L1 — labeled eval set (spec §6b, §11 risk ③).

Each test is a labeled (résumé, expected-verdict) pair. This is both the unit test and
the guard's eval harness: the deterministic gate must catch the lies that get people
fired (invented employer, out-of-range dates, inflated $/% metrics, fabricated skills/
credentials) while passing honest rephrasing.
"""

from __future__ import annotations

import pytest

from auto_applier.resume.factbank import (
    Contact,
    EducationEntry,
    FactBank,
    WorkEntry,
)
from auto_applier.resume.guard import (
    GeneratedResume,
    GenEducation,
    GenWorkEntry,
    Verdict,
    guard_l1,
)


@pytest.fixture
def bank() -> FactBank:
    return FactBank(
        contact=Contact(name="Pat Doe"),
        work_history=[
            WorkEntry("Acme Corporation", "Senior Data Analyst", "2019-03", "2022-06"),
            WorkEntry("Globex Inc", "Data Engineer", "2022-07", "Present"),
        ],
        education=[EducationEntry("University of Washington", "B.S. Computer Science")],
        skills=["Python", "SQL", "Tableau", "Apache Spark"],
        certifications=["AWS Certified Solutions Architect"],
        allowed_metrics=["managed $2M budget", "improved query speed by 40%", "team of 8"],
    )


def _truthful() -> GeneratedResume:
    return GeneratedResume(
        summary="Data analyst.",
        work=[
            GenWorkEntry(
                "Acme Corporation", "Senior Data Analyst", "2019-03", "2022-06",
                bullets=["Managed $2M budget", "Improved query speed by 40%"],
            )
        ],
        education=[GenEducation("University of Washington", "B.S. Computer Science")],
        skills=["Python", "SQL"],
    )


def test_truthful_passes(bank):
    assert guard_l1(_truthful(), bank).verdict is Verdict.PASS


def test_legit_title_rephrase_passes(bank):
    r = _truthful()
    r.work[0].title = "Sr. Data Analyst"  # sr → senior; should not flag
    res = guard_l1(r, bank)
    assert res.verdict is Verdict.PASS, [f.reason for f in res.findings]


def test_invented_company_hard_fails(bank):
    r = _truthful()
    r.work[0].company = "Initech"
    res = guard_l1(r, bank)
    assert res.verdict is Verdict.HARD_FAIL
    assert any(f.category == "company" for f in res.hard_fails())


def test_start_before_bank_span_hard_fails(bank):
    r = _truthful()
    r.work[0].start = "2015-01"  # bank started 2019-03
    res = guard_l1(r, bank)
    assert res.verdict is Verdict.HARD_FAIL
    assert any(f.category == "date" for f in res.hard_fails())


def test_claiming_still_employed_hard_fails(bank):
    r = _truthful()
    r.work[0].end = "Present"  # bank ended 2022-06
    res = guard_l1(r, bank)
    assert res.verdict is Verdict.HARD_FAIL
    assert any(f.category == "date" for f in res.hard_fails())


def test_inflated_money_metric_hard_fails(bank):
    r = _truthful()
    r.work[0].bullets = ["Managed $20M budget"]  # bank owns $2M
    res = guard_l1(r, bank)
    assert res.verdict is Verdict.HARD_FAIL
    assert any(f.category == "metric" for f in res.hard_fails())


def test_inflated_percent_metric_hard_fails(bank):
    r = _truthful()
    r.work[0].bullets = ["Improved query speed by 90%"]  # bank owns 40%
    res = guard_l1(r, bank)
    assert res.verdict is Verdict.HARD_FAIL


def test_supported_metric_passes(bank):
    r = _truthful()
    r.work[0].bullets = ["Managed $2M budget", "Improved query speed by 40%"]
    assert guard_l1(r, bank).verdict is Verdict.PASS


def test_invented_skill_hard_fails(bank):
    r = _truthful()
    r.skills = ["Python", "Kubernetes"]  # never used Kubernetes
    res = guard_l1(r, bank)
    assert res.verdict is Verdict.HARD_FAIL
    assert any(f.category == "skill" and "Kubernetes" in f.claim for f in res.hard_fails())


def test_invented_credential_hard_fails(bank):
    r = _truthful()
    r.education = [GenEducation("Massachusetts Institute of Technology", "PhD Physics")]
    res = guard_l1(r, bank)
    assert res.verdict is Verdict.HARD_FAIL
    assert any(f.category == "credential" for f in res.hard_fails())


def test_unowned_scale_claim_reviews(bank):
    r = _truthful()
    r.work[0].bullets = ["Led team of 50"]  # bank owns "team of 8"
    res = guard_l1(r, bank)
    assert res.verdict is Verdict.REVIEW  # scale → review, not hard fail
    assert not res.ok


def test_owned_scale_claim_passes(bank):
    r = _truthful()
    r.work[0].bullets = ["Led team of 8"]
    assert guard_l1(r, bank).verdict is Verdict.PASS


def test_result_ok_only_on_pass(bank):
    assert guard_l1(_truthful(), bank).ok is True
    r = _truthful()
    r.skills = ["COBOL"]
    assert guard_l1(r, bank).ok is False
