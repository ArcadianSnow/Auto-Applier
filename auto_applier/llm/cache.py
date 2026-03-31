"""File-based LLM response cache with TTL expiry."""

import hashlib
import json
import time
from datetime import datetime, timezone
from pathlib import Path

from auto_applier.llm.base import LLMResponse


class ResponseCache:
    """Disk-backed cache for LLM responses.

    Each entry is a JSON file in *cache_dir* named by the SHA-256 hash
    (first 16 hex chars) of the combined prompt.  Entries expire after
    *ttl_hours* (default 72).
    """

    def __init__(
        self,
        cache_dir: str | Path = "",
        ttl_hours: float = 72.0,
    ) -> None:
        from auto_applier.config import CACHE_DIR

        self.cache_dir = Path(cache_dir) if cache_dir else CACHE_DIR
        self.ttl_hours = ttl_hours
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Key derivation
    # ------------------------------------------------------------------

    @staticmethod
    def _make_key(system_prompt: str, prompt: str) -> str:
        """SHA-256 hash (first 16 hex chars) of the combined prompts."""
        raw = f"{system_prompt}||{prompt}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]

    def _key_path(self, key: str) -> Path:
        return self.cache_dir / f"{key}.json"

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    def get(self, system_prompt: str, prompt: str) -> LLMResponse | None:
        """Return cached response or *None* if missing / expired."""
        key = self._make_key(system_prompt, prompt)
        path = self._key_path(key)
        if not path.exists():
            return None

        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (json.JSONDecodeError, OSError):
            return None

        # Check TTL
        created_at = data.get("created_at", "")
        if created_at:
            try:
                created = datetime.fromisoformat(created_at)
                age_hours = (
                    datetime.now(timezone.utc) - created
                ).total_seconds() / 3600
                if age_hours > self.ttl_hours:
                    path.unlink(missing_ok=True)
                    return None
            except (ValueError, TypeError):
                pass

        return LLMResponse(
            text=data.get("text", ""),
            model=data.get("model", "cached"),
            tokens_used=data.get("tokens_used", 0),
            cached=True,
            latency_ms=0.0,
        )

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    def put(
        self, system_prompt: str, prompt: str, response: LLMResponse
    ) -> None:
        """Persist a response to the cache directory."""
        key = self._make_key(system_prompt, prompt)
        path = self._key_path(key)
        entry = {
            "text": response.text,
            "model": response.model,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "tokens_used": response.tokens_used,
        }
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(entry, fh, indent=2, ensure_ascii=False)

    # ------------------------------------------------------------------
    # Maintenance
    # ------------------------------------------------------------------

    def clear_expired(self) -> int:
        """Remove cache files older than TTL. Returns count of deleted files."""
        deleted = 0
        now = time.time()
        ttl_seconds = self.ttl_hours * 3600

        for path in self.cache_dir.glob("*.json"):
            try:
                age = now - path.stat().st_mtime
                if age > ttl_seconds:
                    path.unlink(missing_ok=True)
                    deleted += 1
            except OSError:
                continue
        return deleted

    def clear_all(self) -> int:
        """Remove every cache file. Returns count of deleted files."""
        deleted = 0
        for path in self.cache_dir.glob("*.json"):
            try:
                path.unlink(missing_ok=True)
                deleted += 1
            except OSError:
                continue
        return deleted
