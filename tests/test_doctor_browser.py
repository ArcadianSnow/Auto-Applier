"""Tests for ``doctor.check_browser`` (the first-run browser readiness check).

WARN-only by design (real Chrome via channel may cover the apply path; a fresh install
legitimately hasn't run ``av3 install-browser`` yet). Detection is read-only — an import
probe + a cache glob + the same filesystem Chrome check the session uses — and never
launches a browser, so these tests just steer those three signals.
"""

from __future__ import annotations

import importlib.util

from auto_applier.config import Settings
from auto_applier.doctor import Status, check_browser


def _patch_driver(monkeypatch, *, present: bool) -> None:
    real = importlib.util.find_spec

    def fake(name, *a, **k):
        if name in ("patchright", "playwright"):
            return object() if present else None
        return real(name, *a, **k)

    monkeypatch.setattr(importlib.util, "find_spec", fake)


def test_warn_when_driver_missing(settings: Settings, monkeypatch):
    _patch_driver(monkeypatch, present=False)
    r = check_browser(settings)
    assert r.status is Status.WARN
    assert "install-browser" in r.fix


def test_pass_when_chromium_cache_present(settings: Settings, monkeypatch):
    _patch_driver(monkeypatch, present=True)
    monkeypatch.setattr("auto_applier.doctor._bundled_chromium_present", lambda: True)
    monkeypatch.setattr(
        "auto_applier.sources.browser.session.detect_chrome_channel", lambda: None
    )
    r = check_browser(settings)
    assert r.status is Status.PASS
    assert "chromium" in r.detail.lower()


def test_pass_when_real_chrome_present(settings: Settings, monkeypatch):
    _patch_driver(monkeypatch, present=True)
    monkeypatch.setattr("auto_applier.doctor._bundled_chromium_present", lambda: False)
    monkeypatch.setattr(
        "auto_applier.sources.browser.session.detect_chrome_channel", lambda: "chrome"
    )
    r = check_browser(settings)
    assert r.status is Status.PASS
    assert "chrome" in r.detail.lower()


def test_warn_when_no_browser_anywhere(settings: Settings, monkeypatch):
    _patch_driver(monkeypatch, present=True)
    monkeypatch.setattr("auto_applier.doctor._bundled_chromium_present", lambda: False)
    monkeypatch.setattr(
        "auto_applier.sources.browser.session.detect_chrome_channel", lambda: None
    )
    r = check_browser(settings)
    assert r.status is Status.WARN
    assert "install-browser" in r.fix
