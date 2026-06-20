"""Unit tests for ``doctor.check_llm`` — Ollama reachability + BOTH models present.

The completion-model check existed; the embed-model check is the MVP-readiness add (a missing
``nomic-embed-text`` used to pass green and only surface as a runtime EmbeddingError). httpx.get
is monkeypatched so no network is touched — same pattern as the relay check.
"""

from __future__ import annotations

import httpx

from auto_applier.doctor import Status, check_llm


class _Resp:
    def __init__(self, models):
        self._models = models

    def raise_for_status(self):  # pragma: no cover - trivial
        pass

    def json(self):
        return {"models": [{"name": m} for m in self._models]}


def _with_models(monkeypatch, models):
    monkeypatch.setattr(httpx, "get", lambda url, timeout=None: _Resp(models))


def test_pass_when_both_models_present(settings, monkeypatch):
    _with_models(monkeypatch, [settings.llm.ollama_model, settings.llm.embed_model])
    r = check_llm(settings)
    assert r.status is Status.PASS
    assert settings.llm.ollama_model in r.detail
    assert settings.llm.embed_model in r.detail


def test_pass_tolerates_latest_tag(settings, monkeypatch):
    # A pulled "nomic-embed-text:latest" satisfies the configured "nomic-embed-text".
    _with_models(monkeypatch,
                 [f"{settings.llm.ollama_model}", f"{settings.llm.embed_model}:latest"])
    r = check_llm(settings)
    assert r.status is Status.PASS


def test_warn_when_embed_model_missing(settings, monkeypatch):
    # Completion model present, embed model absent — the gap this check closes.
    _with_models(monkeypatch, [settings.llm.ollama_model])
    r = check_llm(settings)
    assert r.status is Status.WARN
    assert settings.llm.embed_model in r.detail
    assert "setup-llm" in (r.fix or "")


def test_warn_when_completion_model_missing(settings, monkeypatch):
    _with_models(monkeypatch, [settings.llm.embed_model])
    r = check_llm(settings)
    assert r.status is Status.WARN
    assert settings.llm.ollama_model in r.detail
    assert "setup-llm" in (r.fix or "")


def test_warn_when_unreachable(settings, monkeypatch):
    def boom(url, timeout=None):
        raise httpx.ConnectError("down")

    monkeypatch.setattr(httpx, "get", boom)
    r = check_llm(settings)
    assert r.status is Status.WARN
    assert "setup-llm" in (r.fix or "")
