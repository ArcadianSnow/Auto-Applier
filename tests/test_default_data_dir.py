"""The default data dir must be a STABLE ABSOLUTE per-user path (not CWD-relative `data/v3`),
so a non-repo install doesn't scatter data into whatever directory `av3` was launched from.
``AV3_DATA_DIR`` still overrides everything (tests rely on that)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from auto_applier.config.settings import _default_data_dir, load_settings


def test_default_is_absolute():
    assert _default_data_dir().is_absolute()


@pytest.mark.skipif(os.name != "nt", reason="Windows LOCALAPPDATA branch")
def test_windows_uses_localappdata(monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", r"C:\Users\test\AppData\Local")
    d = _default_data_dir()
    assert d == Path(r"C:\Users\test\AppData\Local") / "AutoApplier" / "data"


@pytest.mark.skipif(os.name == "nt", reason="POSIX XDG branch")
def test_posix_uses_xdg(monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", "/home/test/.local/share")
    d = _default_data_dir()
    assert d == Path("/home/test/.local/share") / "auto-applier"


def test_av3_data_dir_env_overrides_default(tmp_path, monkeypatch):
    monkeypatch.setenv("AV3_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    settings = load_settings()
    assert settings.data_dir == tmp_path
