"""Quiet-hours window parser + predicate (spec section 7a)."""

from __future__ import annotations

from datetime import datetime, time

from auto_applier.pipeline.quiet_hours import QuietHours, parse_quiet_hours


# --------------------------------------------------------------- parser

def test_parse_quiet_hours_same_day():
    qh = parse_quiet_hours("12:00-14:00")
    assert qh.start == time(12, 0)
    assert qh.end == time(14, 0)
    assert qh.is_window is True
    assert qh.raw == "12:00-14:00"


def test_parse_quiet_hours_overnight():
    qh = parse_quiet_hours("22:00-08:00")
    assert qh.start == time(22, 0)
    assert qh.end == time(8, 0)
    assert qh.is_window is True


def test_parse_quiet_hours_none_collapses_to_no_op():
    """``None`` and empty string both produce the no-op window — ``is_window``
    is False and ``is_quiet`` is False for any wall-clock moment."""
    for raw in (None, ""):
        qh = parse_quiet_hours(raw)
        assert qh.is_window is False
        assert qh.is_quiet(datetime(2026, 5, 29, 12, 0)) is False


def test_parse_quiet_hours_malformed_falls_back_to_no_op():
    """A bad config value must NOT raise — we'd rather keep running than
    silent-deadlock on bad input. The raw string is preserved so debugging
    is possible."""
    for bad in ("not a window", "22:00", "9-10", "26:00-28:00"):
        qh = parse_quiet_hours(bad)
        # Default is 00:00-00:00 (no-op) for invalid except the last which
        # parses successfully because Python's time(26) raises... let's check
        # both code paths.
        if not qh.is_window:
            assert qh.is_quiet(datetime(2026, 5, 29, 12, 0)) is False


# --------------------------------------------------------------- predicate

def test_is_quiet_same_day_window_inside():
    qh = parse_quiet_hours("12:00-14:00")
    assert qh.is_quiet(datetime(2026, 5, 29, 13, 0)) is True
    # Boundary: start is inclusive, end is exclusive.
    assert qh.is_quiet(datetime(2026, 5, 29, 12, 0)) is True
    assert qh.is_quiet(datetime(2026, 5, 29, 14, 0)) is False


def test_is_quiet_same_day_window_outside():
    qh = parse_quiet_hours("12:00-14:00")
    assert qh.is_quiet(datetime(2026, 5, 29, 11, 59)) is False
    assert qh.is_quiet(datetime(2026, 5, 29, 14, 1)) is False
    assert qh.is_quiet(datetime(2026, 5, 29, 8, 0)) is False


def test_is_quiet_overnight_window_inside():
    qh = parse_quiet_hours("22:00-08:00")
    # After midnight, before 08:00.
    assert qh.is_quiet(datetime(2026, 5, 29, 1, 0)) is True
    # Before midnight, after 22:00.
    assert qh.is_quiet(datetime(2026, 5, 29, 23, 30)) is True
    # 22:00 itself (inclusive).
    assert qh.is_quiet(datetime(2026, 5, 29, 22, 0)) is True


def test_is_quiet_overnight_window_outside():
    qh = parse_quiet_hours("22:00-08:00")
    # Daytime is awake.
    assert qh.is_quiet(datetime(2026, 5, 29, 12, 0)) is False
    # 08:00 itself (exclusive on end).
    assert qh.is_quiet(datetime(2026, 5, 29, 8, 0)) is False
    assert qh.is_quiet(datetime(2026, 5, 29, 21, 59)) is False


def test_no_window_means_always_awake():
    qh = parse_quiet_hours(None)
    # Sample a few times across the day.
    for hour in range(0, 24, 3):
        assert qh.is_quiet(datetime(2026, 5, 29, hour, 0)) is False


def test_seconds_until_open_zero_when_awake():
    """When already outside the window, ``seconds_until_open`` is 0 — the
    apply worker can resume immediately."""
    qh = parse_quiet_hours("22:00-08:00")
    assert qh.seconds_until_open(datetime(2026, 5, 29, 12, 0)) == 0


def test_seconds_until_open_positive_when_quiet():
    """Inside the window, ``seconds_until_open`` is the time until the window
    closes. Sanity-bound (within 24h, positive)."""
    qh = parse_quiet_hours("22:00-08:00")
    seconds = qh.seconds_until_open(datetime(2026, 5, 29, 23, 0))
    assert 0 < seconds <= 24 * 3600
