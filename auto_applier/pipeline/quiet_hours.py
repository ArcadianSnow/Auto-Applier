"""Quiet-hours window for the staged-worker scheduler (spec §7a).

> "The workers just keep running — 24/7 by default, with optional user-set quiet
>  hours, within rate limits."

The quiet-hours window is the *only* configurable break in the always-on
operating model. Inside the window, the **apply** worker stops driving the
browser (so a sleeping user doesn't wake to keyboard chatter on a shared
machine); the discovery/filter/score/optimize stages keep running because
they're "gather" work per Rule 2.6 — being wrong is cheap and doesn't compound.

Both overnight ("22:00-08:00") and same-day ("12:00-14:00") windows work. An
empty / malformed string collapses to "no quiet hours" so a bad config value
doesn't silently park the bot — better to keep running and surface the bad
config than to silently disable the apply worker.

Ported from v2's ``orchestrator/active_hours.py`` with inverted semantics: v2's
``active_hours`` is when the bot runs; v3's ``quiet_hours`` is when the apply
worker does NOT run. The naming flip matches the spec (§7a uses "quiet hours")
and the v3.0 "always-on by default" stance — windows are exceptions, not
permission.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta

__all__ = ["QuietHours", "parse_quiet_hours"]


@dataclass(frozen=True)
class QuietHours:
    """A local-time window during which the apply worker pauses.

    ``start == end`` means "no window" (the no-op default). The predicate
    :meth:`is_quiet` handles both same-day and overnight ranges uniformly.
    """

    start: time
    end: time
    raw: str

    @property
    def is_window(self) -> bool:
        """True when a real window is configured. Default ``00:00-00:00`` (or
        missing config) collapses to False — every wall-clock moment is awake."""
        return self.start != self.end

    def is_quiet(self, now: datetime) -> bool:
        """Is ``now`` inside the quiet window? ``now`` is interpreted as local
        time (``.time()``)."""
        if not self.is_window:
            return False
        cur = now.time()
        if self.start < self.end:
            # Same-day window, e.g. 12:00-14:00.
            return self.start <= cur < self.end
        # Overnight window, e.g. 22:00-08:00.
        return cur >= self.start or cur < self.end

    def seconds_until_open(self, now: datetime) -> int:
        """Seconds until quiet window CLOSES (apply resumes). Zero when
        currently awake. Used by the scheduler to log how long the apply
        worker will be paused without polling every cycle."""
        if not self.is_quiet(now):
            return 0
        today_close = now.replace(
            hour=self.end.hour, minute=self.end.minute,
            second=0, microsecond=0,
        )
        # Close is "tomorrow's end" if start<end is a same-day window we're inside
        # AND we're past the end (shouldn't happen — is_quiet would be False).
        # For overnight (start>end), close is later today; for same-day, close is later today too.
        if today_close <= now:
            today_close = today_close + timedelta(days=1)
        return int((today_close - now).total_seconds())


def parse_quiet_hours(raw: str | None) -> QuietHours:
    """Parse a ``HH:MM-HH:MM`` window. Falls back to no-quiet on error.

    Empty string or None returns the no-op window (``00:00-00:00``); malformed
    input does too (logged via the QuietHours.raw field so config debugging is
    possible). The scheduler treats both identically — "no window configured."
    """
    default = QuietHours(time(0, 0), time(0, 0), raw or "")
    if not raw:
        return default
    try:
        left, right = raw.split("-", 1)
        start_h, start_m = (int(x) for x in left.strip().split(":", 1))
        end_h, end_m = (int(x) for x in right.strip().split(":", 1))
        return QuietHours(time(start_h, start_m), time(end_h, end_m), raw)
    except (ValueError, AttributeError):
        return default
