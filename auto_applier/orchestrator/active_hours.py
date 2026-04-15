"""Active-hours window helper for continuous-run mode.

Parses a "HH:MM-HH:MM" local-time string and answers two questions:

- ``is_active(now)`` — are we currently inside the window?
- ``seconds_until_open(now)`` — how long until the next window opens?

Both overnight ("22:00-06:00") and same-day ("09:00-22:00") ranges
work. An empty / malformed string collapses to "always active" so
continuous mode still progresses rather than silent-deadlocking on
a bad config value.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta


@dataclass(frozen=True)
class ActiveHours:
    start: time
    end: time
    raw: str

    @property
    def always_on(self) -> bool:
        return self.start == self.end

    def is_active(self, now: datetime) -> bool:
        if self.always_on:
            return True
        cur = now.time()
        if self.start < self.end:
            # Same-day window, e.g. 09:00-22:00.
            return self.start <= cur < self.end
        # Overnight window, e.g. 22:00-06:00.
        return cur >= self.start or cur < self.end

    def seconds_until_open(self, now: datetime) -> int:
        """Return seconds until the next window open.

        Zero when ``now`` is already inside the window.
        """
        if self.is_active(now):
            return 0
        today_open = now.replace(
            hour=self.start.hour,
            minute=self.start.minute,
            second=0,
            microsecond=0,
        )
        if today_open <= now:
            today_open = today_open + timedelta(days=1)
        return int((today_open - now).total_seconds())


def parse_active_hours(raw: str) -> ActiveHours:
    """Parse a ``HH:MM-HH:MM`` window. Falls back to always-on on error."""
    default = ActiveHours(time(0, 0), time(0, 0), raw or "")
    if not raw:
        return default
    try:
        left, right = raw.split("-", 1)
        start_h, start_m = (int(x) for x in left.strip().split(":", 1))
        end_h, end_m = (int(x) for x in right.strip().split(":", 1))
        return ActiveHours(time(start_h, start_m), time(end_h, end_m), raw)
    except (ValueError, AttributeError):
        return default
