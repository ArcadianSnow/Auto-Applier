"""Tests for the doctor preflight runner."""
import json
from pathlib import Path

import pytest

from auto_applier import doctor


# ---------------------------------------------------------------------------
# CheckResult shape
# ---------------------------------------------------------------------------

class TestCheckResult:
    def test_fields(self):
        r = doctor.CheckResult("name", doctor.PASS, "ok", fix="")
        assert r.status == "PASS"
        assert r.message == "ok"
        assert r.fix == ""


# ---------------------------------------------------------------------------
# Sync checks
# ---------------------------------------------------------------------------

class TestPythonVersion:
    def test_current_interpreter_passes(self):
        r = doctor.check_python_version()
        assert r.status == doctor.PASS


class TestEnvFile:
    def test_env_present(self, tmp_path, monkeypatch):
        env = tmp_path / ".env"
        env.write_text("FOO=bar")
        import auto_applier.config as cfg
        monkeypatch.setattr(cfg, "PROJECT_ROOT", tmp_path)
        r = doctor.check_env_file()
        assert r.status == doctor.PASS

    def test_env_missing_example_present(self, tmp_path, monkeypatch):
        (tmp_path / ".env.example").write_text("# template")
        import auto_applier.config as cfg
        monkeypatch.setattr(cfg, "PROJECT_ROOT", tmp_path)
        r = doctor.check_env_file()
        assert r.status == doctor.WARN
        assert "Copy" in r.fix


class TestUserConfig:
    def test_missing_fails(self, tmp_path, monkeypatch):
        import auto_applier.config as cfg
        monkeypatch.setattr(cfg, "USER_CONFIG_FILE", tmp_path / "nope.json")
        r = doctor.check_user_config()
        assert r.status == doctor.FAIL

    def test_bad_json_fails(self, tmp_path, monkeypatch):
        bad = tmp_path / "user_config.json"
        bad.write_text("{ not json")
        import auto_applier.config as cfg
        monkeypatch.setattr(cfg, "USER_CONFIG_FILE", bad)
        r = doctor.check_user_config()
        assert r.status == doctor.FAIL

    def test_missing_fields_warns(self, tmp_path, monkeypatch):
        f = tmp_path / "user_config.json"
        f.write_text(json.dumps({"personal_info": {"name": "Jane"}}))
        import auto_applier.config as cfg
        monkeypatch.setattr(cfg, "USER_CONFIG_FILE", f)
        r = doctor.check_user_config()
        assert r.status == doctor.WARN
        assert "email" in r.message

    def test_complete_passes(self, tmp_path, monkeypatch):
        f = tmp_path / "user_config.json"
        # Include the recommended-but-not-required fields so the
        # soft-check passes too. The bare-minimum (name + email only)
        # case is now WARN — see test_minimal_warns.
        f.write_text(json.dumps({
            "personal_info": {
                "name": "Jane",
                "first_name": "Jane",
                "last_name": "Doe",
                "email": "j@x.com",
                "phone": "+15555550100",
                "city": "Seattle",
                "state": "WA",
                "zip_code": "98101",
                "country": "United States",
            }
        }))
        import auto_applier.config as cfg
        monkeypatch.setattr(cfg, "USER_CONFIG_FILE", f)
        r = doctor.check_user_config()
        assert r.status == doctor.PASS

    def test_minimal_warns(self, tmp_path, monkeypatch):
        """Only name+email present — passes hard check, warns on soft."""
        f = tmp_path / "user_config.json"
        f.write_text(json.dumps({
            "personal_info": {"name": "Jane", "email": "j@x.com"}
        }))
        import auto_applier.config as cfg
        monkeypatch.setattr(cfg, "USER_CONFIG_FILE", f)
        r = doctor.check_user_config()
        assert r.status == doctor.WARN
        assert "recommended" in r.message


class TestResumesLoaded:
    def test_none_fails(self, tmp_path, monkeypatch):
        resumes = tmp_path / "resumes"
        profiles = tmp_path / "profiles"
        resumes.mkdir()
        profiles.mkdir()
        import auto_applier.config as cfg
        monkeypatch.setattr(cfg, "RESUMES_DIR", resumes)
        monkeypatch.setattr(cfg, "PROFILES_DIR", profiles)
        r = doctor.check_resumes_loaded()
        assert r.status == doctor.FAIL

    def test_file_without_profile_warns(self, tmp_path, monkeypatch):
        resumes = tmp_path / "resumes"
        profiles = tmp_path / "profiles"
        resumes.mkdir()
        profiles.mkdir()
        (resumes / "r.pdf").write_bytes(b"%PDF")
        import auto_applier.config as cfg
        monkeypatch.setattr(cfg, "RESUMES_DIR", resumes)
        monkeypatch.setattr(cfg, "PROFILES_DIR", profiles)
        r = doctor.check_resumes_loaded()
        assert r.status == doctor.WARN
        assert "parsed" in r.message

    def test_both_present_passes(self, tmp_path, monkeypatch):
        resumes = tmp_path / "resumes"
        profiles = tmp_path / "profiles"
        resumes.mkdir()
        profiles.mkdir()
        (resumes / "r.pdf").write_bytes(b"%PDF")
        (profiles / "r.json").write_text("{}")
        import auto_applier.config as cfg
        monkeypatch.setattr(cfg, "RESUMES_DIR", resumes)
        monkeypatch.setattr(cfg, "PROFILES_DIR", profiles)
        r = doctor.check_resumes_loaded()
        assert r.status == doctor.PASS


class TestAnswersFile:
    def test_missing_warns(self, tmp_path, monkeypatch):
        import auto_applier.config as cfg
        monkeypatch.setattr(cfg, "ANSWERS_FILE", tmp_path / "nope.json")
        r = doctor.check_answers_file()
        assert r.status == doctor.WARN

    def test_bad_json_fails(self, tmp_path, monkeypatch):
        f = tmp_path / "answers.json"
        f.write_text("{ bad")
        import auto_applier.config as cfg
        monkeypatch.setattr(cfg, "ANSWERS_FILE", f)
        r = doctor.check_answers_file()
        assert r.status == doctor.FAIL

    def test_valid_passes(self, tmp_path, monkeypatch):
        f = tmp_path / "answers.json"
        f.write_text("{}")
        import auto_applier.config as cfg
        monkeypatch.setattr(cfg, "ANSWERS_FILE", f)
        r = doctor.check_answers_file()
        assert r.status == doctor.PASS


class TestGeminiKey:
    """check_gemini_key now re-reads .env at call time (not from the
    cached module constant) so wizard-written keys are visible without
    a process restart. Tests must patch BOTH the file-read source
    (PROJECT_ROOT/.env) and the env-var fallback."""

    def test_missing_warns(self, tmp_path, monkeypatch):
        import auto_applier.config as cfg
        monkeypatch.setattr(cfg, "PROJECT_ROOT", tmp_path)  # no .env in tmp_path
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        r = doctor.check_gemini_key()
        assert r.status == doctor.WARN

    def test_present_passes_from_env_file(self, tmp_path, monkeypatch):
        import auto_applier.config as cfg
        env = tmp_path / ".env"
        env.write_text("GEMINI_API_KEY=abc123\n", encoding="utf-8")
        monkeypatch.setattr(cfg, "PROJECT_ROOT", tmp_path)
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        r = doctor.check_gemini_key()
        assert r.status == doctor.PASS

    def test_present_passes_from_os_env(self, tmp_path, monkeypatch):
        """Fallback path: key set via OS env var, no .env file."""
        import auto_applier.config as cfg
        monkeypatch.setattr(cfg, "PROJECT_ROOT", tmp_path)
        monkeypatch.setenv("GEMINI_API_KEY", "abc123")
        r = doctor.check_gemini_key()
        assert r.status == doctor.PASS


class TestDiskSpace:
    def test_runs(self):
        r = doctor.check_disk_space()
        # Just assert it returns something — real disk space varies.
        assert r.status in (doctor.PASS, doctor.WARN, doctor.FAIL)


class TestFormat:
    def test_pass_has_no_fix_line(self):
        r = doctor.CheckResult("x", doctor.PASS, "ok", fix="")
        out = doctor._format(r)
        assert "fix:" not in out
        assert "[OK]" in out

    def test_fail_includes_fix(self):
        r = doctor.CheckResult("x", doctor.FAIL, "broken", fix="run X")
        out = doctor._format(r)
        assert "fix: run X" in out
        assert "[FAIL]" in out
