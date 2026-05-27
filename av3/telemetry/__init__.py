"""Telemetry: local event sink (always on) + scrubber (for the opt-in remote mirror).

A process-global sink lets the ``@stage`` wrapper emit without threading a sink
reference through every call. Configure once at startup with :func:`configure_sink`;
tests point it at a temp ``events.db``.
"""

from __future__ import annotations

from av3.telemetry.scrub import scrub
from av3.telemetry.sink import EventSink

_sink: EventSink | None = None


def configure_sink(sink: EventSink) -> EventSink:
    """Install the process-global event sink. Returns it for convenience."""
    global _sink
    _sink = sink
    return sink


def get_sink() -> EventSink | None:
    """The installed sink, or ``None`` if telemetry isn't configured yet.

    ``@stage`` treats ``None`` as "drop events" so domain/unit tests that never
    configure a sink still run without I/O.
    """
    return _sink


def reset_sink() -> None:
    """Close + clear the global sink (test teardown)."""
    global _sink
    if _sink is not None:
        _sink.close()
    _sink = None


__all__ = ["EventSink", "configure_sink", "get_sink", "reset_sink", "scrub"]
