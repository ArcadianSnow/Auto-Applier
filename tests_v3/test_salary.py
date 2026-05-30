"""Salary intelligence (spec §8d, Phase 6 3/M) — recommendation + parse + comp-filter.

Pure-logic module; no I/O, no network, no LLM. Covers the three-input recommendation
strategy, the posted-range parser's tolerance, the comp-filter gate, and the local-first
default market source.
"""

from __future__ import annotations

import pytest

from av3.resume.salary import (
    NoMarketData,
    SalaryRange,
    format_ask,
    is_below_floor,
    parse_posted_range,
    recommend_ask,
)


# --------------------------------------------------------------- SalaryRange

def test_range_rejects_inverted_or_negative():
    with pytest.raises(ValueError):
        SalaryRange(150_000, 120_000)
    with pytest.raises(ValueError):
        SalaryRange(-1, 10)


def test_range_midpoint():
    assert SalaryRange(100_000, 140_000).midpoint == 120_000


# --------------------------------------------------------------- parsing

@pytest.mark.parametrize("text,expected", [
    ("$120,000 - $150,000", SalaryRange(120_000, 150_000)),
    ("120k-150k", SalaryRange(120_000, 150_000)),
    ("$120K to $150K", SalaryRange(120_000, 150_000)),
    ("USD 120000–150000", SalaryRange(120_000, 150_000)),
    ("$140,000", SalaryRange(140_000, 140_000)),
    ("compensation: 95k", SalaryRange(95_000, 95_000)),
])
def test_parse_posted_range_shapes(text, expected):
    assert parse_posted_range(text) == expected


@pytest.mark.parametrize("text", [
    None, "", "competitive", "DOE", "3-5 years experience", "$15-20/hr",
])
def test_parse_posted_range_rejects_non_salary(text):
    # "$15-20/hr" parses two small magnitudes (<1000, no k) → rejected as non-annual.
    assert parse_posted_range(text) is None


def test_parse_normalizes_reversed_range():
    assert parse_posted_range("150k - 120k") == SalaryRange(120_000, 150_000)


# --------------------------------------------------------------- recommend: posted anchor

def test_posted_range_drives_upper_middle_ask():
    rec = recommend_ask(user_floor=100_000, posted=SalaryRange(120_000, 160_000))
    # upper-middle = 120k + 3/4*40k = 150k
    assert rec is not None
    assert rec.amount == 150_000
    assert rec.basis == "posted"


def test_posted_ask_never_below_user_floor():
    rec = recommend_ask(user_floor=155_000, posted=SalaryRange(120_000, 160_000))
    # upper-middle 150k < floor 155k → floored up to 155k (still <= posted high 160k)
    assert rec.amount == 155_000


def test_posted_ask_never_overshoots_posted_ceiling():
    rec = recommend_ask(user_floor=200_000, posted=SalaryRange(120_000, 160_000))
    # floor 200k > posted high 160k → capped at posted high 160k (don't auto-filter ourselves)
    assert rec.amount == 160_000


# --------------------------------------------------------------- recommend: market anchor

def test_market_used_when_no_posted_range():
    rec = recommend_ask(user_floor=100_000, market=SalaryRange(130_000, 150_000))
    assert rec.amount == 140_000  # market midpoint
    assert rec.basis == "market"


def test_market_ask_floored_at_user_floor():
    rec = recommend_ask(user_floor=160_000, market=SalaryRange(130_000, 150_000))
    assert rec.amount == 160_000
    assert rec.basis == "market"


def test_posted_takes_priority_over_market():
    rec = recommend_ask(
        user_floor=100_000,
        posted=SalaryRange(120_000, 160_000),
        market=SalaryRange(200_000, 250_000),
    )
    assert rec.basis == "posted"


# --------------------------------------------------------------- recommend: user fallback

def test_user_ceiling_when_no_posted_or_market():
    rec = recommend_ask(user_floor=100_000, user_ceiling=130_000)
    assert rec.amount == 130_000
    assert rec.basis == "user"


def test_user_floor_when_only_floor():
    rec = recommend_ask(user_floor=100_000)
    assert rec.amount == 100_000
    assert rec.basis == "user"


def test_no_inputs_returns_none():
    assert recommend_ask(user_floor=None) is None


# --------------------------------------------------------------- comp filter (§8d gate)

def test_is_below_floor_true_when_whole_band_under_floor():
    assert is_below_floor(SalaryRange(80_000, 95_000), 100_000) is True


def test_is_below_floor_false_when_band_overlaps_floor():
    assert is_below_floor(SalaryRange(90_000, 110_000), 100_000) is False


def test_is_below_floor_false_without_posted_range():
    assert is_below_floor(None, 100_000) is False


def test_is_below_floor_false_without_floor():
    assert is_below_floor(SalaryRange(50_000, 60_000), None) is False


# --------------------------------------------------------------- formatting + default source

def test_format_ask():
    rec = recommend_ask(user_floor=140_000)
    assert format_ask(rec) == "$140,000"
    assert format_ask(None) == ""


def test_no_market_data_default_returns_none():
    assert NoMarketData().estimate(title="Data Engineer", location="Remote") is None
