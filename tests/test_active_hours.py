"""Tests for the active-hours window helper."""
from datetime import datetime

import pytest

from auto_applier.orchestrator.active_hours import (
    ActiveHours,
    parse_active_hours,
)


class TestParse:
    def test_basic(self):
        w = parse_active_hours("09:00-17:00")
        assert w.start.hour == 9 and w.start.minute == 0
        assert w.end.hour == 17 and w.end.minute == 0

    def test_empty_is_always_on(self):
        assert parse_active_hours("").always_on is True

    def test_malformed_falls_back_to_always_on(self):
        assert parse_active_hours("not-a-time").always_on is True

    def test_whitespace_tolerated(self):
        w = parse_active_hours(" 09:00 - 22:00 ")
        assert w.start.hour == 9
        assert w.end.hour == 22


class TestSameDayWindow:
    def setup_method(self):
        self.w = parse_active_hours("09:00-22:00")

    def test_active_at_open(self):
        assert self.w.is_active(datetime(2026, 4, 15, 9, 0)) is True

    def test_active_mid_day(self):
        assert self.w.is_active(datetime(2026, 4, 15, 14, 30)) is True

    def test_inactive_just_before_open(self):
        assert self.w.is_active(datetime(2026, 4, 15, 8, 59)) is False

    def test_inactive_at_close(self):
        """Closing edge is exclusive — 22:00 is outside 09:00-22:00."""
        assert self.w.is_active(datetime(2026, 4, 15, 22, 0)) is False

    def test_inactive_late_night(self):
        assert self.w.is_active(datetime(2026, 4, 15, 2, 0)) is False

    def test_seconds_until_open_while_active_is_zero(self):
        assert self.w.seconds_until_open(datetime(2026, 4, 15, 14, 0)) == 0

    def test_seconds_until_open_same_day(self):
        # At 06:00, next open is 09:00 = 3h = 10800s
        secs = self.w.seconds_until_open(datetime(2026, 4, 15, 6, 0))
        assert secs == 3 * 3600

    def test_seconds_until_open_wraps_to_next_day(self):
        # At 23:00, next open is 09:00 tomorrow = 10h = 36000s
        secs = self.w.seconds_until_open(datetime(2026, 4, 15, 23, 0))
        assert secs == 10 * 3600


class TestOvernightWindow:
    def setup_method(self):
        self.w = parse_active_hours("22:00-06:00")

    def test_active_after_open(self):
        assert self.w.is_active(datetime(2026, 4, 15, 23, 0)) is True

    def test_active_before_close(self):
        assert self.w.is_active(datetime(2026, 4, 15, 5, 30)) is True

    def test_inactive_mid_day(self):
        assert self.w.is_active(datetime(2026, 4, 15, 14, 0)) is False

    def test_seconds_until_open_during_closed_period(self):
        # At 10:00, next open is 22:00 same day = 12h
        secs = self.w.seconds_until_open(datetime(2026, 4, 15, 10, 0))
        assert secs == 12 * 3600


class TestAlwaysOn:
    def test_always_active(self):
        w = parse_active_hours("")
        now = datetime(2026, 4, 15, 3, 0)
        assert w.is_active(now) is True
        assert w.seconds_until_open(now) == 0
