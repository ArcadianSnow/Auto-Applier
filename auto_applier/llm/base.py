"""LLM backend interface and shared types."""

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class LLMResponse:
    """Response from any LLM backend."""

    text: str
    model: str
    tokens_used: int
    cached: bool
    latency_ms: float


class LLMBackend(ABC):
    """Abstract base for all LLM providers."""

    name: str  # "ollama", "gemini", "rule-based"

    @abstractmethod
    async def is_available(self) -> bool:
        """Check if this backend is ready to serve requests."""
        ...

    @abstractmethod
    async def complete(
        self,
        prompt: str,
        system_prompt: str = "",
        temperature: float = 0.3,
        max_tokens: int = 1024,
    ) -> LLMResponse:
        """Generate a text completion."""
        ...

    @abstractmethod
    async def complete_json(
        self,
        prompt: str,
        system_prompt: str = "",
        temperature: float = 0.1,
    ) -> dict:
        """Generate a completion and parse as JSON."""
        ...
