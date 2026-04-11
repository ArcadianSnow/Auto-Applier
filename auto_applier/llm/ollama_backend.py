"""Ollama local LLM backend via HTTP API."""

import json
import time

import httpx

from auto_applier.llm.base import LLMBackend, LLMResponse


def version_gte(actual: str, minimum: str) -> bool:
    """Return True if ``actual`` dotted-numeric version is >= ``minimum``.

    Non-numeric suffixes like '-rc1' are stripped. Returns False on
    parse failure — callers should treat that as "version unknown,
    fail closed".
    """
    def parts(v: str) -> tuple:
        out = []
        for chunk in v.split("."):
            digits = "".join(c for c in chunk if c.isdigit())
            if not digits:
                return ()
            out.append(int(digits))
        return tuple(out)

    a, b = parts(actual), parts(minimum)
    if not a or not b:
        return False
    return a >= b


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
            # Match on full tag first (e.g. "gemma4:e4b"), then fall back
            # to family prefix ("gemma4") so variants like gemma4:e4b-instruct
            # also count as available.
            target = self.model
            family = target.split(":")[0]
            for m in models:
                name = m.get("name", "")
                if name == target or name.startswith(target + "-"):
                    return True
                if name.startswith(family + ":") or name.startswith(family + "-"):
                    return True
            return False
        except (httpx.ConnectError, httpx.TimeoutException, Exception):
            return False

    async def get_version(self) -> str:
        """Return the running Ollama server version, or '' if unreachable."""
        try:
            resp = await self._client.get(f"{self.base_url}/api/version")
            if resp.status_code != 200:
                return ""
            return resp.json().get("version", "")
        except (httpx.ConnectError, httpx.TimeoutException, Exception):
            return ""

    async def list_local_models(self) -> list[str]:
        """Return a list of model tags currently pulled on the Ollama server."""
        try:
            resp = await self._client.get(f"{self.base_url}/api/tags")
            if resp.status_code != 200:
                return []
            return [m.get("name", "") for m in resp.json().get("models", [])]
        except (httpx.ConnectError, httpx.TimeoutException, Exception):
            return []

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
