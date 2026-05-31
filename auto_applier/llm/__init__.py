"""Local-first LLM clients for v3 (embeddings + completion).

Two clients, both tiny and async, both designed for **dependency injection** so the
answer resolver (spec §8b) and any future LLM consumer can be unit-tested without a
live model.

- :mod:`auto_applier.llm.embed` — Ollama ``nomic-embed-text`` (the spec default).
- :mod:`auto_applier.llm.complete` — Ollama -> Gemini -> raises. JSON-mode completion for the
  resolver's tier-3 confidence-gated backup.

We deliberately do NOT port v2's big ``LLMRouter`` / prompt registry / disk cache layer
in this pass — v3 only needs an answer-with-confidence call for the resolver. Richer
infra arrives with résumé generation (Phase 3). Keeping it minimal also keeps the unit
tests fast (no network, no mocks of a large API surface).
"""
