"""Seed the answer bank from v2's flat ``data/answers.json`` (one-time migration).

v2 stored Q&A as a flat ``{question: answer}`` dict; v3's ``answers`` table is the same
shape with an added embedding BLOB for semantic match (spec §8b). This helper reads the
v2 file (if present) and UPSERTs each row with ``source='user'``, computing an embedding
when an :class:`EmbeddingClient` is available. Idempotent: re-running re-embeds (cheap
on warm Ollama) and overwrites with the same value.

Run from the GUI's onboarding step, or on demand:

    >>> from av3.config.settings import load_settings
    >>> from av3.db.engine import open_db                      # doctest: +SKIP
    >>> from av3.db.repositories import AnswerRepo             # doctest: +SKIP
    >>> from av3.llm.embed import OllamaEmbeddings             # doctest: +SKIP
    >>> from av3.resume.seed_answers import seed_from_v2_file  # doctest: +SKIP
    >>> # ... open DB, build repo + client, call seed_from_v2_file(...)
"""

from __future__ import annotations

import json
from pathlib import Path

from av3.llm.embed import EmbeddingClient
from av3.resume.answer_resolver import store_answer


async def seed_from_v2_file(
    answer_repo,
    embed_client: EmbeddingClient | None,
    v2_answers_path: Path | str,
) -> int:
    """Read v2's ``data/answers.json`` and UPSERT each row into the v3 answer bank.

    Returns the count of rows seeded. Missing/empty file -> 0 (no error). Malformed
    JSON raises so the onboarding flow surfaces it instead of silently degrading the
    resolver's bank coverage.
    """
    path = Path(v2_answers_path)
    if not path.exists():
        return 0
    raw = path.read_text(encoding="utf-8").strip()
    if not raw:
        return 0
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError(f"v2 answers.json must be a flat object, got {type(data).__name__}")
    count = 0
    for question, answer in data.items():
        if not isinstance(question, str) or not question.strip():
            continue
        if answer is None:
            continue
        await store_answer(
            answer_repo,
            embed_client,
            question=question.strip(),
            answer=str(answer),
            source="user",
        )
        count += 1
    return count
