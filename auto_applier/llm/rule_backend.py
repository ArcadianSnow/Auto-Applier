"""Rule-based fallback backend using fuzzy matching against answers.json."""

import difflib
import json
import time
from pathlib import Path

from auto_applier.llm.base import LLMBackend, LLMResponse


class RuleBackend(LLMBackend):
    """Last-resort backend that fuzzy-matches questions against a local
    answers.json knowledge base.

    Always available. Cannot produce structured JSON output -- returns
    ``{}`` from :meth:`complete_json`.
    """

    name = "rule-based"

    def __init__(
        self,
        answers_path: str | Path = "",
        unanswered_path: str | Path = "",
        threshold: float = 0.6,
    ) -> None:
        from auto_applier.config import ANSWERS_FILE, UNANSWERED_FILE

        self.answers_path = Path(answers_path) if answers_path else ANSWERS_FILE
        self.unanswered_path = (
            Path(unanswered_path) if unanswered_path else UNANSWERED_FILE
        )
        self.threshold = threshold
        self._answers: list[dict] = []
        self._loaded = False

    # ------------------------------------------------------------------
    # Data loading
    # ------------------------------------------------------------------

    def _ensure_loaded(self) -> None:
        """Lazy-load the answers file on first use."""
        if self._loaded:
            return
        self._loaded = True
        if self.answers_path.exists():
            try:
                with open(self.answers_path, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
                if isinstance(data, list):
                    self._answers = data
            except (json.JSONDecodeError, OSError):
                self._answers = []

    # ------------------------------------------------------------------
    # Availability
    # ------------------------------------------------------------------

    async def is_available(self) -> bool:
        """Always available as the final fallback."""
        return True

    # ------------------------------------------------------------------
    # Text completion (fuzzy match)
    # ------------------------------------------------------------------

    async def complete(
        self,
        prompt: str,
        system_prompt: str = "",
        temperature: float = 0.3,
        max_tokens: int = 1024,
    ) -> LLMResponse:
        self._ensure_loaded()
        start = time.monotonic()
        answer = self._fuzzy_match(prompt)
        elapsed = (time.monotonic() - start) * 1000

        if not answer:
            self._record_unanswered(prompt)

        return LLMResponse(
            text=answer,
            model="rule-based",
            tokens_used=0,
            cached=False,
            latency_ms=elapsed,
        )

    # ------------------------------------------------------------------
    # JSON completion (not supported)
    # ------------------------------------------------------------------

    async def complete_json(
        self,
        prompt: str,
        system_prompt: str = "",
        temperature: float = 0.1,
    ) -> dict:
        """Rule backend cannot generate structured JSON -- returns empty dict."""
        return {}

    # ------------------------------------------------------------------
    # Fuzzy matching
    # ------------------------------------------------------------------

    def _fuzzy_match(self, prompt: str) -> str:
        """Find the best-matching answer above the similarity threshold."""
        if not self._answers:
            return ""

        prompt_lower = prompt.lower().strip()
        best_ratio = 0.0
        best_answer = ""

        for entry in self._answers:
            question = entry.get("question", "").lower().strip()
            if not question:
                continue
            ratio = difflib.SequenceMatcher(
                None, prompt_lower, question
            ).ratio()
            if ratio > best_ratio:
                best_ratio = ratio
                best_answer = entry.get("answer", "")

        if best_ratio >= self.threshold:
            return best_answer
        return ""

    # ------------------------------------------------------------------
    # Unanswered question logging
    # ------------------------------------------------------------------

    def _record_unanswered(self, prompt: str) -> None:
        """Append the unmatched prompt to unanswered.json for later review.

        Skips phantom labels, questions already covered by answers.json,
        and ultra-short fragments — same filter used by the form
        filler's record path so both producers stay consistent.
        """
        from auto_applier.browser.selector_utils import should_skip_unanswered

        if should_skip_unanswered(prompt, self.answers_path):
            return

        existing: list[str] = []
        if self.unanswered_path.exists():
            try:
                with open(self.unanswered_path, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
                if isinstance(data, list):
                    existing = data
            except (json.JSONDecodeError, OSError):
                existing = []

        trimmed = prompt.strip()
        if trimmed and trimmed not in existing:
            existing.append(trimmed)
            self.unanswered_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.unanswered_path, "w", encoding="utf-8") as fh:
                json.dump(existing, fh, indent=2, ensure_ascii=False)
