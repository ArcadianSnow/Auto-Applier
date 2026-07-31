"""The support bundle must be reachable without a terminal.

`av3 export-diagnostics` existed only as a CLI command, so the people this product is FOR —
the non-technical testers who will actually hit the bugs — had no way to send anything back.
A support bundle nobody can produce is not a support bundle.

Two invariants beyond "it works":
  * the web path is **scrubbed only**. `--raw` bundles a verbatim events.db (PII-bearing) and
    stays CLI-only + deliberate; a browser button is exactly the wrong place to make that easy
    to do by accident.
  * the download route serves ONLY diagnostics bundles from the data dir. The filename comes
    off the URL, so a traversal here would hand out arbitrary files from the user's disk.
"""

from __future__ import annotations

import sqlite3
import tarfile

import pytest
from fastapi.testclient import TestClient

from auto_applier.config import Settings
from auto_applier.web import WebState, create_app


@pytest.fixture
def web_state(settings: Settings, conn: sqlite3.Connection) -> WebState:
    return WebState(
        settings=settings,
        app_db_path=settings.app_db_path,
        events_db_path=settings.events_db_path,
    )


@pytest.fixture
def client(web_state: WebState) -> TestClient:
    return TestClient(create_app(state=web_state, service=None))


def test_export_builds_a_bundle_and_reports_where_it_went(client, settings):
    resp = client.post("/api/diagnostics/export")
    assert resp.status_code == 200
    body = resp.json()

    assert body["ok"] is True
    assert body["scrubbed"] is True
    assert body["name"].startswith("diagnostics-") and body["name"].endswith(".tar.gz")
    assert body["bytes"] > 0
    assert (settings.data_dir / body["name"]).is_file()
    assert body["download_url"] == f"/api/diagnostics/download/{body['name']}"


def test_the_web_bundle_never_contains_the_raw_events_db(client, settings):
    """`--raw` is the PII-bearing escape hatch and must stay CLI-only."""
    name = client.post("/api/diagnostics/export").json()["name"]
    with tarfile.open(settings.data_dir / name) as tar:
        members = tar.getnames()
    assert "events.db" not in members
    assert any(m.endswith(".json") for m in members)


def test_download_streams_the_bundle(client):
    name = client.post("/api/diagnostics/export").json()["name"]
    resp = client.get(f"/api/diagnostics/download/{name}")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/gzip"
    assert resp.content[:2] == b"\x1f\x8b"          # gzip magic
    assert name in resp.headers.get("content-disposition", "")


def test_download_404s_for_a_bundle_that_does_not_exist(client):
    resp = client.get("/api/diagnostics/download/diagnostics-20260101T000000Z.tar.gz")
    assert resp.status_code == 404


@pytest.mark.parametrize("name", [
    "app.db",                                   # a real file in the data dir, but not a bundle
    "master.json",
    "diagnostics-x.tar.gz.evil",
    "not-a-bundle.tar.gz",
])
def test_download_refuses_anything_that_is_not_a_bundle(client, name):
    assert client.get(f"/api/diagnostics/download/{name}").status_code == 400


def test_download_refuses_path_traversal(client, settings, tmp_path):
    """The filename comes off the URL — a traversal here would serve arbitrary files."""
    secret = settings.data_dir.parent / "secret.tar.gz"
    secret.write_bytes(b"\x1f\x8b topsecret")
    for attempt in ("../secret.tar.gz", "..%2Fsecret.tar.gz", "sub/diagnostics-a.tar.gz"):
        resp = client.get(f"/api/diagnostics/download/{attempt}")
        assert resp.status_code in (400, 404), attempt
        assert b"topsecret" not in resp.content


def test_report_a_problem_is_on_every_page(client):
    """It lives in the shared footer so a tester can reach it from wherever they got stuck."""
    for path in ("/", "/copilot", "/in-progress"):
        html = client.get(path).text
        assert "Report a problem" in html, path
        assert "/api/diagnostics/export" in html, path


def test_bundle_filename_is_url_and_mail_safe(settings):
    """The stamp is meant to end in Z. Stripping colons first turned "+00:00" into "+0000",
    so the Z replacement never fired and every bundle carried a "+" that then had to survive
    URLs, shells and mail clients."""
    from auto_applier.telemetry.diagnostics import build_diagnostics

    name = build_diagnostics(settings, raw=False).path.name
    assert "+" not in name and ":" not in name
    assert name.endswith(".tar.gz")
    assert "Z." in name          # …T203320Z.tar.gz
