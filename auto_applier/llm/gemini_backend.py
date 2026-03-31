"""Google Gemini free-tier LLM backend via REST API."""

import json
import time
from datetime import datetime, timezone

import httpx

from auto_applier.llm.base import LLMBackend, LLMResponse

_GEMINI_API_BASE = (
    "https://generativelanguage.googleapis.com/v1beta/models"
)


class GeminiBackend(LLMBackend):
    """Google Gemini backend using the free generativelanguage REST API.

    Tracks daily request count in memory and refuses to exceed the
    free-tier limit (default 1,000 requests/day for Gemini 2.5 Flash-Lite).
    The counter resets at midnight UTC.
    """

    name = "gemini"

    def __init__(
        self,
        api_key: str = "",
        model: str = "",
        daily_limit: int = 1000,
    ) -> None:
        from auto_applier.config import GEMINI_API_KEY, GEMINI_MODEL

        self.api_key = api_key or GEMINI_API_KEY
        self.model = model or GEMINI_MODEL
        self.daily_limit = daily_limit
        self._client = httpx.AsyncClient(timeout=30.0)

        # In-memory daily counter (resets at midnight UTC)
        self._request_count: int = 0
        self._count_date: str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # ------------------------------------------------------------------
    # Rate-limit bookkeeping
    # ------------------------------------------------------------------

    def _reset_if_new_day(self) -> None:
        """Reset the request counter if the UTC date has rolled over."""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if today != self._count_date:
            self._request_count = 0
            self._count_date = today

    def _increment_count(self) -> None:
        self._reset_if_new_day()
        self._request_count += 1

    @property
    def requests_remaining(self) -> int:
        self._reset_if_new_day()
        return max(0, self.daily_limit - self._request_count)

    # ------------------------------------------------------------------
    # Availability
    # ------------------------------------------------------------------

    async def is_available(self) -> bool:
        """Available when an API key is set and daily quota is not exhausted."""
        if not self.api_key:
            return False
        self._reset_if_new_day()
        return self._request_count < self.daily_limit

    # ------------------------------------------------------------------
    # Text completion
    # ------------------------------------------------------------------

    async def complete(
        self,
        prompt: str,
        system_prompt: str = "",
        temperature: float = 0.3,
        max_tokens: int = 1024,
    ) -> LLMResponse:
        payload = self._build_payload(
            prompt,
            system_prompt=system_prompt,
            temperature=temperature,
            max_tokens=max_tokens,
            response_mime="text/plain",
        )
        data, elapsed = await self._post(payload)
        text = self._extract_text(data)
        tokens = self._extract_token_count(data)
        self._increment_count()

        return LLMResponse(
            text=text,
            model=self.model,
            tokens_used=tokens,
            cached=False,
            latency_ms=elapsed,
        )

    # ------------------------------------------------------------------
    # JSON completion
    # ------------------------------------------------------------------

    async def complete_json(
        self,
        prompt: str,
        system_prompt: str = "",
        temperature: float = 0.1,
    ) -> dict:
        payload = self._build_payload(
            prompt,
            system_prompt=system_prompt,
            temperature=temperature,
            max_tokens=1024,
            response_mime="application/json",
        )
        data, _ = await self._post(payload)
        text = self._extract_text(data)
        self._increment_count()
        return self._parse_json(text)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _build_payload(
        self,
        prompt: str,
        *,
        system_prompt: str,
        temperature: float,
        max_tokens: int,
        response_mime: str,
    ) -> dict:
        payload: dict = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {
                "temperature": temperature,
                "maxOutputTokens": max_tokens,
                "responseMimeType": response_mime,
            },
        }
        if system_prompt:
            payload["systemInstruction"] = {
                "parts": [{"text": system_prompt}]
            }
        return payload

    async def _post(self, payload: dict) -> tuple[dict, float]:
        """Send a generateContent request and return (response_json, elapsed_ms)."""
        url = (
            f"{_GEMINI_API_BASE}/{self.model}:generateContent"
            f"?key={self.api_key}"
        )
        start = time.monotonic()
        resp = await self._client.post(url, json=payload)
        resp.raise_for_status()
        elapsed = (time.monotonic() - start) * 1000
        return resp.json(), elapsed

    @staticmethod
    def _extract_text(data: dict) -> str:
        """Pull the generated text out of the Gemini response envelope."""
        try:
            return (
                data["candidates"][0]["content"]["parts"][0]["text"]
            )
        except (KeyError, IndexError, TypeError):
            return ""

    @staticmethod
    def _extract_token_count(data: dict) -> int:
        """Pull total token usage from the usageMetadata field."""
        try:
            return data["usageMetadata"].get("totalTokenCount", 0)
        except (KeyError, TypeError):
            return 0

    @staticmethod
    def _parse_json(text: str) -> dict:
        """Best-effort parse of a JSON string."""
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass
        start_idx = text.find("{")
        end_idx = text.rfind("}") + 1
        if start_idx >= 0 and end_idx > start_idx:
            try:
                return json.loads(text[start_idx:end_idx])
            except json.JSONDecodeError:
                pass
        return {}
