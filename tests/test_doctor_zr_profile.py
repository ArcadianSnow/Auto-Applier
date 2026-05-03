"""Tests for the ZipRecruiter profile-completeness preflight check.

Background: yesterday's silent-failure root cause was an empty ZR
account profile. Local CSV said "applied" but ZR's dashboard showed
"Application Incomplete" because QuickApply's iframe requires the
underlying account profile to already have contact info saved on
ZR's side. Doctor can't verify the remote state cheaply, so it
proxies on local personal_info completeness.
"""
import json

import pytest

from auto_applier import doctor


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

COMPLETE_PERSONAL_INFO = {
    "name": "Jordan Testpilot",
    "first_name": "Jordan",
    "last_name": "Testpilot",
    "email": "jordan@example.com",
    "phone": "+15555550100",
    "city": "Seattle",
    "state": "WA",
    "zip_code": "98101",
    "country": "United States",
}


def _write_config(tmp_path, monkeypatch, *, enabled_platforms, personal_info):
    f = tmp_path / "user_config.json"
    f.write_text(json.dumps({
        "enabled_platforms": enabled_platforms,
        "personal_info": personal_info,
    }))
    import auto_applier.config as cfg
    monkeypatch.setattr(cfg, "USER_CONFIG_FILE", f)
    return f


# ---------------------------------------------------------------------------
# The three required tests
# ---------------------------------------------------------------------------

class TestZipRecruiterProfile:
    def test_zr_not_enabled_passes(self, tmp_path, monkeypatch):
        """ZR not in enabled_platforms — check is a no-op PASS, no warning."""
        _write_config(
            tmp_path, monkeypatch,
            enabled_platforms=["linkedin", "indeed"],
            personal_info={},  # doesn't matter when ZR is off
        )
        r = doctor.check_ziprecruiter_profile()
        assert r.status == doctor.PASS
        assert "not configured" in r.message
        assert r.fix == ""

    def test_zr_enabled_complete_personal_info_warns(self, tmp_path, monkeypatch):
        """All required local fields populated + ZR enabled —
        WARN telling the user to eyeball ziprecruiter.com because
        we can't verify the remote profile automatically."""
        _write_config(
            tmp_path, monkeypatch,
            enabled_platforms=["linkedin", "ziprecruiter"],
            personal_info=COMPLETE_PERSONAL_INFO,
        )
        r = doctor.check_ziprecruiter_profile()
        assert r.status == doctor.WARN
        assert "ziprecruiter.com" in r.fix.lower()
        assert "could not verify automatically" in r.fix.lower()

    def test_zr_enabled_missing_fields_fails(self, tmp_path, monkeypatch):
        """ZR enabled but personal_info is missing required fields —
        FAIL with the actionable wizard hint. We can't even hope ZR's
        profile is filled in if the user doesn't have the data locally."""
        partial = {
            "name": "Jordan Testpilot",
            "first_name": "Jordan",
            "email": "jordan@example.com",
            # missing: last_name, phone, city, state, zip_code
        }
        _write_config(
            tmp_path, monkeypatch,
            enabled_platforms=["ziprecruiter"],
            personal_info=partial,
        )
        r = doctor.check_ziprecruiter_profile()
        assert r.status == doctor.FAIL
        # Surface every missing field in the message so the user knows
        # exactly what to fix.
        for field in ("last_name", "phone", "city", "state", "zip_code"):
            assert field in r.message
        assert "wizard" in r.fix.lower()
