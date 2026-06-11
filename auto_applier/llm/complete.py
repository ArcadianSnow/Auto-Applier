"""Tier-3 LLM client for the answer resolver (spec §8b confidence-gated backup).

When a form question doesn't match the answer bank, the resolver asks the LLM to
**propose an answer AND self-report confidence**. Above threshold → submit but flag as
inferred; below → bail to REVIEW. The LLM is therefore the *backstop*, not the default
— most fills come from the bank.

JSON-mode is non-negotiable here: the resolver expects a structured
``{answer, confidence}`` reply. We request JSON via the Ollama ``format: "json"`` knob,
parse, and reject on any malformed reply (no string-scraping fallbacks — those were the
v2 reliability tax).

**Local-only:** the sole completion backend is local Ollama. The former cloud secondary
tier (Google Gemini) was removed once ``gemini-1.5-flash`` was retired by Google (the
``v1beta`` endpoint 404s for new keys) — keeping it pointed at a dead model only produced
spurious fail-closed jobs, and a cloud tier sits awkwardly against the local-first,
zero-cost design. If Ollama can't produce a parseable reply the resolver/score worker
fails closed (→ REVIEW / SKIP), with the deterministic bank + rule path as the floor.

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


def repair_truncated_json(raw: str) -> dict | list | None:
    """Bounded structural repair for a truncated Ollama ``format=json`` reply.

    Ollama's JSON grammar guarantees the output is a valid JSON *prefix*, but a
    model can stop emitting before closing its braces (observed live with
    qwen3:8b: a complete object minus the final ``}``, padded with newlines).
    This appends the missing closers — tracked outside string literals — and
    retries the parse. It is NOT string-scraping (the v2 reliability tax this
    module bans): no content is guessed, only structure the grammar already
    promised is completed. Returns ``None`` when repair doesn't yield JSON.
    """
    s = raw.strip()
    if not s or s[0] not in "{[":
        return None
    stack: list[str] = []
    in_string = False
    escape = False
    for ch in s:
        if escape:
            escape = False
            continue
        if ch == "\\":
            escape = in_string  # backslash only escapes inside a string
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch in "{[":
            stack.append("}" if ch == "{" else "]")
        elif ch in "}]":
            if stack and stack[-1] == ch:
                stack.pop()
            else:
                return None  # mismatched closer — not a clean truncation
    if in_string:
        s += '"'  # close a dangling string literal before the brackets
    try:
        return json.loads(s + "".join(reversed(stack)))
    except json.JSONDecodeError:
        return None


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
            repaired = repair_truncated_json(raw)
            if repaired is not None:
                return repaired
            raise CompletionError(f"Ollama returned non-JSON: {raw[:500]!r}") from exc


class FallbackCompletion:
    """Local Ollama completion. If Ollama can't produce a parseable JSON reply, raises
    :class:`CompletionError` — the resolver/score worker treats that as "tier-3
    unavailable" and bails (→ REVIEW / SKIP).

    Named ``FallbackCompletion`` for historical reasons: it once chained Ollama → Gemini.
    The Gemini cloud tier was removed (retired model + local-first design); the
    deterministic bank + rule path is the real floor below this.
    """

    def __init__(self, ollama: _OllamaJSONBackend | None = None):
        self.ollama = ollama

    async def complete_json(self, prompt: str, *, system: str = "") -> dict:
        last_exc: Exception | None = None
        if self.ollama is not None:
            try:
                return await self.ollama.complete_json(prompt, system=system)
            except (httpx.HTTPError, CompletionError) as exc:
                last_exc = exc
        raise CompletionError(
            f"no completion backend available (last error: {last_exc})"
        )


def build_default(settings) -> FallbackCompletion:
    """Construct the default (Ollama-only) client from
    :class:`auto_applier.config.settings.Settings`."""
    ollama = _OllamaJSONBackend(host=settings.llm.ollama_host, model=settings.llm.ollama_model)
    return FallbackCompletion(ollama=ollama)
