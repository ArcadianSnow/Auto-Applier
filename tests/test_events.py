"""Tests for orchestrator/events.py — EventEmitter pub/sub and blocking events."""

import asyncio

import pytest

from auto_applier.orchestrator.events import (
    EventEmitter,
    RUN_STARTED,
    JOBS_FOUND,
    USER_REVIEW_NEEDED,
    CAPTCHA_DETECTED,
)


class TestEmit:
    def test_handler_receives_kwargs(self):
        emitter = EventEmitter()
        received = {}

        def handler(**data):
            received.update(data)

        emitter.on("test", handler)
        emitter.emit("test", count=5, platform="linkedin")
        assert received == {"count": 5, "platform": "linkedin"}

    def test_multiple_handlers(self):
        emitter = EventEmitter()
        calls = []
        emitter.on("e", lambda **d: calls.append("a"))
        emitter.on("e", lambda **d: calls.append("b"))
        emitter.emit("e")
        assert calls == ["a", "b"]

    def test_unsubscribe(self):
        emitter = EventEmitter()
        calls = []
        handler = lambda **d: calls.append(1)
        emitter.on("e", handler)
        emitter.off("e", handler)
        emitter.emit("e")
        assert calls == []

    def test_off_nonexistent_handler_is_noop(self):
        emitter = EventEmitter()
        emitter.off("e", lambda **d: None)  # should not raise

    def test_emit_unknown_event_is_noop(self):
        emitter = EventEmitter()
        emitter.emit("nonexistent", x=1)  # should not raise

    def test_handler_exception_does_not_propagate(self):
        emitter = EventEmitter()
        calls = []

        def bad(**d):
            raise ValueError("boom")

        def good(**d):
            calls.append("ok")

        emitter.on("e", bad)
        emitter.on("e", good)
        emitter.emit("e")
        assert calls == ["ok"]


class TestEmitAndWait:
    def test_resolve_returns_value(self):
        emitter = EventEmitter()

        async def run():
            async def resolver():
                await asyncio.sleep(0.05)
                emitter.resolve_event("review", "approved")

            asyncio.create_task(resolver())
            return await emitter.emit_and_wait("review", timeout=2.0)

        result = asyncio.run(run())
        assert result == "approved"

    def test_timeout_returns_none(self):
        emitter = EventEmitter()

        async def run():
            return await emitter.emit_and_wait("never_resolved", timeout=0.1)

        result = asyncio.run(run())
        assert result is None

    def test_cleanup_after_resolve(self):
        emitter = EventEmitter()

        async def run():
            async def resolver():
                await asyncio.sleep(0.01)
                emitter.resolve_event("e", "done")

            asyncio.create_task(resolver())
            await emitter.emit_and_wait("e", timeout=1.0)

        asyncio.run(run())
        assert "e" not in emitter._pending_futures
        assert "e" not in emitter._pending_loops

    def test_cleanup_after_timeout(self):
        emitter = EventEmitter()

        async def run():
            await emitter.emit_and_wait("e", timeout=0.05)

        asyncio.run(run())
        assert "e" not in emitter._pending_futures
        assert "e" not in emitter._pending_loops

    def test_resolve_already_done_future_is_noop(self):
        emitter = EventEmitter()

        async def run():
            async def double_resolve():
                await asyncio.sleep(0.01)
                emitter.resolve_event("e", "first")
                emitter.resolve_event("e", "second")  # should not raise

            asyncio.create_task(double_resolve())
            return await emitter.emit_and_wait("e", timeout=1.0)

        result = asyncio.run(run())
        assert result == "first"

    def test_handlers_fire_during_emit_and_wait(self):
        emitter = EventEmitter()
        received = []
        emitter.on("e", lambda **d: received.append(d))

        async def run():
            async def resolver():
                await asyncio.sleep(0.01)
                emitter.resolve_event("e", "ok")

            asyncio.create_task(resolver())
            await emitter.emit_and_wait("e", timeout=1.0, job_id="j1")

        asyncio.run(run())
        assert received == [{"job_id": "j1"}]


class TestResolveNoPending:
    def test_resolve_without_pending_is_noop(self):
        emitter = EventEmitter()
        emitter.resolve_event("nothing", "value")  # should not raise


class TestEventNameConstants:
    def test_standard_event_names_are_strings(self):
        for name in [RUN_STARTED, JOBS_FOUND, USER_REVIEW_NEEDED, CAPTCHA_DETECTED]:
            assert isinstance(name, str)
            assert len(name) > 0
