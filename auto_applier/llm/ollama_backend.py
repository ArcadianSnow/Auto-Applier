"""Ollama local LLM backend via HTTP API."""

import json
import time

import httpx

from auto_applier.llm.base import LLMBackend, LLMResponse


class OllamaBackend(LLMBackend):
    """Local Ollama server backend.

    Communicates with the Ollama REST API at ``/api/generate``.
    Preferred backend because it is entirely free and local.
    """

    name = "ollama"

    def __init__(self, base_url: str = "", model: str = "") -> None:
        from auto_applier.config import OLLAMA_BASE_URL, OLLAMA_MODEL

        self.base_url = (base_url or OLLAMA_BASE_URL).rstrip("/")
        self.model = model or OLLAMA_MODEL
        self._client = httpx.AsyncClient(timeout=120.0)

    # ------------------------------------------------------------------
    # Availability
    # ------------------------------------------------------------------

    async def is_available(self) -> bool:
        """Return *True* if Ollama is running and the target model is pulled."""
        try:
            resp = await self._client.get(f"{self.base_url}/api/tags")
            if resp.status_code != 200:
                return False
            models = resp.json().get("models", [])
            # Allow prefix match so "llama3.1:8b" matches "llama3.1:8b-instruct-…"
            prefix = self.model.split(":")[0]
            return any(
                m.get("name", "").startswith(prefix) for m in models
            )
        except (httpx.ConnectError, httpx.TimeoutException, Exception):
            return False

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
        start = time.monotonic()
        payload: dict = {
            "model": self.model,
            "prompt": prompt,
            "stream": False,
            "options": {
                "temperature": temperature,
                "num_predict": max_tokens,
            },
        }
        if system_prompt:
            payload["system"] = system_prompt

        resp = await self._client.post(
            f"{self.base_url}/api/generate", json=payload
        )
        resp.raise_for_status()
        data = resp.json()

        elapsed = (time.monotonic() - start) * 1000
        return LLMResponse(
            text=data.get("response", ""),
            model=self.model,
            tokens_used=data.get("eval_count", 0),
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
        start = time.monotonic()
        payload: dict = {
            "model": self.model,
            "prompt": prompt,
            "system": system_prompt or "Respond with valid JSON only.",
            "stream": False,
            "format": "json",
            "options": {"temperature": temperature},
        }

        resp = await self._client.post(
            f"{self.base_url}/api/generate", json=payload
        )
        resp.raise_for_status()
        data = resp.json()

        text = data.get("response", "{}")
        return self._parse_json(text)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_json(text: str) -> dict:
        """Best-effort parse of a JSON string, with brace extraction fallback."""
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass
        # Try to extract the outermost { ... } block
        start_idx = text.find("{")
        end_idx = text.rfind("}") + 1
        if start_idx >= 0 and end_idx > start_idx:
            try:
                return json.loads(text[start_idx:end_idx])
            except json.JSONDecodeError:
                pass
        return {}
