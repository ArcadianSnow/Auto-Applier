"""Tests for dashboard runtime-visibility helpers.

Covers the static format helpers added for the title-bar status
pill, footer cooldown countdown, and "Last activity Xs ago" stamp.
These exist so a 60-180s post-apply quiet window can't be confused
with a hang. Helpers are pure functions, so we don't need a Tk root
to exercise them.
"""

import pytest

from auto_applier.gui.dashboard import _format_age, _format_countdown


class TestFormatAge:
    """`_format_age` powers the "Last activity Xs ago" footer label."""

    def test_below_5s_reads_as_just_now(self):
        # The 1Hz ticker would otherwise jitter between 0s/1s and look
        # broken; collapse the early window into a stable "just now".
        assert _format_age(0) == "just now"
        assert _format_age(2) == "just now"
        assert _format_age(4.9) == "just now"

    def test_under_a_minute_shows_seconds(self):
        assert _format_age(5) == "5s ago"
        assert _format_age(45) == "45s ago"
        assert _format_age(59) == "59s ago"

    def test_minutes_and_seconds_above_60s(self):
        assert _format_age(60) == "1m 0s ago"
        assert _format_age(125) == "2m 5s ago"
        assert _format_age(599) == "9m 59s ago"

    def test_hours_and_minutes_above_3600s(self):
        # Exact-seconds detail isn't meaningful at hour-scale gaps.
        assert _format_age(3600) == "1h 0m ago"
        assert _format_age(3700) == "1h 1m ago"
        assert _format_age(7325) == "2h 2m ago"

    def test_negative_input_clamped_to_zero(self):
        # Monotonic clock drift shouldn't produce a negative age render.
        assert _format_age(-1) == "just now"


class TestFormatCountdown:
    """`_format_countdown` powers the cooldown remaining-time label."""

    def test_zero_returns_empty(self):
        # Empty string hides the label, which is the contract the 1Hz
        # ticker relies on to drop the cooldown line once the timer
        # has run out.
        assert _format_countdown(0) == ""
        assert _format_countdown(-5) == ""

    def test_under_a_minute_shows_seconds_remaining(self):
        assert _format_countdown(1) == "1s remaining"
        assert _format_countdown(45) == "45s remaining"

    def test_minutes_and_seconds_remaining(self):
        assert _format_countdown(125) == "2m 5s remaining"
        assert _format_countdown(60) == "1m 0s remaining"

    def test_hours_and_minutes_remaining(self):
        assert _format_countdown(3700) == "1h 1m remaining"
