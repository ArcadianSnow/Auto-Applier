"""Tests for the contextual priority layer + date handling in FormFiller.

The existing form filler tests live elsewhere; this file covers the
new behaviour introduced when we taught FormFiller about 'how did
you hear about this', 'previously worked for us', and native date
pickers.
"""
from datetime import date, timedelta
from unittest.mock import MagicMock

import pytest

from auto_applier.browser.form_filler import (
    DEFAULT_START_DATE_OFFSET_DAYS,
    FormFiller,
)
from auto_applier.browser.selector_utils import FormField


def _filler(
    resume_text: str = "",
    company: str = "",
    platform: str = "",
) -> FormFiller:
    return FormFiller(
        router=MagicMock(),
        personal_info={},
        resume_text=resume_text,
        job_description="",
        company_name=company,
        job_title="",
        resume_label="",
        platform_display_name=platform,
    )


def _text_field(label: str) -> FormField:
    return FormField(label=label, element=MagicMock(), field_type="text")


# ---------------------------------------------------------------------------
# Source attribution
# ---------------------------------------------------------------------------


class TestSourceAttribution:
    # _match_contextual is called with an already-lowercased label by
    # fill_field, so tests pass lowercase to match the real contract.

    def test_matches_how_did_you_hear(self):
        f = _filler(platform="LinkedIn")
        result = f._match_contextual(
            "how did you hear about this position?", _text_field("x"),
        )
        assert result == "LinkedIn"

    def test_matches_where_did_you_find(self):
        f = _filler(platform="Indeed")
        assert f._match_contextual(
            "where did you find this role?", _text_field("x"),
        ) == "Indeed"

    def test_matches_referral_source(self):
        f = _filler(platform="Dice")
        assert f._match_contextual(
            "referral source", _text_field("x"),
        ) == "Dice"

    def test_empty_platform_falls_through(self):
        f = _filler(platform="")
        assert f._match_contextual(
            "how did you hear about this?", _text_field("x"),
        ) == ""

    def test_no_match_returns_empty(self):
        f = _filler(platform="LinkedIn")
        assert f._match_contextual(
            "what is your favorite color?", _text_field("x"),
        ) == ""


# ---------------------------------------------------------------------------
# Prior employment check
# ---------------------------------------------------------------------------


class TestPriorEmployment:
    def test_yes_when_company_in_resume(self):
        f = _filler(
            resume_text="Senior Engineer at Acme Corporation from 2020 to 2023",
            company="Acme Corporation",
        )
        result = f._match_contextual(
            "have you previously worked for this company?", _text_field("x"),
        )
        assert result == "Yes"

    def test_canonical_match_handles_suffix(self):
        # Resume says 'Acme Inc.' but company is listed as 'Acme Corp'
        f = _filler(
            resume_text="Worked at Acme Inc. from 2019 to 2022",
            company="Acme Corp",
        )
        assert f._check_prior_employment() == "Yes"

    def test_no_when_different_company(self):
        f = _filler(
            resume_text="Worked at Globex from 2018 to 2020",
            company="Acme",
        )
        assert f._check_prior_employment() == "No"

    def test_no_when_resume_empty(self):
        f = _filler(company="Acme")
        assert f._check_prior_employment() == "No"

    def test_no_when_company_empty(self):
        f = _filler(resume_text="worked at Acme")
        assert f._check_prior_employment() == "No"

    def test_matches_formerly_employed_phrasing(self):
        f = _filler(resume_text="worked at Acme", company="Acme")
        assert f._match_contextual(
            "were you formerly employed by acme?", _text_field("x"),
        ) == "Yes"

    def test_matches_ever_worked_for(self):
        f = _filler(resume_text="worked at Acme", company="Acme")
        assert f._match_contextual(
            "have you ever worked for acme?", _text_field("x"),
        ) == "Yes"


# ---------------------------------------------------------------------------
# Start date
# ---------------------------------------------------------------------------


class TestStartDate:
    def test_matches_earliest_start(self):
        f = _filler()
        result = f._match_contextual(
            "earliest start date", _text_field("x"),
        )
        expected = (
            date.today() + timedelta(days=DEFAULT_START_DATE_OFFSET_DAYS)
        ).isoformat()
        assert result == expected

    def test_matches_when_can_you_start(self):
        f = _filler()
        result = f._match_contextual(
            "when can you start?", _text_field("x"),
        )
        # ISO format YYYY-MM-DD
        assert len(result) == 10
        assert result[4] == "-"

    def test_matches_availability_date(self):
        f = _filler()
        assert f._match_contextual(
            "availability date", _text_field("x"),
        ) != ""


# ---------------------------------------------------------------------------
# Date coercion
# ---------------------------------------------------------------------------


class TestCoerceIsoDate:
    def test_iso_passthrough(self):
        assert FormFiller._coerce_iso_date("2026-05-01") == "2026-05-01"

    def test_us_slash_format(self):
        assert FormFiller._coerce_iso_date("05/01/2026") == "2026-05-01"

    def test_long_month_format(self):
        assert FormFiller._coerce_iso_date("May 1, 2026") == "2026-05-01"

    def test_short_month_format(self):
        assert FormFiller._coerce_iso_date("May 1, 2026") == "2026-05-01"

    def test_unparseable_uses_default(self):
        result = FormFiller._coerce_iso_date("in a couple of weeks")
        expected = (
            date.today() + timedelta(days=DEFAULT_START_DATE_OFFSET_DAYS)
        ).isoformat()
        assert result == expected

    def test_empty_uses_default(self):
        result = FormFiller._coerce_iso_date("")
        expected = (
            date.today() + timedelta(days=DEFAULT_START_DATE_OFFSET_DAYS)
        ).isoformat()
        assert result == expected


# ---------------------------------------------------------------------------
# Priority chain integration
# ---------------------------------------------------------------------------


class TestPriorityChain:
    def test_source_beats_llm(self):
        """Contextual match should beat LLM so we don't waste a call."""
        f = _filler(platform="LinkedIn", resume_text="x")
        result = f._match_contextual(
            "how did you hear about this job?", _text_field("x"),
        )
        assert result == "LinkedIn"
        f.router.complete.assert_not_called()

    def test_previously_worked_beats_llm(self):
        f = _filler(resume_text="worked at Acme", company="Acme")
        result = f._match_contextual(
            "have you previously worked for this company?", _text_field("x"),
        )
        assert result == "Yes"
        f.router.complete.assert_not_called()
