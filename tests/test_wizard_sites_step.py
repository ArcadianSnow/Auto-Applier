"""Tests for the SitesStep wizard wiring.

The step is heavily Tk-coupled (text widgets, threaded probes,
toggle handlers), but the data-flow pieces are well-extractable:

  - Wizard data dict has the right BooleanVar / StringVar entries.
  - get_config() round-trips ATS slugs and engine choices into the
    correct config shape.
  - The Test-slug URL builder picks the right ATS endpoint.
  - The job-count parser handles each ATS's response shape.
  - _pip_install_nodriver returns sensible (ok, msg) tuples on
    pip success, pip failure, and missing-pip cases.

Tk-dependent paths (the threaded probe, the install button) are
NOT exercised here — they need a live mainloop and would flake in
parallel test runs. They're guarded structurally by the module-
import test below: if SitesStep can't be imported, every other
test in the suite would also break, so the import guard is the
canary.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


def test_sites_module_imports_clean():
    """If SitesStep module fails to import (typo, missing dep, etc.)
    the whole wizard breaks silently in production. Catch it here."""
    from auto_applier.gui.steps import sites  # noqa: F401


# ----------------------------------------------------------------------
# Probe URL + job-count helpers (pure functions, no Tk)
# ----------------------------------------------------------------------

class TestAtsProbeUrl:
    def test_greenhouse(self):
        from auto_applier.gui.steps.sites import SitesStep
        url = SitesStep._ats_probe_url("greenhouse", "stripe")
        assert "boards-api.greenhouse.io" in url
        assert "/stripe/" in url

    def test_lever(self):
        from auto_applier.gui.steps.sites import SitesStep
        url = SitesStep._ats_probe_url("lever", "netflix")
        assert "api.lever.co" in url
        assert "/netflix" in url
        assert "mode=json" in url

    def test_ashby(self):
        from auto_applier.gui.steps.sites import SitesStep
        url = SitesStep._ats_probe_url("ashby", "openai")
        assert "api.ashbyhq.com" in url
        assert "/openai" in url

    def test_unknown_ats(self):
        from auto_applier.gui.steps.sites import SitesStep
        assert SitesStep._ats_probe_url("workable", "x") == ""


class TestAtsJobCountParse:
    def test_greenhouse_dict_with_jobs(self):
        from auto_applier.gui.steps.sites import SitesStep
        resp = MagicMock()
        resp.json.return_value = {"jobs": [{"id": 1}, {"id": 2}, {"id": 3}]}
        assert SitesStep._count_jobs_in_response("greenhouse", resp) == 3

    def test_lever_top_level_list(self):
        from auto_applier.gui.steps.sites import SitesStep
        resp = MagicMock()
        resp.json.return_value = [{"id": "a"}, {"id": "b"}]
        assert SitesStep._count_jobs_in_response("lever", resp) == 2

    def test_ashby_dict_with_jobs(self):
        from auto_applier.gui.steps.sites import SitesStep
        resp = MagicMock()
        resp.json.return_value = {"jobs": [{"id": "x"}]}
        assert SitesStep._count_jobs_in_response("ashby", resp) == 1

    def test_malformed_returns_negative_one(self):
        """Per the docstring contract, 'couldn't parse' returns -1
        rather than misreporting zero. The wizard's status label
        treats negative as 'unknown'."""
        from auto_applier.gui.steps.sites import SitesStep
        resp = MagicMock()
        resp.json.side_effect = ValueError("bad json")
        assert SitesStep._count_jobs_in_response("greenhouse", resp) == -1

    def test_unexpected_top_level_shape(self):
        from auto_applier.gui.steps.sites import SitesStep
        resp = MagicMock()
        resp.json.return_value = "I am not a dict or list"
        assert SitesStep._count_jobs_in_response("greenhouse", resp) == -1


# ----------------------------------------------------------------------
# pip-install-nodriver wrapper
# ----------------------------------------------------------------------

class TestPipInstallNodriver:
    def test_returns_ok_on_success(self):
        from auto_applier.gui.steps.sites import _pip_install_nodriver
        with patch("subprocess.run") as run:
            run.return_value = MagicMock(returncode=0, stderr="")
            ok, msg = _pip_install_nodriver()
        assert ok is True
        assert msg

    def test_returns_failure_with_pip_error_tail(self):
        from auto_applier.gui.steps.sites import _pip_install_nodriver
        with patch("subprocess.run") as run:
            run.return_value = MagicMock(
                returncode=1,
                stderr="ERROR: Could not find a version that satisfies\n"
                       "ERROR: No matching distribution found",
            )
            ok, msg = _pip_install_nodriver()
        assert ok is False
        assert "matching distribution" in msg.lower()

    def test_handles_missing_pip(self):
        from auto_applier.gui.steps.sites import _pip_install_nodriver
        with patch("subprocess.run", side_effect=FileNotFoundError()):
            ok, msg = _pip_install_nodriver()
        assert ok is False
        assert "pip" in msg.lower()
        assert "manual" in msg.lower() or "readme" in msg.lower()

    def test_handles_timeout(self):
        from auto_applier.gui.steps.sites import _pip_install_nodriver
        import subprocess
        with patch(
            "subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="pip", timeout=180),
        ):
            ok, msg = _pip_install_nodriver()
        assert ok is False
        assert "timed out" in msg.lower()


# ----------------------------------------------------------------------
# Wizard config round-trip
# ----------------------------------------------------------------------

class TestWizardConfigRoundtrip:
    """Without Tk we can't instantiate the wizard, but we CAN
    exercise the data-dict mutation + config-builder logic by
    monkey-patching the BooleanVar / StringVar surface. The wizard
    code only needs ``.get()`` and ``.set()``."""

    def _make_var(self, value):
        var = MagicMock()
        var.get.return_value = value
        return var

    def test_get_config_serializes_ats_slugs(self):
        """Newline-separated text in ats_<id>_slugs becomes the
        ats_api_companies dict. Empty entries dropped, whitespace
        stripped."""
        from auto_applier.gui import wizard as wiz_mod

        # Build a fake wizard with just the data dict + the
        # methods get_config needs. We can't import Tk, so mock
        # everything else.
        wiz = MagicMock(spec=wiz_mod.WizardApp)
        wiz.data = {
            "linkedin_enabled": self._make_var(False),
            "linkedin_nodriver_enabled": self._make_var(True),
            "indeed_enabled": self._make_var(True),
            "dice_enabled": self._make_var(False),
            "ziprecruiter_enabled": self._make_var(False),
            "ats_greenhouse_enabled": self._make_var(True),
            "ats_lever_enabled": self._make_var(True),
            "ats_ashby_enabled": self._make_var(False),
            "ats_greenhouse_slugs": self._make_var(
                "stripe\nairbnb\n  github  \n\n"
            ),
            "ats_lever_slugs": self._make_var("netflix"),
            "ats_ashby_slugs": self._make_var(""),
            "search_keywords": self._make_var("data engineer"),
            "location": self._make_var("Remote"),
            "max_applications_per_day": self._make_var(10),
            "auto_apply_min": self._make_var(7),
            "cli_auto_apply_min": self._make_var(7),
            "review_min": self._make_var(4),
            "ollama_model": self._make_var("gemma4:e4b"),
            "gemini_api_key": self._make_var(""),
            "continuous_mode": self._make_var(False),
            "continuous_cycle_delay_min": self._make_var(30),
            "continuous_cycle_delay_max": self._make_var(90),
            "continuous_active_hours": self._make_var(""),
            "continuous_max_cycles": self._make_var(0),
            "first_name": self._make_var("Jane"),
            "last_name": self._make_var("Doe"),
            "email": self._make_var("jane@example.com"),
            "phone": self._make_var(""),
            "street_address": self._make_var(""),
            "city": self._make_var(""),
            "state": self._make_var(""),
            "zip_code": self._make_var(""),
            "country": self._make_var("United States"),
            "linkedin_url": self._make_var(""),
            "website": self._make_var(""),
        }
        wiz.resume_list = []

        # Force the on-disk merge step to be a no-op by pointing it
        # at a non-existent file.
        with patch.object(wiz_mod, "USER_CONFIG_FILE", Path("/no/such/file")):
            cfg = wiz_mod.WizardApp.get_config(wiz)

        # Enabled platforms
        assert "linkedin_nodriver" in cfg["enabled_platforms"]
        assert "indeed" in cfg["enabled_platforms"]
        assert "ats_greenhouse" in cfg["enabled_platforms"]
        assert "ats_lever" in cfg["enabled_platforms"]
        assert "linkedin" not in cfg["enabled_platforms"]
        assert "ats_ashby" not in cfg["enabled_platforms"]

        # ATS slugs serialized correctly
        ats = cfg["ats_api_companies"]
        assert ats["greenhouse"] == ["stripe", "airbnb", "github"]
        assert ats["lever"] == ["netflix"]
        # Empty slug list NOT included in the dict (avoids confusing
        # the adapter's "no companies configured" log).
        assert "ashby" not in ats

    def test_load_saved_config_reads_ats_dict_shape(self, tmp_path, monkeypatch):
        """The reverse direction: dict shape on disk → newline-joined
        StringVars in the wizard data dict, ready for the text widget."""
        from auto_applier.gui import wizard as wiz_mod

        cfg_file = tmp_path / "user_config.json"
        cfg_file.write_text(json.dumps({
            "enabled_platforms": ["ats_greenhouse"],
            "ats_api_companies": {
                "greenhouse": ["stripe", "github"],
                "lever": [],
                "ashby": ["openai"],
            },
        }), encoding="utf-8")

        monkeypatch.setattr(wiz_mod, "USER_CONFIG_FILE", cfg_file)

        # Build a fake wizard object — same trick as above.
        wiz = MagicMock(spec=wiz_mod.WizardApp)
        wiz.data = {}
        # Pre-create the StringVar slots the loader expects.
        for ats in ("greenhouse", "lever", "ashby"):
            v = MagicMock()
            v.set = MagicMock()
            wiz.data[f"ats_{ats}_slugs"] = v
        for plat in (
            "linkedin", "linkedin_nodriver", "indeed", "dice",
            "ziprecruiter", "ats_greenhouse", "ats_lever", "ats_ashby",
        ):
            v = MagicMock()
            v.set = MagicMock()
            wiz.data[f"{plat}_enabled"] = v

        wiz_mod.WizardApp._load_saved_config(wiz)

        # Greenhouse/Ashby populated; Lever stays empty (no
        # spurious newlines).
        wiz.data["ats_greenhouse_slugs"].set.assert_called_with("stripe\ngithub")
        wiz.data["ats_ashby_slugs"].set.assert_called_with("openai")
        # Lever: set was called with empty string (no slugs).
        wiz.data["ats_lever_slugs"].set.assert_called_with("")
        # ats_greenhouse_enabled flipped to True
        wiz.data["ats_greenhouse_enabled"].set.assert_called_with(True)

    def test_load_saved_config_legacy_list_shape(self, tmp_path, monkeypatch):
        """Hand-edited configs sometimes use the list-of-dicts shape.
        The loader must not lose data on that path."""
        from auto_applier.gui import wizard as wiz_mod

        cfg_file = tmp_path / "user_config.json"
        cfg_file.write_text(json.dumps({
            "ats_api_companies": [
                {"ats": "greenhouse", "company": "stripe"},
                {"ats": "Greenhouse", "company": "airbnb"},  # case
                {"ats": "lever", "company": "netflix"},
                {"ats": "workable", "company": "ignored"},
            ],
        }), encoding="utf-8")
        monkeypatch.setattr(wiz_mod, "USER_CONFIG_FILE", cfg_file)

        wiz = MagicMock(spec=wiz_mod.WizardApp)
        wiz.data = {}
        for ats in ("greenhouse", "lever", "ashby"):
            v = MagicMock()
            v.set = MagicMock()
            wiz.data[f"ats_{ats}_slugs"] = v
        # Pre-create *_enabled vars so the platform-loader path
        # has somewhere to set state.
        for plat in (
            "linkedin", "linkedin_nodriver", "indeed", "dice",
            "ziprecruiter", "ats_greenhouse", "ats_lever", "ats_ashby",
        ):
            v = MagicMock()
            v.set = MagicMock()
            wiz.data[f"{plat}_enabled"] = v

        wiz_mod.WizardApp._load_saved_config(wiz)

        # Both greenhouse entries (case-insensitive ATS match)
        wiz.data["ats_greenhouse_slugs"].set.assert_called_with("stripe\nairbnb")
        wiz.data["ats_lever_slugs"].set.assert_called_with("netflix")
        # ashby: no entries → empty
        wiz.data["ats_ashby_slugs"].set.assert_called_with("")
