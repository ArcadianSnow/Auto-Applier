"""Live IMAP fetch for the email outcome loop (email-outcome-loop Phase C).

**Read-only by construction.** ``select(folder, readonly=True)`` plus *only*
``UID SEARCH`` and ``UID FETCH`` — never ``STORE`` / ``COPY`` / ``EXPUNGE`` — so the
loop physically cannot mark, move, or delete the user's mail. stdlib ``imaplib``
only; no new dependency.

The fetcher is a **re-iterable** source, not a one-shot generator: each
``for (uid, raw) in fetcher`` opens a fresh read-only IMAP session, pulls only mail
newer than the persisted ``last_uid`` cursor (or, on a cold start, the
``since_days`` window), yields ``(uid, raw_bytes)`` in the exact shape the
:class:`~auto_applier.inbox.worker.InboxWorker` consumes, advances the cursor, and
closes. That re-iterability is what lets the always-on scheduler poll every cycle by
simply re-iterating the same object — the in-memory stub (tests) and
:func:`~auto_applier.inbox.worker.eml_file_source` (offline CLI) satisfy the same
``Iterable[tuple[str, bytes]]`` contract.

Iteration is **synchronous** (blocking ``imaplib``) on purpose: the worker iterates
the source inside ``run_once``, and keeping the source a plain iterable lets the stub
/ eml / IMAP sources stay interchangeable. We deliberately do NOT offload to a thread —
the cursor read/write uses the shared (single-thread) sqlite connection, and a brief
per-cycle poll stall is acceptable for a local single-user tool. (If it ever bites in
``serve`` mode, the offload would have to move the cursor I/O off the shared conn.)

Secrets: the app-password is read from ``os.environ["AV3_IMAP_PASSWORD"]`` at connect
time and is never stored on :class:`~auto_applier.config.settings.Settings` or in
``user_config.json`` (the project's ``.env``-only secrets rule).
:func:`creds_from_settings` returns ``None`` when the inbox is unconfigured/disabled so
callers degrade to a setup nudge instead of crashing.
"""

from __future__ import annotations

import imaplib
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Iterator

from auto_applier.config.settings import Settings
from auto_applier.inbox.repo import InboxMessageRepo

__all__ = ["InboxCreds", "creds_from_settings", "ImapFetcher", "IMAP_PASSWORD_ENV"]

#: The ONLY place the IMAP app-password is read from — never a config field.
IMAP_PASSWORD_ENV = "AV3_IMAP_PASSWORD"


@dataclass(frozen=True)
class InboxCreds:
    """Everything needed to open a read-only IMAP session.

    The password comes from the environment (never persisted); the rest is the
    non-secret :class:`~auto_applier.config.settings.InboxConfig`.
    """

    host: str
    port: int
    user: str
    password: str
    folder: str
    since_days: int


def creds_from_settings(settings: Settings) -> InboxCreds | None:
    """Build :class:`InboxCreds` from settings + ``$AV3_IMAP_PASSWORD``.

    Returns ``None`` (never raises) when the loop can't run yet — inbox disabled,
    no user configured, or the app-password env var unset/blank — so the CLI and
    scheduler degrade to the friendly "set this up" path. Live use requires ALL
    THREE: ``inbox.enabled``, ``inbox.user``, and the env password.
    """
    ic = settings.inbox
    if not ic.enabled:
        return None
    user = (ic.user or "").strip()
    if not user:
        return None
    password = os.environ.get(IMAP_PASSWORD_ENV, "").strip()
    if not password:
        return None
    return InboxCreds(
        host=ic.host,
        port=ic.port,
        user=user,
        password=password,
        folder=ic.folder,
        since_days=ic.since_days,
    )


# A factory so tests inject a fake IMAP client instead of a live SSL socket.
ImapFactory = Callable[[str, int], "imaplib.IMAP4"]


def _default_imap_factory(host: str, port: int) -> imaplib.IMAP4:
    return imaplib.IMAP4_SSL(host, port)


class ImapFetcher:
    """Re-iterable, read-only IMAP source of ``(uid, raw_bytes)`` tuples.

    Conforms to the ``Iterable[tuple[str, bytes]]`` the :class:`InboxWorker`
    consumes. Iterating opens a fresh session each time, so the scheduler can poll
    by re-iterating; the offline ``.eml`` path and the test stub are the other two
    sources that satisfy the same shape.

    Read-only by construction: ``select(..., readonly=True)`` + only
    ``uid('SEARCH', ...)`` / ``uid('FETCH', ...)``. No mailbox-mutating verb appears
    anywhere in this class.

    ``advance_cursor=False`` (the ``--dry-run`` posture) fetches without persisting
    the ``last_uid`` cursor, so a dry run can be repeated. ``max_messages`` bounds a
    single poll so a huge cold-start backlog can't stall one cycle (the rest are
    picked up next poll once the cursor has advanced).
    """

    def __init__(
        self,
        creds: InboxCreds,
        repo: InboxMessageRepo,
        *,
        since_days: int | None = None,
        advance_cursor: bool = True,
        max_messages: int = 200,
        imap_factory: ImapFactory | None = None,
        now: Callable[[], datetime] | None = None,
    ):
        self._creds = creds
        self._repo = repo
        self._since_days = since_days if since_days is not None else creds.since_days
        self._advance_cursor = advance_cursor
        self._max_messages = max_messages
        self._imap_factory = imap_factory or _default_imap_factory
        self._now = now or (lambda: datetime.now(timezone.utc))

    def __iter__(self) -> Iterator[tuple[str, bytes]]:
        folder = self._creds.folder
        last = self._repo.last_uid(folder)  # sqlite read (caller's thread)
        messages, max_uid = self._poll(last)
        for uid, raw in messages:
            yield (str(uid), raw)
        # Advance only AFTER the consumer has drained the batch, and only past the
        # prior cursor — a no-new-mail poll never moves it, and a dry run never does.
        if self._advance_cursor and max_uid is not None and max_uid > (last or 0):
            self._repo.set_last_uid(folder, max_uid)

    # -- blocking IMAP (no sqlite in here) --------------------------------

    def _poll(self, last: int | None) -> tuple[list[tuple[int, bytes]], int | None]:
        """Connect, search, fetch. Returns ``(messages, max_uid)``.

        Pure network + parsing; touches no sqlite so the cursor I/O stays on the
        caller's thread. ``max_uid`` is the highest UID actually fetched (None when
        nothing new), used by :meth:`__iter__` to advance the cursor.
        """
        client = self._imap_factory(self._creds.host, self._creds.port)
        try:
            client.login(self._creds.user, self._creds.password)
            client.select(self._creds.folder, readonly=True)
            uids = self._search_uids(client, last)
            messages: list[tuple[int, bytes]] = []
            max_uid: int | None = None
            for uid in uids:
                raw = self._fetch_one(client, uid)
                if raw is None:
                    continue
                messages.append((uid, raw))
                if max_uid is None or uid > max_uid:
                    max_uid = uid
            return messages, max_uid
        finally:
            self._safe_logout(client)

    def _search_uids(self, client: imaplib.IMAP4, last: int | None) -> list[int]:
        """UIDs to fetch this poll: strictly newer than the cursor, else the
        ``since_days`` window on a cold start."""
        if last:
            # IMAP quirk: ``UID n:*`` is inclusive of the largest existing UID even
            # when none are newer, so a no-new-mail poll returns the single highest
            # UID — we filter to strictly-greater below.
            typ, data = client.uid("SEARCH", None, f"UID {last + 1}:*")
        else:
            since = (self._now() - timedelta(days=self._since_days)).strftime("%d-%b-%Y")
            typ, data = client.uid("SEARCH", None, "SINCE", since)
        if typ != "OK" or not data or data[0] is None:
            return []
        raw = data[0]
        if isinstance(raw, (bytes, bytearray)):
            raw = bytes(raw).decode("ascii", "ignore")
        uids = sorted(int(tok) for tok in str(raw).split() if tok.isdigit())
        if last:
            uids = [u for u in uids if u > last]
        return uids[: self._max_messages]

    @staticmethod
    def _fetch_one(client: imaplib.IMAP4, uid: int) -> bytes | None:
        """Fetch one message's RFC822 bytes by UID. None on any non-OK / odd shape."""
        typ, data = client.uid("FETCH", str(uid), "(RFC822)")
        if typ != "OK" or not data:
            return None
        for part in data:
            # imaplib returns the body as the 2nd element of a (header, body) tuple.
            if isinstance(part, tuple) and len(part) >= 2 and isinstance(
                part[1], (bytes, bytearray)
            ):
                return bytes(part[1])
        return None

    @staticmethod
    def _safe_logout(client: imaplib.IMAP4) -> None:
        """Best-effort teardown. ``CLOSE`` on a read-only mailbox never expunges."""
        try:
            client.close()
        except Exception:  # noqa: BLE001 — teardown must never mask a fetch error
            pass
        try:
            client.logout()
        except Exception:  # noqa: BLE001
            pass
