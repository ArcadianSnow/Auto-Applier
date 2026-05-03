"""Tests for the Camoufox optional anti-detect backend scaffold.

Phase 1 scaffold (2026-05-03). Camoufox is an optional dependency
that's not yet wired into any platform adapter — these tests verify
the integration shape holds together so we're ready when Camoufox
cuts a stable release.

Strategy mirrors test_linkedin_nodriver: confirm the module imports
clean even without the optional dep installed, the install hint
fires correctly, and the doctor preflight reports the right status.
"""
from __future__ import annotations

import asyncio
import sys
from unittest.mock import MagicMock, patch

import pytest


def test_camoufox_session_module_imports_clean():
    """Module-import canary. If CamoufoxSession's import surface is
    broken (typo, syntax error, missing helper), wizard / engine /
    every platform adapter fails at collection. Catch it here."""
    from auto_applier.browser import camoufox_session  # noqa: F401


class TestOptionalDependencyContract:
    def test_is_camoufox_available_returns_bool(self):
        from auto_applier.browser.camoufox_session import (
            is_camoufox_available,
        )
        # Whether or not camoufox is installed, the function should
        # always return a clean boolean.
        result = is_camoufox_available()
        assert isinstance(result, bool)

    def test_session_start_raises_clear_error_when_camoufox_missing(self):
        """Identical contract to NodriverSession: if the optional
        package isn't installed, start() raises ImportError with the
        actionable install hint. No cryptic AttributeError."""
        from auto_applier.browser.camoufox_session import CamoufoxSession

        original_modules = dict(sys.modules)
        # Strip any cached camoufox modules so the import fails fresh.
        for key in list(sys.modules.keys()):
            if key == "camoufox" or key.startswith("camoufox."):
                del sys.modules[key]

        original_import = __builtins__["__import__"] if isinstance(
            __builtins__, dict
        ) else __builtins__.__import__

        def fake_import(name, *args, **kwargs):
            if name == "camoufox" or name.startswith("camoufox."):
                raise ImportError("No module named 'camoufox'")
            return original_import(name, *args, **kwargs)

        try:
            with patch("builtins.__import__", side_effect=fake_import):
                session = CamoufoxSession()

                async def do():
                    await session.start()

                with pytest.raises(ImportError) as exc_info:
                    asyncio.run(do())
                msg = str(exc_info.value).lower()
                assert "camoufox" in msg
                assert "install" in msg
                assert "fetch" in msg  # mentions `camoufox fetch`
                # Acknowledges experimental status so the user
                # knows what they're getting into.
                assert "experimental" in msg or "beta" in msg
        finally:
            sys.modules.clear()
            sys.modules.update(original_modules)


class TestSessionLifecycleSemantics:
    def test_started_property_false_initially(self):
        from auto_applier.browser.camoufox_session import CamoufoxSession
        session = CamoufoxSession()
        assert session.started is False

    def test_stop_without_start_does_not_raise(self):
        """Idempotency: calling stop() on a never-started session
        must be a clean no-op. Engine's stop() depends on this."""
        from auto_applier.browser.camoufox_session import CamoufoxSession
        session = CamoufoxSession()

        async def do():
            await session.stop()

        # No exception
        asyncio.run(do())
        assert session.started is False

    def test_new_tab_raises_clear_error_before_start(self):
        from auto_applier.browser.camoufox_session import CamoufoxSession
        session = CamoufoxSession()

        async def do():
            await session.new_tab("https://example.com")

        with pytest.raises(RuntimeError) as exc_info:
            asyncio.run(do())
        # Caller gets a useful message, not just a None-deref.
        assert "not started" in str(exc_info.value).lower()


class TestProfileDirIsolation:
    """Camoufox profile must be isolated from patchright + nodriver
    so cookies / login state don't cross-contaminate. Each backend
    gets its own subdirectory under data/."""

    def test_camoufox_profile_dir_distinct(self):
        from auto_applier.browser.camoufox_session import (
            CAMOUFOX_PROFILE_DIR,
        )
        from auto_applier.browser.nodriver_session import (
            NODRIVER_PROFILE_DIR,
        )
        from auto_applier.config import BROWSER_PROFILE_DIR

        assert CAMOUFOX_PROFILE_DIR != BROWSER_PROFILE_DIR
        assert CAMOUFOX_PROFILE_DIR != NODRIVER_PROFILE_DIR
        # Both backends share the same parent (data/) so cleanup is
        # easy and the user can find them.
        assert CAMOUFOX_PROFILE_DIR.parent == BROWSER_PROFILE_DIR.parent


class TestDoctorIntegration:
    def test_camoufox_check_returns_pass_in_both_states(self):
        """The doctor's camoufox check is informational only —
        nothing fails if it isn't installed (it's purely scaffolded).
        This is intentional: shipping a FAIL would push every user
        to install an experimental dep just to satisfy preflight."""
        from auto_applier import doctor
        result = doctor.check_camoufox()
        # Whether installed or not, status is PASS
        assert result.status == doctor.PASS
        # Message tells the user what state we found
        msg = result.message.lower()
        assert "camoufox" in msg or "not installed" in msg or "available" in msg
