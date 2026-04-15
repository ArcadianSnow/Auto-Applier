"""Tests for the discovery-only platform contract.

Discovery-only platforms (LinkedIn today) never navigate to job detail
pages or attempt apply. The engine scores them from title+company and
saves each as a skipped Application so `cli almost` can surface them.
"""
from auto_applier.browser.base_platform import JobPlatform
from auto_applier.browser.platforms.linkedin import LinkedInPlatform
from auto_applier.browser.platforms.indeed import IndeedPlatform
from auto_applier.browser.platforms.dice import DicePlatform
from auto_applier.browser.platforms.ziprecruiter import ZipRecruiterPlatform


class TestDiscoveryOnlyContract:
    def test_base_default_is_false(self):
        """Default: platforms auto-apply (discovery_only=False)."""
        assert JobPlatform.discovery_only is False
        assert JobPlatform.discovery_only_reason == ""

    def test_linkedin_is_discovery_only(self):
        """LinkedIn must stay discovery-only — its anti-automation
        reliably catches direct detail-page navigation."""
        assert LinkedInPlatform.discovery_only is True
        assert LinkedInPlatform.discovery_only_reason
        # The reason text must mention manual apply so users aren't
        # confused about why LinkedIn jobs don't get auto-submitted.
        assert "manual" in LinkedInPlatform.discovery_only_reason.lower()

    def test_auto_apply_platforms_stay_auto(self):
        """Indeed/Dice/ZR validated 3/3 dry-run success — must remain
        auto-apply or we break the core product."""
        assert IndeedPlatform.discovery_only is False
        assert DicePlatform.discovery_only is False
        assert ZipRecruiterPlatform.discovery_only is False
