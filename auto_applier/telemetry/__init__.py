"""Telemetry: local event sink (always on) + scrubber (for the opt-in remote mirror).

A process-global sink lets the ``@stage`` wrapper emit without threading a sink
reference through every call. Configure once at startup with :func:`configure_sink`;
tests point it at a temp ``events.db``.
"""

from __future__ import annotations

from auto_applier.telemetry.mirror import (
    MirrorPolicy,
    MirrorQueue,
    QueuedMirrorRow,
    user_id_from_handle,
)
from auto_applier.telemetry.scrub import (
    scrub,
    scrub_error_event,
    scrub_inferred_answer_event,
)
from auto_applier.telemetry.sink import EventSink

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


def attach_mirror_from_settings(sink: EventSink, settings) -> MirrorPolicy:
    """Build a :class:`MirrorPolicy` from ``settings.telemetry`` and attach it.

    Returns the policy for callers that want to introspect it (e.g. the
    ``cli telemetry status`` command in 3/M). Safe to call whether or not
    telemetry is enabled — ``policy.enabled=False`` keeps :meth:`EventSink.emit`
    silent on the mirror path.
    """
    from auto_applier import __version__ as _app_version

    policy = MirrorPolicy.from_settings(settings.telemetry, _app_version)
    sink.attach_mirror(policy)
    return policy


__all__ = [
    "EventSink",
    "MirrorPolicy",
    "MirrorQueue",
    "QueuedMirrorRow",
    "attach_mirror_from_settings",
    "configure_sink",
    "get_sink",
    "reset_sink",
    "scrub",
    "scrub_error_event",
    "scrub_inferred_answer_event",
    "user_id_from_handle",
]
