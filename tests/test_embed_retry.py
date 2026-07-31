"""Transient-failure retry for the Ollama embeddings client.

Motivated by the owner's production event spine (2026-07-31): `EmbeddingError: Ollama
embeddings unreachable: HTTPStatusError("Server error…")` was the single largest error class
(27 of 43 errors in 60 days). The filter stage fails open on those, so nothing is lost — but
each one then pays the full JD-scrape + LLM score the pre-filter exists to avoid.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from auto_applier.llm.embed import EmbeddingError, OllamaEmbeddings


class _FakeResponse:
    def __init__(self, status: int, payload: dict | None = None):
        self.status_code = status
        self._payload = payload or {}

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"status {self.status_code}", request=None, response=self
            )


class _FakeClient:
    """Stands in for httpx.AsyncClient; replays a scripted sequence of outcomes."""

    def __init__(self, script: list):
        self._script = script
        self.calls = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, json=None):
        item = self._script[min(self.calls, len(self._script) - 1)]
        self.calls += 1
        if isinstance(item, Exception):
            raise item
        return item


@pytest.fixture
def patch_client(monkeypatch):
    holder = {}

    def _install(script):
        client = _FakeClient(script)
        holder["client"] = client
        monkeypatch.setattr(
            "auto_applier.llm.embed.httpx.AsyncClient", lambda **kw: client
        )
        return client

    return _install


OK = _FakeResponse(200, {"embedding": [0.1, 0.2, 0.3]})


def test_transient_5xx_is_retried_then_succeeds(patch_client):
    client = patch_client([_FakeResponse(500), OK])
    vec = asyncio.run(OllamaEmbeddings(backoff_s=0).embed("hello"))
    assert vec == pytest.approx([0.1, 0.2, 0.3])
    assert client.calls == 2


def test_timeout_is_retried(patch_client):
    client = patch_client([httpx.ReadTimeout("timed out"), OK])
    assert asyncio.run(OllamaEmbeddings(backoff_s=0).embed("hello"))
    assert client.calls == 2


def test_connect_error_is_retried(patch_client):
    client = patch_client([httpx.ConnectError("refused"), OK])
    assert asyncio.run(OllamaEmbeddings(backoff_s=0).embed("hello"))
    assert client.calls == 2


def test_retries_are_bounded_then_raise(patch_client):
    client = patch_client([_FakeResponse(503)])
    with pytest.raises(EmbeddingError) as exc:
        asyncio.run(OllamaEmbeddings(retries=2, backoff_s=0).embed("hello"))
    assert "unreachable" in str(exc.value)
    assert client.calls == 3          # 1 attempt + 2 retries, never unbounded


def test_4xx_is_not_retried(patch_client):
    """A 4xx is permanent and actionable (model not pulled) — surface it immediately
    instead of delaying the error the user needs to see."""
    client = patch_client([_FakeResponse(404)])
    with pytest.raises(EmbeddingError):
        asyncio.run(OllamaEmbeddings(retries=2, backoff_s=0).embed("hello"))
    assert client.calls == 1


def test_429_is_retried(patch_client):
    client = patch_client([_FakeResponse(429), OK])
    assert asyncio.run(OllamaEmbeddings(backoff_s=0).embed("hello"))
    assert client.calls == 2


def test_malformed_payload_still_raises_without_retry(patch_client):
    client = patch_client([_FakeResponse(200, {"oops": True})])
    with pytest.raises(EmbeddingError) as exc:
        asyncio.run(OllamaEmbeddings(retries=2, backoff_s=0).embed("hello"))
    assert "no embedding" in str(exc.value)
    assert client.calls == 1


def test_retries_can_be_disabled(patch_client):
    client = patch_client([_FakeResponse(500)])
    with pytest.raises(EmbeddingError):
        asyncio.run(OllamaEmbeddings(retries=0, backoff_s=0).embed("hello"))
    assert client.calls == 1
