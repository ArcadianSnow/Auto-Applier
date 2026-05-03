"""Tests for the wizard's skip-to-Ready behavior on subsequent runs.

When ``user_config.json`` already has name + email + enabled_platforms
AND at least one resume is loaded, the wizard's __init__ jumps the
user straight to the Ready step. Saves friends 60+ seconds of
clicking through unchanged config every launch.

We test the predicate (``_wizard_already_completed``) directly so
we don't need a live Tk mainloop. The predicate is the load-bearing
piece — once it returns True, the existing __init__ logic does the
right thing (starts at the last step). Once it returns False, the
existing __init__ logic does the right thing (starts at step 0).

Each test monkeypatches ``USER_CONFIG_FILE``, ``PROFILES_DIR``,
``RESUMES_DIR`` to a tmp_path so we don't poison real user data.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest


def _patch_paths(monkeypatch, tmp_path):
    """Redirect the three paths the predicate reads into tmp_path."""
    from auto_applier import config as cfg_mod
    cfg_file = tmp_path / "user_config.json"
    profiles = tmp_path / "profiles"
    resumes = tmp_path / "resumes"
    profiles.mkdir()
    resumes.mkdir()
    monkeypatch.setattr(cfg_mod, "USER_CONFIG_FILE", cfg_file)
    monkeypatch.setattr(cfg_mod, "PROFILES_DIR", profiles)
    monkeypatch.setattr(cfg_mod, "RESUMES_DIR", resumes)
    return cfg_file, profiles, resumes


def _make_predicate_caller():
    """Bind the unbound predicate so we can call it without Tk init.

    The wizard's __init__ creates Tk widgets, which would require a
    mainloop. We exercise just the predicate via a MagicMock self.
    """
    from auto_applier.gui import wizard as wiz_mod
    wiz = MagicMock(spec=wiz_mod.WizardApp)
    return lambda: wiz_mod.WizardApp._wizard_already_completed(wiz)


# ----------------------------------------------------------------------
# True path — all conditions met
# ----------------------------------------------------------------------

class TestPredicateTrueWhenComplete:
    def test_full_config_with_profile_returns_true(self, tmp_path, monkeypatch):
        cfg_file, profiles, _ = _patch_paths(monkeypatch, tmp_path)
        cfg_file.write_text(json.dumps({
            "personal_info": {
                "first_name": "Jane",
                "last_name": "Doe",
                "email": "jane@example.com",
            },
            "enabled_platforms": ["indeed"],
        }), encoding="utf-8")
        (profiles / "default.json").write_text("{}", encoding="utf-8")

        predicate = _make_predicate_caller()
        assert predicate() is True

    def test_full_config_with_resume_file_only_returns_true(
        self, tmp_path, monkeypatch,
    ):
        """Profile MISSING but raw resume file present — still True.
        ResumesStep materializes profiles on demand, so the user is
        ready in spirit; making them step through the wizard would
        be needless friction."""
        cfg_file, _, resumes = _patch_paths(monkeypatch, tmp_path)
        cfg_file.write_text(json.dumps({
            "personal_info": {"name": "Jane Doe", "email": "j@x.com"},
            "enabled_platforms": ["indeed"],
        }), encoding="utf-8")
        (resumes / "Jane_Doe_Resume.pdf").write_bytes(b"PDF stub")

        predicate = _make_predicate_caller()
        assert predicate() is True

    def test_combined_name_field_accepted(self, tmp_path, monkeypatch):
        """The fixture generator writes 'name' as combined; the wizard
        writes first_name + last_name. Either shape passes."""
        cfg_file, profiles, _ = _patch_paths(monkeypatch, tmp_path)
        cfg_file.write_text(json.dumps({
            "personal_info": {"name": "Jane Doe", "email": "j@x.com"},
            "enabled_platforms": ["indeed"],
        }), encoding="utf-8")
        (profiles / "default.json").write_text("{}", encoding="utf-8")

        predicate = _make_predicate_caller()
        assert predicate() is True


# ----------------------------------------------------------------------
# False path — missing pieces force full wizard flow
# ----------------------------------------------------------------------

class TestPredicateFalseWhenIncomplete:
    def test_no_user_config_returns_false(self, tmp_path, monkeypatch):
        # Don't create the file
        _patch_paths(monkeypatch, tmp_path)
        predicate = _make_predicate_caller()
        assert predicate() is False

    def test_malformed_user_config_returns_false(self, tmp_path, monkeypatch):
        cfg_file, _, _ = _patch_paths(monkeypatch, tmp_path)
        cfg_file.write_text("not valid json", encoding="utf-8")
        predicate = _make_predicate_caller()
        assert predicate() is False

    def test_missing_email_returns_false(self, tmp_path, monkeypatch):
        cfg_file, profiles, _ = _patch_paths(monkeypatch, tmp_path)
        cfg_file.write_text(json.dumps({
            "personal_info": {"name": "Jane Doe"},  # no email
            "enabled_platforms": ["indeed"],
        }), encoding="utf-8")
        (profiles / "default.json").write_text("{}", encoding="utf-8")

        predicate = _make_predicate_caller()
        assert predicate() is False

    def test_missing_name_returns_false(self, tmp_path, monkeypatch):
        cfg_file, profiles, _ = _patch_paths(monkeypatch, tmp_path)
        cfg_file.write_text(json.dumps({
            "personal_info": {"email": "j@x.com"},
            "enabled_platforms": ["indeed"],
        }), encoding="utf-8")
        (profiles / "default.json").write_text("{}", encoding="utf-8")

        predicate = _make_predicate_caller()
        assert predicate() is False

    def test_first_name_alone_returns_false(self, tmp_path, monkeypatch):
        """first_name without last_name doesn't satisfy the name check
        unless 'name' is also set."""
        cfg_file, profiles, _ = _patch_paths(monkeypatch, tmp_path)
        cfg_file.write_text(json.dumps({
            "personal_info": {"first_name": "Jane", "email": "j@x.com"},
            "enabled_platforms": ["indeed"],
        }), encoding="utf-8")
        (profiles / "default.json").write_text("{}", encoding="utf-8")

        predicate = _make_predicate_caller()
        assert predicate() is False

    def test_no_enabled_platforms_returns_false(self, tmp_path, monkeypatch):
        cfg_file, profiles, _ = _patch_paths(monkeypatch, tmp_path)
        cfg_file.write_text(json.dumps({
            "personal_info": {"name": "Jane Doe", "email": "j@x.com"},
            "enabled_platforms": [],
        }), encoding="utf-8")
        (profiles / "default.json").write_text("{}", encoding="utf-8")

        predicate = _make_predicate_caller()
        assert predicate() is False

    def test_no_resumes_returns_false(self, tmp_path, monkeypatch):
        """Config OK but neither a profile nor a resume file present
        — user must complete ResumesStep first."""
        cfg_file, _, _ = _patch_paths(monkeypatch, tmp_path)
        cfg_file.write_text(json.dumps({
            "personal_info": {"name": "Jane Doe", "email": "j@x.com"},
            "enabled_platforms": ["indeed"],
        }), encoding="utf-8")
        # Profiles + resumes dirs exist but are empty

        predicate = _make_predicate_caller()
        assert predicate() is False

    def test_dotfile_in_resumes_does_not_count(self, tmp_path, monkeypatch):
        """Defensive: a stray .DS_Store in resumes/ shouldn't trick
        the predicate into thinking a resume exists."""
        cfg_file, _, resumes = _patch_paths(monkeypatch, tmp_path)
        cfg_file.write_text(json.dumps({
            "personal_info": {"name": "Jane Doe", "email": "j@x.com"},
            "enabled_platforms": ["indeed"],
        }), encoding="utf-8")
        (resumes / ".DS_Store").write_bytes(b"")

        predicate = _make_predicate_caller()
        assert predicate() is False


# ----------------------------------------------------------------------
# Wizard data dict pre-population — module-import canary
# ----------------------------------------------------------------------

class TestWizardImportCanary:
    """If the new predicate has a syntax error or imports something
    nonexistent, the whole wizard module fails to load. Catch that
    structurally."""

    def test_wizard_module_imports(self):
        from auto_applier.gui import wizard  # noqa: F401
