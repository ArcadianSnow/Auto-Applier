"""Role-family classifier (domain/job_family.py) — deterministic title bucketing."""

from __future__ import annotations

import pytest

from auto_applier.domain.job_family import FAMILY_LABELS, JobFamily, classify_family


@pytest.mark.parametrize("title,expected", [
    # Solutions / forward-deployed / customer
    ("Solutions Engineer", JobFamily.SOLUTIONS),
    ("Senior Solutions Engineer, Enterprise", JobFamily.SOLUTIONS),
    ("Value Engineer, Solutions Engineering", JobFamily.SOLUTIONS),
    ("Implementation Engineer", JobFamily.SOLUTIONS),
    ("Forward Deployed Engineer", JobFamily.SOLUTIONS),
    ("Sales Engineer", JobFamily.SOLUTIONS),
    # Data / platform engineering (the cluster the Data-Platform résumé targets)
    ("Senior Data Platform Engineer", JobFamily.DATA_PLATFORM),
    ("Data Engineer", JobFamily.DATA_PLATFORM),
    ("Data Engineer II", JobFamily.DATA_PLATFORM),
    ("Analytics Engineer", JobFamily.DATA_PLATFORM),
    ("Lead Analytics Engineer, Borrowing", JobFamily.DATA_PLATFORM),
    ("Senior Software Engineer, Product Data Platform", JobFamily.DATA_PLATFORM),
    ("Platform Engineer", JobFamily.DATA_PLATFORM),
    # Database / DBA / SQL
    ("Senior DBA", JobFamily.DATABASE),
    ("Database Support Engineer (AMER)", JobFamily.DATABASE),
    ("PostgreSQL Specialist", JobFamily.DATABASE),
    # AI application
    ("Staff AI Engineer", JobFamily.AI_APPLICATION),
    ("Applied AI Engineer - Agentic Workflows", JobFamily.AI_APPLICATION),
    ("Machine Learning Engineer", JobFamily.AI_APPLICATION),
    # ...but an ML *platform* role is infra → DATA_PLATFORM ("platform engineer" wins).
    ("Machine Learning Platform Engineer", JobFamily.DATA_PLATFORM),
    # Backend / general SWE
    ("Backend Engineer", JobFamily.BACKEND),
    ("Senior Software Engineer", JobFamily.BACKEND),
    ("Full Stack Developer", JobFamily.BACKEND),
    # Analytics / BI (non-engineering)
    ("Revenue Analytics Manager", JobFamily.ANALYTICS),
    ("Fleet Business Intelligence Lead", JobFamily.ANALYTICS),
    ("Senior Data Analyst, Customer Care", JobFamily.ANALYTICS),
    # Other
    ("Product Manager", JobFamily.OTHER),
    ("Chief of Staff", JobFamily.OTHER),
])
def test_classify_family(title, expected):
    assert classify_family(title) is expected


def test_data_platform_beats_analytics_and_backend():
    # "Analytics Engineer" is an engineering role (DATA_PLATFORM), not a BI analyst (ANALYTICS).
    assert classify_family("Analytics Engineer") is JobFamily.DATA_PLATFORM
    # "...Software Engineer, Data Platform" leads with the platform family, not BACKEND.
    assert classify_family("Software Engineer, Data Platform") is JobFamily.DATA_PLATFORM


def test_empty_and_none_are_other():
    assert classify_family("") is JobFamily.OTHER
    assert classify_family(None) is JobFamily.OTHER
    assert classify_family("   ") is JobFamily.OTHER


def test_whole_word_matching_not_substring():
    # "ai"/"ml"/"bi" are tokens, not substrings — a title that merely contains the
    # letters must not match (e.g. "email", "html").
    assert classify_family("Email Marketing Coordinator") is JobFamily.OTHER


def test_every_family_has_a_label():
    for fam in JobFamily:
        assert fam in FAMILY_LABELS and FAMILY_LABELS[fam]
