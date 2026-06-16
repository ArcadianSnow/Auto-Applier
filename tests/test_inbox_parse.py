"""Email parsing (email-outcome-loop Phase A) — pure / offline.

Covers multipart / HTML-only / encoded-subject / missing-Message-ID via fixture
.eml bytes. No IMAP, no DB.
"""

from __future__ import annotations

from email.message import EmailMessage
from pathlib import Path

import pytest

from auto_applier.inbox.parse import FetchedEmail, parse_message

FIXTURES = Path(__file__).parent / "fixtures" / "inbox"


def _load(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


# --------------------------------------------------------------- plain-text


class TestPlainText:

    def test_confirmation_fields(self):
        fe = parse_message(_load("confirmation.eml"), uid="42")
        assert isinstance(fe, FetchedEmail)
        assert fe.uid == "42"
        assert fe.subject == "Thank you for applying to Acme"
        assert fe.from_addr == "no-reply@acme.com"
        assert fe.from_name == "Acme Careers"
        assert "received your application" in fe.body_text.lower()
        assert "boards.greenhouse.io/acme/jobs/12345" in fe.body_text
        assert fe.message_id == "<confirm-001@acme.com>"
        assert fe.raw_size > 0

    def test_from_addr_lowercased(self):
        raw = (
            b"From: Loud Sender <Recruiting@EXAMPLE.COM>\r\n"
            b"Subject: hi\r\n"
            b"Message-ID: <x@example.com>\r\n"
            b"\r\n"
            b"body\r\n"
        )
        fe = parse_message(raw)
        assert fe.from_addr == "recruiting@example.com"
        assert fe.from_name == "Loud Sender"


# --------------------------------------------------------------- HTML-only


class TestHtmlOnly:

    def test_html_stripped_to_text(self):
        fe = parse_message(_load("html_only.eml"))
        # tags gone, entities unescaped, script/style content dropped
        assert "<" not in fe.body_text and ">" not in fe.body_text
        assert "var a=1" not in fe.body_text
        assert "color:red" not in fe.body_text
        assert "Hi Joseph" in fe.body_text          # &nbsp; → space
        assert "Data Analyst" in fe.body_text        # <b> contents kept
        assert "received your application" in fe.body_text.lower()

    def test_multipart_prefers_plain(self):
        msg = EmailMessage()
        msg["From"] = "Team <team@multi.com>"
        msg["Subject"] = "multipart"
        msg["Message-ID"] = "<mp@multi.com>"
        msg.set_content("PLAIN BODY here")
        msg.add_alternative("<p>HTML BODY here</p>", subtype="html")
        fe = parse_message(msg.as_bytes())
        assert "PLAIN BODY here" in fe.body_text
        assert "HTML BODY" not in fe.body_text


# --------------------------------------------------------------- header edge cases


class TestHeaderEdges:

    def test_encoded_subject_decoded(self):
        fe = parse_message(_load("encoded_no_msgid.eml"))
        assert fe.subject == "Thank you for applying to Soylent"
        assert fe.from_name == "Soylent Recruiting"
        assert fe.from_addr == "recruiting@soylent.com"

    def test_missing_message_id_synthesized_and_stable(self):
        raw = _load("encoded_no_msgid.eml")
        fe1 = parse_message(raw)
        fe2 = parse_message(raw)
        assert fe1.message_id  # non-empty
        assert fe1.message_id.startswith("<synthetic-")
        # derived only from content → stable across re-parses, never clock/random
        assert fe1.message_id == fe2.message_id

    def test_missing_subject_tolerated(self):
        raw = (
            b"From: x@example.com\r\n"
            b"\r\n"
            b"just a body\r\n"
        )
        fe = parse_message(raw)
        assert fe.subject == ""
        assert fe.from_addr == "x@example.com"
        assert fe.from_name == ""
        assert "just a body" in fe.body_text
        assert fe.message_id.startswith("<synthetic-")
