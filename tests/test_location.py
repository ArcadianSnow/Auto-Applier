"""Location-fit classifier (digest geography preference) — deterministic, no LLM.

Priority ladder (0 best .. 4 worst for the canonical user): remote+target-EU(0),
remote+US/global(1), on-site target-EU(2), remote+other(3), on-site other(4).
"""

from __future__ import annotations

import pytest

from auto_applier.domain.location import classify_location, passes_filter


@pytest.mark.parametrize(
    "loc,expected_priority",
    [
        ("Remote - Netherlands", 0),
        ("Germany (remote)", 0),
        ("Remote, Ireland", 0),
        ("United States (remote)", 1),
        ("Remote, Americas; Remote", 1),
        ("Remote, US", 1),
        ("", 1),                              # blank → optimistic remote/unspecified
        ("Amsterdam, Netherlands", 2),        # target-EU on-site → relocate
        ("Dublin, Ireland", 2),
        ("United Kingdom (remote)", 3),       # remote but non-target geography
        ("Canada (remote)", 3),
        ("Bengaluru, India", 4),              # far-flung on-site → sinks
        ("Seoul, South Korea", 4),
        ("San Francisco", 4),
    ],
)
def test_classify_priority(loc, expected_priority):
    assert classify_location(loc).priority == expected_priority


def test_labels_are_human_readable():
    assert "target EU" in classify_location("Remote - Germany").label
    assert "US" in classify_location("United States (remote)").label


def test_filter_targets_keeps_remote_us_and_target_eu_drops_far_flung():
    keep = ["Remote - Netherlands", "United States (remote)", "Amsterdam, Netherlands"]
    drop = ["Bengaluru, India", "United Kingdom (remote)"]  # onsite-other + remote-other
    for loc in keep:
        assert passes_filter(classify_location(loc), "targets") is True
    for loc in drop:
        assert passes_filter(classify_location(loc), "targets") is False


def test_filter_eu_keeps_only_target_eu():
    assert passes_filter(classify_location("Remote - Germany"), "eu") is True
    assert passes_filter(classify_location("Amsterdam, Netherlands"), "eu") is True
    assert passes_filter(classify_location("United States (remote)"), "eu") is False


def test_filter_remote_keeps_any_remote_drops_onsite():
    assert passes_filter(classify_location("Canada (remote)"), "remote") is True
    assert passes_filter(classify_location("San Francisco"), "remote") is False


def test_filter_all_keeps_everything():
    for loc in ["San Francisco", "Bengaluru, India", "Remote - Netherlands"]:
        assert passes_filter(classify_location(loc), "all") is True
