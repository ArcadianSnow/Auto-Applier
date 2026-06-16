"""Pure, offline email parsing (email-outcome-loop Phase A).

Turns the raw RFC822 bytes IMAP hands back into a small, classifier-ready
:class:`FetchedEmail`. Stdlib only (``email`` + ``html``) — no new dependency,
and nothing here touches the network or the DB. Keeping it pure means the
fuzzy parts (subject decoding, multipart text selection, HTML stripping) are
fully testable against fixture bytes.

Design notes:
  * Subjects arrive RFC2047-encoded (``=?utf-8?q?...?=``); we decode them.
  * ``From`` is split into a display name + a lowercased bare address.
  * Body prefers ``text/plain``; if a message is HTML-only we strip tags down
    to plain text deterministically (no parser dependency).
  * ``Message-ID`` is tolerated-missing: we synthesize a STABLE id from a hash
    of subject+from+date so re-parsing the same bytes yields the same id (never
    a clock/random value — Phase B keys inbox idempotency off message_id).
"""

from __future__ import annotations

import email
import hashlib
import html as html_module
import re
from dataclasses import dataclass
from email.header import decode_header, make_header
from email.message import EmailMessage
from email.policy import default as default_policy
from email.utils import parseaddr

__all__ = ["FetchedEmail", "parse_message"]


@dataclass(frozen=True)
class FetchedEmail:
    """One parsed inbox message — the classifier/matcher input. Offline/pure."""

    uid: str
    message_id: str
    subject: str
    from_addr: str       # parsed sender email, lowercased ("" if unparseable)
    from_name: str       # display name ("" if none)
    body_text: str       # text/plain preferred, else HTML stripped to text
    date_iso: str        # raw Date header value (best-effort; "" if absent)
    raw_size: int        # len(raw_bytes), for observability


# --------------------------------------------------------------- header helpers


def _decode_header_value(value: str | None) -> str:
    """Decode an RFC2047-encoded header (``=?utf-8?q?...?=``) to a plain str.

    Tolerant: any decode error falls back to the raw value rather than raising.
    """
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value))).strip()
    except Exception:  # noqa: BLE001 — a malformed header must never crash parse
        return value.strip()


def _parse_from(raw_from: str | None) -> tuple[str, str]:
    """Split a ``From`` header into ``(display_name, lowercased_addr)``."""
    decoded = _decode_header_value(raw_from)
    name, addr = parseaddr(decoded)
    return name.strip(), addr.strip().lower()


# --------------------------------------------------------------- body helpers


_TAG_DROP = re.compile(r"(?is)<(script|style)\b.*?</\1>")
_TAG_ANY = re.compile(r"(?s)<[^>]+>")
_WS = re.compile(r"[ \t\r\f\v]+")
_MULTI_NL = re.compile(r"\n{3,}")


def _strip_html(html: str) -> str:
    """Minimal stdlib HTML → text: drop script/style, tags → spaces, unescape,
    collapse whitespace. Lossy on purpose (the classifier keys on keywords,
    sender, and subject — not layout)."""
    if not html:
        return ""
    text = _TAG_DROP.sub(" ", html)
    # Treat block-ish boundaries as newlines so paragraphs don't run together.
    text = re.sub(r"(?i)<\s*br\s*/?\s*>", "\n", text)
    text = re.sub(r"(?i)</\s*(p|div|li|tr|h[1-6])\s*>", "\n", text)
    text = _TAG_ANY.sub(" ", text)
    text = html_module.unescape(text)
    # Normalize NBSP (&nbsp; → \xa0) and other unicode spaces to a plain space so
    # downstream keyword matching sees uniform whitespace.
    text = text.replace("\xa0", " ").replace("​", "")
    text = _WS.sub(" ", text)
    text = _MULTI_NL.sub("\n\n", text)
    return text.strip()


def _decode_part(part: EmailMessage) -> str:
    """Decode one part's payload to text, honoring the declared charset."""
    try:
        payload = part.get_content()  # policy=default decodes + applies charset
        if isinstance(payload, bytes):
            return payload.decode("utf-8", errors="replace")
        return str(payload)
    except Exception:  # noqa: BLE001 — fall back to raw bytes on any decode issue
        raw = part.get_payload(decode=True)
        if isinstance(raw, bytes):
            return raw.decode("utf-8", errors="replace")
        return str(part.get_payload() or "")


def _best_text_part(msg: EmailMessage) -> str:
    """Pick the best text body: prefer the first ``text/plain``; otherwise strip
    the first ``text/html``. Walks multipart trees, skips attachments."""
    plain: str | None = None
    html: str | None = None
    for part in msg.walk():
        if part.is_multipart():
            continue
        disposition = (part.get_content_disposition() or "").lower()
        if disposition == "attachment":
            continue
        ctype = (part.get_content_type() or "").lower()
        if ctype == "text/plain" and plain is None:
            plain = _decode_part(part)
        elif ctype == "text/html" and html is None:
            html = _decode_part(part)
    if plain is not None and plain.strip():
        return plain.strip()
    if html is not None and html.strip():
        return _strip_html(html)
    # Non-multipart, non-text/* (rare) — best effort on the raw payload.
    if plain is None and html is None:
        return _decode_part(msg).strip()
    return ""


def _synthesize_message_id(subject: str, from_addr: str, date_iso: str) -> str:
    """Stable fallback Message-ID derived ONLY from message content (never a
    clock/random value) so re-parsing the same bytes yields the same id."""
    seed = f"{subject}|{from_addr}|{date_iso}".encode("utf-8")
    digest = hashlib.sha256(seed).hexdigest()[:32]
    return f"<synthetic-{digest}@auto-applier.local>"


# --------------------------------------------------------------- public parse


def parse_message(raw_bytes: bytes, *, uid: str = "") -> FetchedEmail:
    """Parse raw RFC822 bytes into a :class:`FetchedEmail`. Pure / offline.

    A missing ``Message-ID`` is tolerated: a stable id is synthesized from the
    message content (subject + from + date). ``uid`` is the IMAP UID the caller
    fetched this by; defaults to "" in offline tests.
    """
    msg = email.message_from_bytes(raw_bytes, policy=default_policy)

    subject = _decode_header_value(msg.get("Subject"))
    from_name, from_addr = _parse_from(msg.get("From"))
    date_iso = (msg.get("Date") or "").strip()
    body_text = _best_text_part(msg)

    raw_mid = (msg.get("Message-ID") or msg.get("Message-Id") or "").strip()
    message_id = raw_mid or _synthesize_message_id(subject, from_addr, date_iso)

    return FetchedEmail(
        uid=str(uid),
        message_id=message_id,
        subject=subject,
        from_addr=from_addr,
        from_name=from_name,
        body_text=body_text,
        date_iso=date_iso,
        raw_size=len(raw_bytes),
    )
