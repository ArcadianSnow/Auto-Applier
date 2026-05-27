"""Lever + Ashby discovery adapters (spec §6a). Offline MockTransport + live integration."""

from __future__ import annotations

import httpx
import pytest

from av3.sources.ashby import AshbySource, confirm_probe as ashby_probe
from av3.sources.lever import LeverSource, confirm_probe as lever_probe


# ------------------------------------------------------------------------- Lever
_LEVER_POSTINGS = [
    {
        "id": "uuid-1",
        "text": "Senior Data Analyst",
        "categories": {"location": "Remote - US", "team": "Data"},
        "hostedUrl": "https://jobs.lever.co/acme/uuid-1",
        "applyUrl": "https://jobs.lever.co/acme/uuid-1/apply",
        "descriptionPlain": "Build dashboards and pipelines.",
        "createdAt": 1716000000000,
    }
]


def _lever(handler):
    return LeverSource(client=httpx.Client(transport=httpx.MockTransport(handler)), min_interval_s=0.0)


def test_lever_discover():
    def handler(req):
        if req.url.path.endswith("/postings/acme"):
            return httpx.Response(200, json=_LEVER_POSTINGS)
        return httpx.Response(404)

    src = _lever(handler)
    try:
        listings = src.discover("acme")
    finally:
        src.close()
    assert len(listings) == 1
    lst = listings[0]
    assert lst.title == "Senior Data Analyst"
    assert lst.apply_url == "https://jobs.lever.co/acme/uuid-1/apply"
    assert lst.description.startswith("Build dashboards")
    assert lst.location == "Remote - US"


def test_lever_discover_bad_site_empty():
    src = _lever(lambda req: httpx.Response(404))
    try:
        assert src.discover("missing") == []
    finally:
        src.close()


def test_lever_confirm_probe():
    client = httpx.Client(transport=httpx.MockTransport(
        lambda req: httpx.Response(200, json=_LEVER_POSTINGS)
        if req.url.path.endswith("/postings/acme") else httpx.Response(404)
    ))
    try:
        assert lever_probe("acme", client=client) == (True, 1)
        assert lever_probe("missing", client=client) == (False, 0)
    finally:
        client.close()


# ------------------------------------------------------------------------- Ashby
_ASHBY_JOBS = {
    "jobs": [
        {
            "id": "a-1",
            "title": "Data Engineer",
            "location": "San Francisco",
            "jobUrl": "https://jobs.ashbyhq.com/Acme/a-1",
            "applyUrl": "https://jobs.ashbyhq.com/Acme/a-1/application",
            "descriptionPlain": "Own the data platform.",
            "isListed": True,
            "publishedAt": "2026-05-20",
        },
        {"id": "a-2", "title": "Hidden", "isListed": False},  # filtered out
    ]
}


def _ashby(handler):
    return AshbySource(client=httpx.Client(transport=httpx.MockTransport(handler)), min_interval_s=0.0)


def test_ashby_discover_filters_unlisted():
    def handler(req):
        if "/job-board/Acme" in req.url.path:
            return httpx.Response(200, json=_ASHBY_JOBS)
        return httpx.Response(404)

    src = _ashby(handler)
    try:
        listings = src.discover("Acme")
    finally:
        src.close()
    assert len(listings) == 1  # unlisted dropped
    assert listings[0].apply_url == "https://jobs.ashbyhq.com/Acme/a-1/application"
    assert src.spa is True


def test_ashby_confirm_probe():
    client = httpx.Client(transport=httpx.MockTransport(
        lambda req: httpx.Response(200, json=_ASHBY_JOBS)
        if "/job-board/Acme" in req.url.path else httpx.Response(404)
    ))
    try:
        assert ashby_probe("Acme", client=client) == (True, 2)  # counts raw jobs
        assert ashby_probe("missing", client=client) == (False, 0)
    finally:
        client.close()


# ------------------------------------------------------------------- integration
@pytest.mark.integration
def test_live_lever_matchgroup():
    valid, n = lever_probe("matchgroup")
    assert valid and n > 0
    src = LeverSource()
    try:
        listings = src.discover("matchgroup")
        assert listings and listings[0].apply_url.endswith("/apply")
        assert listings[0].description  # descriptionPlain included
    finally:
        src.close()


@pytest.mark.integration
def test_live_ashby_board():
    valid, n = ashby_probe("Ashby")
    assert valid and n > 0
    src = AshbySource()
    try:
        listings = src.discover("Ashby")
        assert listings and "/application" in listings[0].apply_url
    finally:
        src.close()
