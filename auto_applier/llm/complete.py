"""Tier-3 LLM client for the answer resolver (spec §8b confidence-gated backup).

When a form question doesn't match the answer bank, the resolver asks the LLM to
**propose an answer AND self-report confidence**. Above threshold → submit but flag as
inferred; below → bail to REVIEW. The LLM is therefore the *backstop*, not the default
— most fills come from the bank.

JSON-mode is non-negotiable here: the resolver expects a structured
``{answer, confidence}`` reply. We request JSON via the Ollama ``format: "json"`` knob
and the Gemini ``response_mime_type`` knob, parse, and reject on any malformed reply
(no string-scraping fallbacks — those were the v2 reliability tax).

Like the embedding client, this is an injectable Protocol so resolver tests can run
without a live model.
"""

from __future__ import annotations

import json
from typing import Protocol, runtime_checkable

import httpx


@runtime_checkable
class CompletionClient(Protocol):
    """JSON-out completion. Tests pass a deterministic stub."""

    async def complete_json(self, prompt: str, *, system: str = "") -> dict: ...


class CompletionError(RuntimeError):
    """Raised when no backend can produce a parseable JSON reply."""


class _OllamaJSONBackend:
    """Local Ollama ``/api/generate`` with ``format=json``. Primary tier."""

    def __init__(
        self,
        host: str,
        model: str,
        timeout_s: float = 60.0,
    ):
        self.host = host.rstrip("/")
        self.model = model
        self.timeout_s = timeout_s

    async def complete_json(self, prompt: str, *, system: str = "") -> dict:
        payload: dict = {
            "model": self.model,
            "prompt": prompt,
            "format": "json",
            "stream": False,
            "options": {"temperature": 0.0},  # deterministic for resolver use
        }
        if system:
            payload["system"] = system
        url = f"{self.host}/api/generate"
        async with httpx.AsyncClient(timeout=self.timeout_s) as client:
            resp = await client.post(url, json=payload)
            resp.raise_for_status()
            body = resp.json()
        raw = body.get("response", "")
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise CompletionError(f"Ollama returned non-JSON: {raw!r}") from exc


class _GeminiJSONBackend:
    """Google Gemini ``v1beta/models/{model}:generateContent`` with
    ``response_mime_type=application/json``. Secondary tier (free 1k/day, spec §6)."""

    def __init__(
        self,
        api_key: str,
        model: str = "gemini-1.5-flash",
        timeout_s: float = 60.0,
    ):
        self.api_key = api_key
        self.model = model
        self.timeout_s = timeout_s

    async def complete_json(self, prompt: str, *, system: str = "") -> dict:
        url = (
            f"https://generativelanguage.googleapis.com/v1beta/models/"
            f"{self.model}:generateContent?key={self.api_key}"
        )
        parts: list[dict] = []
        if system:
            parts.append({"text": system + "\n\n"})
        parts.append({"text": prompt})
        payload = {
            "contents": [{"role": "user", "parts": parts}],
            "generationConfig": {
                "responseMimeType": "application/json",
                "temperature": 0.0,
            },
        }
        async with httpx.AsyncClient(timeout=self.timeout_s) as client:
            resp = await client.post(url, json=payload)
            resp.raise_for_status()
            body = resp.json()
        try:
            text = body["candidates"][0]["content"]["parts"][0]["text"]
        except (KeyError, IndexError, TypeError) as exc:
            raise CompletionError(f"Gemini reply shape unexpected: {body!r}") from exc
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise CompletionError(f"Gemini returned non-JSON: {text!r}") from exc


class FallbackCompletion:
    """Ollama-first, Gemini-fallback. If both fail, raises :class:`CompletionError` —
    the resolver treats that as "tier-3 unavailable" and bails to REVIEW.

    No third tier (the v2 ``rule_based`` answer table is what the *bank* already does
    deterministically — duplicating it here would just make the call graph confusing).
    """

    def __init__(
        self,
        ollama: _OllamaJSONBackend | None = None,
        gemini: _GeminiJSONBackend | None = None,
    ):
        self.ollama = ollama
        self.gemini = gemini

    async def complete_json(self, prompt: str, *, system: str = "") -> dict:
        last_exc: Exception | None = None
        if self.ollama is not None:
            try:
                return await self.ollama.complete_json(prompt, system=system)
            except (httpx.HTTPError, CompletionError) as exc:
                last_exc = exc
        if self.gemini is not None:
            try:
                return await self.gemini.complete_json(prompt, system=system)
            except (httpx.HTTPError, CompletionError) as exc:
                last_exc = exc
        raise CompletionError(
            f"no completion backend available (last error: {last_exc})"
        )


def build_default(settings) -> FallbackCompletion:
    """Construct the default chain from :class:`auto_applier.config.settings.Settings`."""
    ollama = _OllamaJSONBackend(host=settings.llm.ollama_host, model=settings.llm.ollama_model)
    gemini = (
        _GeminiJSONBackend(api_key=settings.llm.gemini_api_key)
        if settings.llm.gemini_api_key
        else None
    )
    return FallbackCompletion(ollama=ollama, gemini=gemini)
