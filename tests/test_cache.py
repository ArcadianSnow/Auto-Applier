"""Tests for llm/cache.py — ResponseCache TTL, hits, misses, maintenance."""

import json
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pytest

from auto_applier.llm.base import LLMResponse
from auto_applier.llm.cache import ResponseCache


@pytest.fixture
def cache(tmp_path):
    """Return a cache backed by a temp directory with a short TTL."""
    return ResponseCache(cache_dir=tmp_path, ttl_hours=1.0)


def _resp(text="hello", model="test-model"):
    return LLMResponse(text=text, model=model, tokens_used=10, cached=False, latency_ms=5.0)


# ------------------------------------------------------------------
# Key derivation
# ------------------------------------------------------------------

class TestKeyDerivation:
    def test_deterministic(self):
        k1 = ResponseCache._make_key("sys", "prompt")
        k2 = ResponseCache._make_key("sys", "prompt")
        assert k1 == k2

    def test_different_prompts_differ(self):
        k1 = ResponseCache._make_key("sys", "a")
        k2 = ResponseCache._make_key("sys", "b")
        assert k1 != k2

    def test_different_system_prompts_differ(self):
        k1 = ResponseCache._make_key("sys1", "prompt")
        k2 = ResponseCache._make_key("sys2", "prompt")
        assert k1 != k2

    def test_key_is_16_hex_chars(self):
        key = ResponseCache._make_key("x", "y")
        assert len(key) == 16
        int(key, 16)  # should not raise


# ------------------------------------------------------------------
# Put / Get round-trip
# ------------------------------------------------------------------

class TestPutGet:
    def test_cache_miss_returns_none(self, cache):
        assert cache.get("sys", "nonexistent") is None

    def test_round_trip(self, cache):
        cache.put("sys", "prompt", _resp("world"))
        hit = cache.get("sys", "prompt")
        assert hit is not None
        assert hit.text == "world"
        assert hit.model == "test-model"
        assert hit.cached is True
        assert hit.latency_ms == 0.0

    def test_tokens_preserved(self, cache):
        cache.put("s", "p", _resp())
        hit = cache.get("s", "p")
        assert hit.tokens_used == 10

    def test_overwrite(self, cache):
        cache.put("s", "p", _resp("v1"))
        cache.put("s", "p", _resp("v2"))
        assert cache.get("s", "p").text == "v2"


# ------------------------------------------------------------------
# TTL expiry
# ------------------------------------------------------------------

class TestTTL:
    def test_expired_entry_returns_none(self, tmp_path):
        cache = ResponseCache(cache_dir=tmp_path, ttl_hours=0.0001)  # ~0.36 seconds
        cache.put("s", "p", _resp())
        # Backdate the created_at timestamp
        key = cache._make_key("s", "p")
        path = cache._key_path(key)
        data = json.loads(path.read_text())
        data["created_at"] = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        path.write_text(json.dumps(data))

        assert cache.get("s", "p") is None
        # File should have been deleted
        assert not path.exists()

    def test_fresh_entry_is_not_expired(self, cache):
        cache.put("s", "p", _resp())
        assert cache.get("s", "p") is not None

    def test_corrupt_created_at_still_returns_data(self, cache):
        cache.put("s", "p", _resp("ok"))
        key = cache._make_key("s", "p")
        path = cache._key_path(key)
        data = json.loads(path.read_text())
        data["created_at"] = "not-a-date"
        path.write_text(json.dumps(data))
        # Should return the cached response since TTL can't be checked
        assert cache.get("s", "p").text == "ok"

    def test_missing_created_at_still_returns_data(self, cache):
        cache.put("s", "p", _resp("ok"))
        key = cache._make_key("s", "p")
        path = cache._key_path(key)
        data = json.loads(path.read_text())
        del data["created_at"]
        path.write_text(json.dumps(data))
        assert cache.get("s", "p").text == "ok"


# ------------------------------------------------------------------
# Corrupt files
# ------------------------------------------------------------------

class TestCorruptFiles:
    def test_invalid_json_returns_none(self, cache):
        key = cache._make_key("s", "p")
        path = cache._key_path(key)
        path.write_text("not json {{{{")
        assert cache.get("s", "p") is None

    def test_empty_file_returns_none(self, cache):
        key = cache._make_key("s", "p")
        path = cache._key_path(key)
        path.write_text("")
        assert cache.get("s", "p") is None


# ------------------------------------------------------------------
# Maintenance
# ------------------------------------------------------------------

class TestMaintenance:
    def test_clear_all(self, cache):
        for i in range(5):
            cache.put("s", f"p{i}", _resp(f"v{i}"))
        deleted = cache.clear_all()
        assert deleted == 5
        for i in range(5):
            assert cache.get("s", f"p{i}") is None

    def test_clear_expired(self, tmp_path):
        cache = ResponseCache(cache_dir=tmp_path, ttl_hours=1.0)
        # Create one fresh and one stale file
        cache.put("s", "fresh", _resp("new"))

        # Create a stale file by backdating mtime
        cache.put("s", "stale", _resp("old"))
        stale_key = cache._make_key("s", "stale")
        stale_path = cache._key_path(stale_key)
        old_time = time.time() - 7200  # 2 hours ago
        import os
        os.utime(stale_path, (old_time, old_time))

        deleted = cache.clear_expired()
        assert deleted == 1
        assert cache.get("s", "fresh") is not None

    def test_clear_all_on_empty_dir(self, cache):
        assert cache.clear_all() == 0

    def test_clear_expired_on_empty_dir(self, cache):
        assert cache.clear_expired() == 0
