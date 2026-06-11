"""Embedding client — Ollama ``nomic-embed-text`` for the answer resolver (spec §8b).

The resolver embeds each form question and cosine-compares against the answer bank to
match differently-worded versions of the same Q&A ("authorized to work in the US?" vs
"have US work authorization?"). Embeddings live in ``answers.embedding`` as raw float32
bytes (BLOB) — small (~3 KB per vector at 768d) and load fast for the in-process
brute-force compare we do at apply time (bank is bounded — dozens to low hundreds, not
millions, so an ANN index is overkill).

Design:
  * Async (matches the apply driver's call site).
  * Injectable Protocol :class:`EmbeddingClient` — tests pass a stub; prod passes
    :class:`OllamaEmbeddings`.
  * Cosine similarity is a plain :func:`cosine` function on ``list[float]`` — no numpy
    dependency added (the bank is small; a Python loop is fine and one less wheel to
    ship in the standalone build).
  * Storage helpers ``vec_to_bytes`` / ``bytes_to_vec`` (float32) round-trip the BLOB
    column without numpy.

If Ollama is unreachable, :meth:`OllamaEmbeddings.embed` raises — the resolver treats
that as "no semantic match available" and falls through to the LLM tier (which itself
may fall back to REVIEW). The resolver does NOT silently bypass the bank.
"""

from __future__ import annotations

import math
import struct
from typing import Protocol, runtime_checkable

import httpx


@runtime_checkable
class EmbeddingClient(Protocol):
    """Resolve-time embedding interface. Tests pass a deterministic stub."""

    async def embed(self, text: str) -> list[float]: ...


class EmbeddingError(RuntimeError):
    """Raised when the embedding backend is unreachable or returns malformed data."""


class OllamaEmbeddings:
    """Thin Ollama ``/api/embeddings`` client (spec §6 default backend).

    One vector per call — Ollama's API doesn't batch on this endpoint. Per-call latency
    on ``nomic-embed-text`` is ~20-50ms on a warm model; the bank is small enough that
    we just embed the question once at resolve time and re-use stored bank vectors.
    """

    def __init__(
        self,
        host: str = "http://localhost:11434",
        model: str = "nomic-embed-text",
        timeout_s: float = 60.0,
    ):
        # 60s default, not 10: the FIRST call after Ollama swaps models pays the
        # cold-load (observed live 2026-06-11: >10s while qwen3:8b was resident,
        # surfacing as ReadTimeout → the filter failed open every run). A warm
        # call is ~20-50ms, so the generous ceiling costs nothing in the normal
        # case and only shows up when the alternative was a spurious failure.
        self.host = host.rstrip("/")
        self.model = model
        self.timeout_s = timeout_s

    async def embed(self, text: str) -> list[float]:
        url = f"{self.host}/api/embeddings"
        payload = {"model": self.model, "prompt": text}
        try:
            async with httpx.AsyncClient(timeout=self.timeout_s) as client:
                resp = await client.post(url, json=payload)
                resp.raise_for_status()
                data = resp.json()
        except httpx.HTTPError as exc:
            # !r, not str: httpx.ReadTimeout formats as "" — an empty reason in
            # the fail-open note made this bug needlessly hard to diagnose.
            raise EmbeddingError(f"Ollama embeddings unreachable: {exc!r}") from exc
        vec = data.get("embedding")
        if not isinstance(vec, list) or not vec:
            raise EmbeddingError(f"Ollama returned no embedding: {data!r}")
        return [float(x) for x in vec]


def cosine(a: list[float], b: list[float]) -> float:
    """Cosine similarity in [-1, 1]. Returns 0.0 on degenerate input (zero-norm or
    mismatched length) so callers can treat "no signal" identically to "no match"."""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = 0.0
    na = 0.0
    nb = 0.0
    for x, y in zip(a, b):
        dot += x * y
        na += x * x
        nb += y * y
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (math.sqrt(na) * math.sqrt(nb))


# --- BLOB <-> vector codec (float32, native endianness — DB never leaves the box) ---

def vec_to_bytes(vec: list[float]) -> bytes:
    """Pack a vector as float32 bytes for the ``answers.embedding`` BLOB column."""
    return struct.pack(f"{len(vec)}f", *vec)


def bytes_to_vec(blob: bytes | None) -> list[float]:
    """Unpack a float32 BLOB back to a Python list. Empty/None -> []."""
    if not blob:
        return []
    n = len(blob) // 4
    return list(struct.unpack(f"{n}f", blob[: n * 4]))
