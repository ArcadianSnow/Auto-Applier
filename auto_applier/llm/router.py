"""LLM fallback-chain router.

Tries backends in order -- Ollama (local) -> Gemini (free API) ->
Rule-based (answers.json fuzzy match) -- and returns the first
successful result.  Responses are cached to disk to avoid redundant
calls.
"""

import asyncio
import logging
from typing import Optional

from auto_applier.llm.base import LLMBackend, LLMResponse
from auto_applier.llm.cache import ResponseCache
from auto_applier.llm.gemini_backend import GeminiBackend
from auto_applier.llm.ollama_backend import OllamaBackend
from auto_applier.llm.rule_backend import RuleBackend

logger = logging.getLogger(__name__)


class LLMRouter:
    """Orchestrates the LLM fallback chain with caching.

    Usage::

        router = LLMRouter()
        await router.initialize()
        response = await router.complete("What is your name?")
    """

    def __init__(
        self,
        backends: Optional[list[LLMBackend]] = None,
        cache: Optional[ResponseCache] = None,
    ) -> None:
        self.backends: list[LLMBackend] = backends or [
            OllamaBackend(),
            GeminiBackend(),
            RuleBackend(),
        ]
        self.cache = cache or ResponseCache()
        self._availability: dict[str, bool] = {}
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Initialization
    # ------------------------------------------------------------------

    async def initialize(self) -> dict[str, bool]:
        """Probe every backend and log which are available.

        Returns a dict mapping backend names to availability booleans.
        """
        self._availability = {}
        for backend in self.backends:
            try:
                available = await backend.is_available()
            except Exception:
                available = False
            self._availability[backend.name] = available
            status = "available" if available else "unavailable"
            logger.info("LLM backend %-12s : %s", backend.name, status)
        return dict(self._availability)

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def active_backend(self) -> str:
        """Name of the first available backend, or ``'none'``."""
        for backend in self.backends:
            if self._availability.get(backend.name, False):
                return backend.name
        return "none"

    # ------------------------------------------------------------------
    # Text completion
    # ------------------------------------------------------------------

    async def complete(
        self,
        prompt: str,
        system_prompt: str = "",
        temperature: float = 0.3,
        max_tokens: int = 1024,
        use_cache: bool = True,
    ) -> LLMResponse:
        """Try cache, then each backend in order until one succeeds."""
        async with self._lock:
            # Check cache first
            if use_cache:
                cached = self.cache.get(system_prompt, prompt)
                if cached is not None:
                    logger.debug("Cache hit for prompt (%.40s...)", prompt)
                    return cached

            # Try each backend in fallback order
            for backend in self.backends:
                if not self._availability.get(backend.name, False):
                    continue
                try:
                    response = await backend.complete(
                        prompt,
                        system_prompt=system_prompt,
                        temperature=temperature,
                        max_tokens=max_tokens,
                    )
                    if response.text:
                        if use_cache:
                            self.cache.put(system_prompt, prompt, response)
                        logger.debug(
                            "Backend %s answered (%.0fms, %d tokens)",
                            backend.name,
                            response.latency_ms,
                            response.tokens_used,
                        )
                        return response
                except Exception as exc:
                    logger.warning(
                        "Backend %s failed: %s", backend.name, exc
                    )
                    # Mark as unavailable so subsequent calls skip it
                    self._availability[backend.name] = False

            # All backends failed -- return empty response
            logger.error("All LLM backends failed for prompt")
            return LLMResponse(
                text="",
                model="none",
                tokens_used=0,
                cached=False,
                latency_ms=0.0,
            )

    # ------------------------------------------------------------------
    # JSON completion
    # ------------------------------------------------------------------

    async def complete_json(
        self,
        prompt: str,
        system_prompt: str = "",
        temperature: float = 0.1,
        use_cache: bool = True,
    ) -> dict:
        """Try cache, then each backend in order for JSON output."""
        async with self._lock:
            # Check cache (stored as text, needs JSON parse)
            if use_cache:
                cached = self.cache.get(system_prompt, prompt)
                if cached is not None and cached.text:
                    import json

                    try:
                        return json.loads(cached.text)
                    except json.JSONDecodeError:
                        pass

            for backend in self.backends:
                if not self._availability.get(backend.name, False):
                    continue
                try:
                    result = await backend.complete_json(
                        prompt,
                        system_prompt=system_prompt,
                        temperature=temperature,
                    )
                    if result:
                        # Cache the JSON as text for later retrieval
                        if use_cache:
                            import json

                            cache_resp = LLMResponse(
                                text=json.dumps(result),
                                model=backend.name,
                                tokens_used=0,
                                cached=False,
                                latency_ms=0.0,
                            )
                            self.cache.put(
                                system_prompt, prompt, cache_resp
                            )
                        logger.debug(
                            "Backend %s returned JSON", backend.name
                        )
                        return result
                except Exception as exc:
                    logger.warning(
                        "Backend %s JSON failed: %s", backend.name, exc
                    )
                    self._availability[backend.name] = False

            logger.error("All LLM backends failed for JSON prompt")
            return {}

    # ------------------------------------------------------------------
    # Maintenance
    # ------------------------------------------------------------------

    async def refresh_availability(self) -> dict[str, bool]:
        """Re-probe all backends (e.g. after Ollama was started late)."""
        return await self.initialize()

    def clear_cache(self) -> int:
        """Remove all cached responses. Returns count of deleted files."""
        return self.cache.clear_all()

    def clear_expired_cache(self) -> int:
        """Remove expired cache entries. Returns count of deleted files."""
        return self.cache.clear_expired()
