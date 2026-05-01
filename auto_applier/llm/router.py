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
        # Per-method consecutive-failure tracking. A backend that's
        # great at plain text but garbage at structured JSON used to
        # get disabled for EVERYTHING after 3 JSON failures, breaking
        # text-only callers downstream. Tracking by method means
        # Ollama can keep serving complete() requests even after its
        # complete_json() ran into a schema it couldn't produce.
        self._consecutive_failures: dict[tuple[str, str], int] = {}
        # Per-method disabled flags. Replaces the old per-backend
        # _availability flag for failure-tracking purposes (initial
        # availability probe still uses _availability for is_available
        # checks).
        self._method_disabled: dict[tuple[str, str], bool] = {}
        self._lock = asyncio.Lock()

    # Disable a backend only after this many *consecutive* failures.
    # A single transient httpx blip or Ollama warmup timeout used to
    # poison the backend for the rest of the run, leaving rule-based
    # as the only option (which returns "" for anything not in
    # answers.json) and producing the misleading "All LLM backends
    # failed" cascade. Reset on the next success.
    _FAILURE_DISABLE_THRESHOLD = 3

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
                if self._method_disabled.get((backend.name, "complete"), False):
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
                        self._consecutive_failures[(backend.name, "complete")] = 0
                        logger.debug(
                            "Backend %s answered (%.0fms, %d tokens)",
                            backend.name,
                            response.latency_ms,
                            response.tokens_used,
                        )
                        return response
                    # Empty-text response counts as a soft failure: the
                    # backend is up but isn't producing usable output
                    # (Gemma 4 sometimes returns "" on long prompts).
                    # Bump the failure counter so a stuck backend gets
                    # disabled after threshold instead of silently
                    # falling through forever.
                    self._record_failure(
                        backend.name,
                        RuntimeError("empty response"),
                        method="complete",
                    )
                except Exception as exc:
                    self._record_failure(backend.name, exc, method="complete")

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
                if self._method_disabled.get(
                    (backend.name, "complete_json"), False,
                ):
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
                        self._consecutive_failures[
                            (backend.name, "complete_json")
                        ] = 0
                        logger.debug(
                            "Backend %s returned JSON", backend.name
                        )
                        return result
                    # Empty-dict / None response is a soft failure —
                    # backend is up but the model couldn't produce
                    # parseable JSON. Without bumping the counter,
                    # Ollama returning {} forever just slows every
                    # call by always trying it first.
                    self._record_failure(
                        backend.name,
                        RuntimeError("empty JSON"),
                        method="complete_json",
                    )
                except Exception as exc:
                    self._record_failure(
                        backend.name, exc, method="complete_json",
                    )

            logger.error("All LLM backends failed for JSON prompt")
            return {}

    # ------------------------------------------------------------------
    # Failure tracking
    # ------------------------------------------------------------------

    def _record_failure(
        self,
        backend_name: str,
        exc: Exception,
        method: str = "complete",
        kind: str = "",
    ) -> None:
        """Bump per-method consecutive-failure count and disable past threshold.

        Per-method tracking ensures a backend that's bad at JSON but
        fine at text doesn't lose its text-completion ability when
        ``complete_json()`` repeatedly fails (which used to break
        every text-mode caller after one stuck JSON prompt).
        """
        key = (backend_name, method)
        count = self._consecutive_failures.get(key, 0) + 1
        self._consecutive_failures[key] = count
        suffix = f" {kind}" if kind else f" {method}"
        logger.warning(
            "Backend %s%s failed (%d/%d): %s",
            backend_name, suffix, count,
            self._FAILURE_DISABLE_THRESHOLD, exc,
        )
        if count >= self._FAILURE_DISABLE_THRESHOLD:
            # Per-method disable — the backend stays available for
            # other methods so a bad-JSON Ollama can still serve
            # text completions.
            self._method_disabled[key] = True
            logger.warning(
                "Backend %s disabled for %s after %d consecutive failures",
                backend_name, method, count,
            )

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
