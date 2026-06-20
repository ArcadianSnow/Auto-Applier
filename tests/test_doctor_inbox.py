"""Unit tests for ``doctor.check_inbox`` — the email outcome loop config check (Direction 4).

Optional + opt-in, so OFF is PASS. When enabled, it WARNs (never FAILs) on an incomplete setup
(missing user or missing AV3_IMAP_PASSWORD) so a half-configured inbox surfaces in `av3 doctor`
instead of silently never ingesting. No live IMAP login here.
"""

from __future__ import annotations

from auto_applier.doctor import Status, check_inbox


def test_pass_when_disabled(settings, monkeypatch):
    monkeypatch.delenv("AV3_IMAP_PASSWORD", raising=False)
    # Default settings → inbox disabled.
    r = check_inbox(settings)
    assert r.status is Status.PASS
    assert "off" in r.detail


def test_warn_when_enabled_without_user(settings, monkeypatch):
    monkeypatch.delenv("AV3_IMAP_PASSWORD", raising=False)
    settings.inbox.enabled = True
    settings.inbox.user = None
    r = check_inbox(settings)
    assert r.status is Status.WARN
    assert "no user" in r.detail.lower()


def test_warn_when_enabled_without_password(settings, monkeypatch):
    monkeypatch.delenv("AV3_IMAP_PASSWORD", raising=False)
    settings.inbox.enabled = True
    settings.inbox.user = "me@gmail.com"
    r = check_inbox(settings)
    assert r.status is Status.WARN
    assert "AV3_IMAP_PASSWORD" in r.detail


def test_pass_when_fully_configured(settings, monkeypatch):
    settings.inbox.enabled = True
    settings.inbox.user = "me@gmail.com"
    monkeypatch.setenv("AV3_IMAP_PASSWORD", "app-password")
    r = check_inbox(settings)
    assert r.status is Status.PASS
    assert "me@gmail.com" in r.detail
