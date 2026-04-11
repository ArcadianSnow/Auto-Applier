"""Regression tests for EventEmitter cross-thread resolve.

The real-world bug: GUI callbacks on the Tk main thread called
future.set_result() to unblock the engine's asyncio loop running
on a background thread. Python asyncio futures aren't thread-safe,
so the set_result was silently ignored and the engine hung until
timeout. resolve_event now uses call_soon_threadsafe on the
captured loop, which is the documented cross-thread pattern.
"""
import asyncio
import threading
import time

import pytest

from auto_applier.orchestrator.events import EventEmitter


class TestResolveEventSameThread:
    def test_basic_resolve(self):
        """Resolving from the same thread still works."""
        emitter = EventEmitter()

        async def main():
            async def resolver():
                await asyncio.sleep(0.05)
                emitter.resolve_event("test", "ok")
            asyncio.create_task(resolver())
            result = await emitter.emit_and_wait("test", timeout=2.0)
            return result

        assert asyncio.run(main()) == "ok"

    def test_timeout_returns_none(self):
        emitter = EventEmitter()

        async def main():
            return await emitter.emit_and_wait("nothing", timeout=0.1)

        assert asyncio.run(main()) is None


class TestResolveEventCrossThread:
    """The hang case: resolve_event called from a different thread
    than the asyncio loop. Must work without deadlocking."""

    def test_resolve_from_other_thread(self):
        emitter = EventEmitter()
        resolved_from_thread = threading.Event()

        async def main():
            def thread_resolver():
                # Simulate a Tk after() callback on the GUI main thread
                time.sleep(0.1)
                emitter.resolve_event("cross", "from-thread")
                resolved_from_thread.set()

            t = threading.Thread(target=thread_resolver, daemon=True)
            t.start()

            result = await emitter.emit_and_wait("cross", timeout=3.0)
            return result

        result = asyncio.run(main())
        assert result == "from-thread"
        assert resolved_from_thread.is_set()

    def test_resolve_dict_payload(self):
        """Batch review passes a dict of decisions through the event."""
        emitter = EventEmitter()

        async def main():
            def resolver():
                time.sleep(0.05)
                emitter.resolve_event("review", {"job-1": "apply", "job-2": "skip"})
            threading.Thread(target=resolver, daemon=True).start()
            return await emitter.emit_and_wait("review", timeout=3.0)

        result = asyncio.run(main())
        assert result == {"job-1": "apply", "job-2": "skip"}

    def test_resolve_after_timeout_is_noop(self):
        """Resolving after the await has already timed out must not crash."""
        emitter = EventEmitter()

        async def main():
            result = await emitter.emit_and_wait("late", timeout=0.1)
            # Now resolve after the fact — the key is gone from the
            # pending dict, so this should be a quiet no-op.
            emitter.resolve_event("late", "too late")
            return result

        assert asyncio.run(main()) is None  # the original timeout
