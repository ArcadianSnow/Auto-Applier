"""Greenhouse discovery adapter (spec §6a). Offline via httpx.MockTransport + one live
integration test (``pytest -m integration``)."""

from __future__ import annotations

import json

import httpx
import pytest

from av3.sources.greenhouse import (
    GreenhouseError,
    GreenhouseSource,
    confirm_probe,
    html_to_text,
)

# --- a minimal fake of the Greenhouse Job Board API -------------------------
_JOBS_PAYLOAD = {
    "jobs": [
        {
            "id": 12345,
            "title": "Senior Data Analyst",
            "location": {"name": "Remote - US"},
            "updated_at": "2026-05-20T00:00:00Z",
        },
        {"id": 67890, "title": "Data Engineer", "location": {"name": "NYC"}},
    ]
}
_BOARD_PAYLOAD = {"name": "Acme Corp"}
_JOB_CONTENT = {"content": "&lt;p&gt;Build &lt;b&gt;pipelines&lt;/b&gt;.&lt;/p&gt;"}


def _handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path.endswith("/boards/acme/jobs/12345"):
        return httpx.Response(200, json=_JOB_CONTENT)
    if path.endswith("/boards/acme/jobs"):
        return httpx.Response(200, json=_JOBS_PAYLOAD)
    if path.endswith("/boards/acme"):
        return httpx.Response(200, json=_BOARD_PAYLOAD)
    if path.endswith("/boards/empty/jobs"):
        return httpx.Response(200, json={"jobs": []})
    if path.endswith("/boards/missing/jobs"):
        return httpx.Response(404, text="not found")
    return httpx.Response(500)


@pytest.fixture
def src() -> GreenhouseSource:
    client = httpx.Client(transport=httpx.MockTransport(_handler))
    s = GreenhouseSource(client=client, min_interval_s=0.0)
    yield s
    s.close()


def test_html_to_text_unescapes_and_strips():
    out = html_to_text("&lt;p&gt;Hello &amp; &lt;b&gt;world&lt;/b&gt;&lt;/p&gt;")
    # entities decoded, tags stripped (each tag → newline); content survives
    assert "Hello &" in out and "world" in out
    assert "<" not in out and ">" not in out


def test_html_to_text_empty():
    assert html_to_text("") == ""


def test_discover_parses_listings(src):
    listings = src.discover("acme")
    assert len(listings) == 2
    first = listings[0]
    assert first.title == "Senior Data Analyst"
    assert first.company == "Acme Corp"  # resolved from board metadata
    assert first.location == "Remote - US"
    assert first.source_job_id == "12345"
    # canonical hosted URL (drive THIS, not a company wrapper)
    assert first.url == "https://job-boards.greenhouse.io/acme/jobs/12345"


def test_describe_fetches_full_text(src):
    listings = src.discover("acme")
    text = src.describe(listings[0])
    assert "Build" in text and "pipelines" in text
    assert "<" not in text  # HTML stripped
    assert listings[0].description == text  # mutated in place


def test_discover_bad_token_raises(src):
    with pytest.raises(GreenhouseError, match="not found"):
        src.discover("missing")


def test_confirm_probe(src):
    # confirm_probe uses its own client param
    client = httpx.Client(transport=httpx.MockTransport(_handler))
    try:
        assert confirm_probe("acme", client=client) == (True, 2)
        assert confirm_probe("empty", client=client) == (False, 0)
        assert confirm_probe("missing", client=client) == (False, 0)
    finally:
        client.close()


@pytest.mark.integration
def test_live_greenhouse_anthropic():
    """Hits the real public API. Run with: pytest -m integration"""
    valid, n = confirm_probe("anthropic")
    assert valid and n > 0
    src = GreenhouseSource()
    try:
        listings = src.discover("anthropic")
        assert len(listings) > 0
        assert listings[0].company == "Anthropic"
        text = src.describe(listings[0])
        assert len(text) > 200
    finally:
        src.close()
