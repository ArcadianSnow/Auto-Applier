"""Tests for config.py — path constants, defaults, env overrides."""

from pathlib import Path

import pytest

from auto_applier import config


class TestPathConstants:
    def test_project_root_exists(self):
        assert config.PROJECT_ROOT.is_dir()

    def test_data_dir_under_project_root(self):
        assert config.DATA_DIR == config.PROJECT_ROOT / "data"

    def test_all_data_dirs_exist(self):
        dirs = [
            config.DATA_DIR,
            config.BROWSER_PROFILE_DIR,
            config.RESUMES_DIR,
            config.PROFILES_DIR,
            config.CACHE_DIR,
            config.BACKUP_DIR,
            config.GENERATED_RESUMES_DIR,
            config.RESEARCH_DIR,
            config.LOGS_DIR,
        ]
        for d in dirs:
            assert d.is_dir(), f"{d} should exist"

    def test_csv_paths_are_under_data_dir(self):
        for csv in [config.JOBS_CSV, config.APPLICATIONS_CSV,
                     config.SKILL_GAPS_CSV, config.FOLLOWUPS_CSV]:
            assert csv.parent == config.DATA_DIR

    def test_csv_paths_end_with_csv(self):
        for csv in [config.JOBS_CSV, config.APPLICATIONS_CSV,
                     config.SKILL_GAPS_CSV, config.FOLLOWUPS_CSV]:
            assert csv.suffix == ".csv"


class TestDefaults:
    def test_ollama_defaults(self):
        assert config.OLLAMA_BASE_URL  # not empty
        assert config.OLLAMA_MODEL  # not empty
        assert config.OLLAMA_MIN_VERSION == "0.8.0"

    def test_rate_limit_defaults_are_sane(self):
        assert config.MAX_APPLICATIONS_PER_DAY > 0
        assert config.MIN_DELAY_BETWEEN_ACTIONS < config.MAX_DELAY_BETWEEN_ACTIONS
        assert config.MIN_DELAY_BETWEEN_APPLICATIONS < config.MAX_DELAY_BETWEEN_APPLICATIONS

    def test_scoring_thresholds(self):
        assert config.DEFAULT_AUTO_APPLY_MIN > config.DEFAULT_REVIEW_MIN
        assert config.DEFAULT_REVIEW_MIN > 0

    def test_evolution_threshold_positive(self):
        assert config.DEFAULT_EVOLUTION_TRIGGER_THRESHOLD >= 1

    def test_ghost_skip_threshold(self):
        assert 0 <= config.GHOST_SKIP_THRESHOLD <= 11

    def test_followup_cadence_is_sorted(self):
        assert config.FOLLOWUP_CADENCE_DAYS == sorted(config.FOLLOWUP_CADENCE_DAYS)
        assert all(d > 0 for d in config.FOLLOWUP_CADENCE_DAYS)

    def test_model_presets_list(self):
        assert len(config.OLLAMA_MODEL_PRESETS) >= 3
        assert config.OLLAMA_MODEL in config.OLLAMA_MODEL_PRESETS
