"""Tests for the Nodriver-backed LinkedIn discovery adapter.

Nodriver is an optional dependency, so these tests must work whether
or not the package is installed locally. Strategy:

  - Mock ``nodriver`` at the import level via sys.modules injection
    so the adapter can be exercised without the real package.
  - Test the scaffolding contract: lazy import, ImportError remediation,
    discovery_only flag, registry presence, doctor preflight gating,
    JSON card-parsing path with a mocked tab.evaluate.

We don't (and can't) test real LinkedIn HTML resilience here — that
requires a live login. The adapter docstring explicitly flags this
as scaffolding pending real-world validation.
"""
from __future__ import annotations

import asyncio
import json as _json
import sys
import types
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from auto_applier.browser.platforms.linkedin_nodriver import (
    LinkedInNodriverPlatform,
)
from auto_applier.storage.models import Job


def _run(coro):
    return asyncio.run(coro)


def _make_platform(config: dict | None = None):
    ctx = MagicMock()
    return LinkedInNodriverPlatform(context=ctx, config=config or {})


# ----------------------------------------------------------------------
# Registry + scaffolding contract
# ----------------------------------------------------------------------

class TestRegistration:
    def test_registered_under_namespaced_key(self):
        from auto_applier.browser.platforms import PLATFORM_REGISTRY
        assert "linkedin_nodriver" in PLATFORM_REGISTRY
        # Existing patchright LinkedIn adapter must still be there
        # — they're parallel implementations, not replacements.
        assert "linkedin" in PLATFORM_REGISTRY

    def test_discovery_only_flag(self):
        platform = _make_platform()
        assert platform.discovery_only is True
        assert platform.discovery_only_reason

    def test_apply_returns_failure_with_manual_apply_flag(self):
        platform = _make_platform()
        job = Job(
            job_id="linkedin_nd_123", title="t", company="c",
            url="https://linkedin.com", description="",
        )

        async def do():
            return await platform.apply_to_job(job, "/path/resume.pdf")

        result = _run(do())
        assert result.success is False
        assert result.requires_manual_apply is True


# ----------------------------------------------------------------------
# Optional-dependency contract
# ----------------------------------------------------------------------

class TestOptionalDependency:
    def test_ensure_logged_in_returns_false_when_nodriver_missing(self):
        platform = _make_platform()
        with patch(
            "auto_applier.browser.platforms.linkedin_nodriver.is_nodriver_available",
            return_value=False,
        ):
            async def do():
                return await platform.ensure_logged_in()
            assert _run(do()) is False

    def test_session_start_raises_clear_error_when_missing(self):
        """If nodriver isn't installed, NodriverSession.start() must
        raise ImportError with the install hint, not crash with a
        cryptic AttributeError. Critical — friends without the dep
        installed must get an actionable message, not a stack trace.
        """
        from auto_applier.browser.nodriver_session import NodriverSession

        # Simulate import failure by removing nodriver from sys.modules
        # cache (if present) and patching __import__ to raise.
        original_modules = dict(sys.modules)
        if "nodriver" in sys.modules:
            del sys.modules["nodriver"]
        original_import = __builtins__["__import__"] if isinstance(
            __builtins__, dict
        ) else __builtins__.__import__

        def fake_import(name, *args, **kwargs):
            if name == "nodriver":
                raise ImportError("No module named 'nodriver'")
            return original_import(name, *args, **kwargs)

        try:
            with patch("builtins.__import__", side_effect=fake_import):
                session = NodriverSession()

                async def do():
                    await session.start()

                with pytest.raises(ImportError) as exc_info:
                    _run(do())
                # Must mention how to install
                assert "nodriver" in str(exc_info.value).lower()
                assert "install" in str(exc_info.value).lower()
        finally:
            # Restore module cache
            sys.modules.clear()
            sys.modules.update(original_modules)


# ----------------------------------------------------------------------
# Card-parsing
# ----------------------------------------------------------------------

class TestCardParsing:
    """The _parse_card method takes the JSON output of the in-page
    JS extractor and produces a Job. Test resilience to missing
    fields and the namespaced-id contract."""

    def test_full_card(self):
        platform = _make_platform()
        raw = {
            "title": "Senior Backend Engineer",
            "company": "Acme",
            "url": "https://www.linkedin.com/jobs/view/3987654321/",
            "jobId": "3987654321",
        }
        job = platform._parse_card(raw, keyword="python")
        assert job is not None
        assert job.title == "Senior Backend Engineer"
        assert job.company == "Acme"
        assert job.job_id == "linkedin_nd_3987654321"
        assert job.source == "linkedin_nodriver"
        assert job.search_keyword == "python"

    def test_missing_jobid_extracted_from_url(self):
        """LinkedIn sometimes omits data-occludable-job-id; fall back
        to parsing the numeric id from the /jobs/view/<n>/ URL."""
        platform = _make_platform()
        raw = {
            "title": "Eng",
            "company": "Co",
            "url": "https://www.linkedin.com/jobs/view/1234567/?ref=feed",
            "jobId": "",
        }
        job = platform._parse_card(raw, keyword="")
        assert job is not None
        assert job.job_id == "linkedin_nd_1234567"

    def test_missing_title_returns_none(self):
        """A card with no title is unusable for scoring. Drop silently."""
        platform = _make_platform()
        raw = {"title": "", "company": "Co", "url": "https://x"}
        assert platform._parse_card(raw, keyword="") is None

    def test_missing_company_returns_none(self):
        platform = _make_platform()
        raw = {"title": "Eng", "company": "", "url": "https://x"}
        assert platform._parse_card(raw, keyword="") is None

    def test_no_jobid_anywhere_returns_none(self):
        """Without a stable job_id we can't dedup. Drop the card."""
        platform = _make_platform()
        raw = {"title": "Eng", "company": "Co", "url": "https://example.com"}
        assert platform._parse_card(raw, keyword="") is None


# ----------------------------------------------------------------------
# Search URL construction
# ----------------------------------------------------------------------

class TestSearchUrl:
    def test_url_encoding(self):
        platform = _make_platform()
        url = platform._build_search_url("data engineer", "Austin, TX")
        assert "keywords=data+engineer" in url
        assert "Austin" in url
        assert url.startswith("https://www.linkedin.com/jobs/search/")

    def test_empty_args(self):
        platform = _make_platform()
        url = platform._build_search_url("", "")
        # Empty params are still included — LinkedIn accepts them.
        assert "keywords=" in url
        assert "location=" in url


# ----------------------------------------------------------------------
# Doctor preflight integration
# ----------------------------------------------------------------------

class TestDoctorIntegration:
    def test_pass_when_not_enabled(self, tmp_path):
        """If linkedin_nodriver isn't in enabled_platforms, the
        doctor check is irrelevant — should PASS silently. Don't
        spam users who never opted in."""
        from auto_applier import doctor

        cfg_file = tmp_path / "user_config.json"
        cfg_file.write_text(_json.dumps({
            "enabled_platforms": ["indeed"],
        }), encoding="utf-8")
        with patch("auto_applier.config.USER_CONFIG_FILE", cfg_file):
            result = doctor.check_nodriver()
        assert result.status == doctor.PASS
        assert "not enabled" in result.message.lower()

    def test_fail_when_enabled_but_not_installed(self, tmp_path):
        """If user enabled linkedin_nodriver but nodriver isn't
        installed, doctor must FAIL with an actionable fix line.
        This is the path that prevents friends from getting a silent
        ImportError mid-run."""
        from auto_applier import doctor

        cfg_file = tmp_path / "user_config.json"
        cfg_file.write_text(_json.dumps({
            "enabled_platforms": ["linkedin_nodriver"],
        }), encoding="utf-8")

        # Force ImportError when nodriver is imported inside the check.
        original_import = __builtins__["__import__"] if isinstance(
            __builtins__, dict
        ) else __builtins__.__import__

        def fake_import(name, *args, **kwargs):
            if name == "nodriver":
                raise ImportError("No module named 'nodriver'")
            return original_import(name, *args, **kwargs)

        original_modules = dict(sys.modules)
        if "nodriver" in sys.modules:
            del sys.modules["nodriver"]
        try:
            with patch("auto_applier.config.USER_CONFIG_FILE", cfg_file), \
                 patch("builtins.__import__", side_effect=fake_import):
                result = doctor.check_nodriver()
            assert result.status == doctor.FAIL
            assert "nodriver" in result.message.lower()
            assert result.fix
            assert "install" in result.fix.lower()
        finally:
            sys.modules.clear()
            sys.modules.update(original_modules)
