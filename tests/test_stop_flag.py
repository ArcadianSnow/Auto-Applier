"""Regression tests for the cooperative stop flag.

Before this fix, the dashboard's Stop button only set a flag on
the dashboard object that the engine never read. Clicking Stop
logged 'finishing current action...' and then nothing happened —
the entire rest of the run kept going.

The new flow: engine.request_stop() sets a flag that every natural
loop point checks. The CURRENT job finishes cleanly (mid-form
abort would corrupt state), but nothing new starts after.
"""
from unittest.mock import MagicMock

import pytest

from auto_applier.orchestrator.engine import ApplicationEngine
from auto_applier.orchestrator.events import EventEmitter


def _make_engine():
    """Minimal engine instance for flag-level testing."""
    emitter = EventEmitter()
    config = {
        "enabled_platforms": ["indeed", "dice"],
        "search_keywords": ["a", "b"],
        "location": "remote",
        "personal_info": {},
        "max_applications_per_day": 10,
        "resumes": [],
    }
    return ApplicationEngine(config, emitter, cli_mode=True)


class TestStopFlag:
    def test_fresh_engine_not_stopped(self):
        engine = _make_engine()
        assert engine._stop_requested is False

    def test_request_stop_sets_flag(self):
        engine = _make_engine()
        engine.request_stop()
        assert engine._stop_requested is True

    def test_request_stop_is_idempotent(self):
        engine = _make_engine()
        engine.request_stop()
        engine.request_stop()
        engine.request_stop()
        assert engine._stop_requested is True

    def test_stop_flag_is_instance_level(self):
        """Two engines must have independent stop state."""
        a = _make_engine()
        b = _make_engine()
        a.request_stop()
        assert a._stop_requested is True
        assert b._stop_requested is False
