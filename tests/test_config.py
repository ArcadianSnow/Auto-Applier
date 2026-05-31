"""Config: defaults, validation (fail-fast), and .env secret injection (spec §10)."""

from __future__ import annotations

import json

import pytest

from auto_applier.config import Settings, load_settings
from auto_applier.config.settings import ScoringConfig, ScoringWeights


def test_defaults_are_valid():
    s = Settings()
    assert s.scoring.auto_apply_min == 7.0
    assert s.scoring.review_min == 4.0
    assert abs(sum(s.scoring.weights.as_dict().values()) - 1.0) < 0.01
    assert s.telemetry.enabled is False  # opt-in, default OFF


def test_weights_must_sum_to_one():
    with pytest.raises(ValueError, match="sum to 1.0"):
        ScoringWeights(skills=0.9, experience=0.9)


def test_thresholds_must_be_ordered():
    with pytest.raises(ValueError, match="review_min"):
        ScoringConfig(auto_apply_min=4.0, review_min=7.0)


def test_derived_paths(settings):
    assert settings.app_db_path.name == "app.db"
    assert settings.events_db_path.name == "events.db"
    assert settings.app_db_path.parent == settings.data_dir
    # events.db is a SEPARATE file from app.db (spec §9)
    assert settings.app_db_path != settings.events_db_path


def test_load_settings_reads_user_config(data_dir, monkeypatch):
    monkeypatch.setenv("AV3_DATA_DIR", str(data_dir))
    (data_dir / "user_config.json").write_text(
        json.dumps({"scoring": {"auto_apply_min": 8.0, "review_min": 5.0}})
    )
    s = load_settings()
    assert s.scoring.auto_apply_min == 8.0
    assert s.scoring.review_min == 5.0


def test_gemini_key_injected_from_env_not_json(data_dir, monkeypatch):
    monkeypatch.setenv("AV3_DATA_DIR", str(data_dir))
    monkeypatch.setenv("GEMINI_API_KEY", "secret-from-env")
    s = load_settings()
    assert s.llm.gemini_api_key == "secret-from-env"
    # the key must NOT be persisted in the inspectable JSON config
    assert not (data_dir / "user_config.json").exists()
