"""ImapFetcher + creds_from_settings (email-outcome-loop Phase C).

100% offline: a ``FakeIMAP`` stands in for ``imaplib.IMAP4_SSL`` so we exercise the
real fetcher logic — cursor-bounded UID search, RFC822 fetch, the ``UID n:*`` "no new
mail still returns the highest UID" quirk, cursor advance / dry-run hold, and
re-iterability (the scheduler poll) — with zero network. The fake ALSO asserts the
fetcher is read-only: any ``STORE`` / ``COPY`` / ``EXPUNGE`` blows up the test.

The live test (a real mailbox, gated on a Gmail app-password) is ``@pytest.mark.eval``
at the bottom and skips unless ``$AV3_IMAP_PASSWORD`` + ``inbox.user`` are set.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from auto_applier.config.settings import InboxConfig, Settings
from auto_applier.db.repositories import JobRepo
from auto_applier.domain.models import Job
from auto_applier.domain.state import JobState
from auto_applier.inbox.fetcher import ImapFetcher, InboxCreds, creds_from_settings
from auto_applier.inbox.repo import InboxMessageRepo
from auto_applier.inbox.worker import InboxWorker

_FIXTURES = Path(__file__).parent / "fixtures" / "inbox"


# --------------------------------------------------------------- the fake transport

class FakeIMAP:
    """Minimal in-memory IMAP server: a ``{uid: raw_bytes}`` mailbox. Records every
    call. Implements only what the fetcher uses (login / select / uid SEARCH+FETCH /
    close / logout); every *mutating* verb raises so a read-only regression fails loudly.
    """

    def __init__(self, messages: dict[int, bytes]):
        self._messages = dict(messages)
        self.calls: list[tuple] = []
        self.select_readonly: bool | None = None
        self.closed = False
        self.logged_out = False

    # -- the verbs the fetcher uses --------------------------------------
    def login(self, user, password):
        self.calls.append(("login", user))
        return ("OK", [b"LOGIN completed"])

    def select(self, folder, readonly=False):
        self.select_readonly = readonly
        self.calls.append(("select", folder, readonly))
        return ("OK", [str(len(self._messages)).encode()])

    def uid(self, command, *args):
        self.calls.append(("uid", command.upper(), args))
        cmd = command.upper()
        if cmd == "SEARCH":
            return ("OK", [self._search(args)])
        if cmd == "FETCH":
            return self._fetch(int(args[0]))
        raise AssertionError(f"unexpected/unsupported uid command: {command!r}")

    def close(self):
        self.closed = True
        return ("OK", [b"CLOSE completed"])

    def logout(self):
        self.logged_out = True
        return ("BYE", [b"logout"])

    # -- test helper: simulate a new email arriving between polls ----------
    def deliver(self, uid: int, raw: bytes) -> None:
        self._messages[uid] = raw

    # -- mutating verbs MUST never be reached (read-only proof) ----------
    def store(self, *a, **k):
        raise AssertionError("STORE called — fetcher is supposed to be read-only!")

    def copy(self, *a, **k):
        raise AssertionError("COPY called — fetcher is supposed to be read-only!")

    def expunge(self, *a, **k):
        raise AssertionError("EXPUNGE called — fetcher is supposed to be read-only!")

    # -- helpers ----------------------------------------------------------
    def _search(self, args) -> bytes:
        uids = sorted(self._messages)
        lower = None
        for a in args:
            if isinstance(a, str) and a.upper().startswith("UID "):
                lower = int(a.split()[1].split(":")[0])
        if lower is not None:
            newer = [u for u in uids if u >= lower]
            # IMAP quirk: ``UID n:*`` is inclusive of the largest UID even when none
            # are >= n, so it returns the single highest existing UID, never empty.
            uids = newer or ([max(self._messages)] if self._messages else [])
        return " ".join(str(u) for u in uids).encode()

    def _fetch(self, uid: int):
        raw = self._messages.get(uid)
        if raw is None:
            return ("NO", [b""])
        # imaplib shape: a list whose first element is (header, body_bytes).
        return ("OK", [(f"{uid} (RFC822 {{{len(raw)}}})".encode(), raw), b")"])


def _factory_for(fake: FakeIMAP):
    return lambda host, port: fake


def _eml(name: str) -> bytes:
    return (_FIXTURES / name).read_bytes()


def _creds(folder: str = "INBOX", since_days: int = 30) -> InboxCreds:
    return InboxCreds(
        host="imap.test", port=993, user="me@gmail.com",
        password="app-pw", folder=folder, since_days=since_days,
    )


# --------------------------------------------------------------- creds_from_settings

def _with_inbox(settings: Settings, **kw) -> Settings:
    settings.inbox = InboxConfig(**kw)
    return settings


def test_creds_none_when_disabled(settings: Settings, monkeypatch):
    monkeypatch.setenv("AV3_IMAP_PASSWORD", "pw")
    _with_inbox(settings, enabled=False, user="me@gmail.com")
    assert creds_from_settings(settings) is None


def test_creds_none_when_no_user(settings: Settings, monkeypatch):
    monkeypatch.setenv("AV3_IMAP_PASSWORD", "pw")
    _with_inbox(settings, enabled=True, user=None)
    assert creds_from_settings(settings) is None


def test_creds_none_when_no_password(settings: Settings, monkeypatch):
    monkeypatch.delenv("AV3_IMAP_PASSWORD", raising=False)
    _with_inbox(settings, enabled=True, user="me@gmail.com")
    assert creds_from_settings(settings) is None


def test_creds_built_when_all_present(settings: Settings, monkeypatch):
    monkeypatch.setenv("AV3_IMAP_PASSWORD", "  app-pw  ")  # trimmed
    _with_inbox(settings, enabled=True, user="me@gmail.com", host="imap.gmail.com")
    creds = creds_from_settings(settings)
    assert creds is not None
    assert creds.user == "me@gmail.com"
    assert creds.password == "app-pw"
    assert creds.host == "imap.gmail.com"


# --------------------------------------------------------------- fetch + cursor

def test_cold_start_fetches_all_and_advances_cursor(conn):
    repo = InboxMessageRepo(conn)
    fake = FakeIMAP({3: b"a", 5: b"b", 7: b"c"})
    fetcher = ImapFetcher(_creds(), repo, imap_factory=_factory_for(fake))

    out = list(fetcher)

    assert [uid for uid, _ in out] == ["3", "5", "7"]
    assert [raw for _, raw in out] == [b"a", b"b", b"c"]
    assert repo.last_uid("INBOX") == 7          # cursor advanced to the max fetched
    assert fake.select_readonly is True          # read-only select
    assert fake.closed and fake.logged_out       # clean teardown


def test_incremental_fetch_only_returns_new(conn):
    repo = InboxMessageRepo(conn)
    repo.set_last_uid("INBOX", 5)
    fake = FakeIMAP({3: b"a", 5: b"b", 7: b"c", 9: b"d"})
    fetcher = ImapFetcher(_creds(), repo, imap_factory=_factory_for(fake))

    out = list(fetcher)

    assert [uid for uid, _ in out] == ["7", "9"]  # only > cursor
    assert repo.last_uid("INBOX") == 9


def test_no_new_mail_does_not_advance_cursor(conn):
    """The ``UID n:*`` quirk: a no-new-mail poll returns the highest existing UID,
    which the fetcher must filter out so the cursor never moves on empty."""
    repo = InboxMessageRepo(conn)
    repo.set_last_uid("INBOX", 9)
    fake = FakeIMAP({3: b"a", 5: b"b", 7: b"c", 9: b"d"})
    fetcher = ImapFetcher(_creds(), repo, imap_factory=_factory_for(fake))

    out = list(fetcher)

    assert out == []
    assert repo.last_uid("INBOX") == 9            # unchanged


def test_dry_run_fetches_without_advancing_cursor(conn):
    repo = InboxMessageRepo(conn)
    fake = FakeIMAP({3: b"a", 5: b"b"})
    fetcher = ImapFetcher(
        _creds(), repo, advance_cursor=False, imap_factory=_factory_for(fake),
    )

    out = list(fetcher)

    assert [uid for uid, _ in out] == ["3", "5"]
    assert repo.last_uid("INBOX") is None         # cursor untouched → repeatable


def test_reiterable_second_poll_sees_only_newer(conn):
    """Re-iteration is the scheduler's poll. First pass drains + advances the cursor;
    a second pass with no new mail yields nothing; a third sees only a newly-arrived UID."""
    repo = InboxMessageRepo(conn)
    box = {3: b"a", 5: b"b"}
    fake = FakeIMAP(box)
    fetcher = ImapFetcher(_creds(), repo, imap_factory=_factory_for(fake))

    first = [uid for uid, _ in fetcher]
    second = [uid for uid, _ in fetcher]
    fake.deliver(8, b"new")                        # a new email arrives between polls
    third = [uid for uid, _ in fetcher]

    assert first == ["3", "5"]
    assert second == []
    assert third == ["8"]
    assert repo.last_uid("INBOX") == 8


def test_max_messages_caps_one_poll(conn):
    repo = InboxMessageRepo(conn)
    fake = FakeIMAP({i: f"m{i}".encode() for i in range(1, 11)})
    fetcher = ImapFetcher(
        _creds(), repo, max_messages=4, imap_factory=_factory_for(fake),
    )

    out = list(fetcher)

    assert len(out) == 4
    assert [uid for uid, _ in out] == ["1", "2", "3", "4"]
    # Cursor advances to the last fetched, so the next poll continues from there.
    assert repo.last_uid("INBOX") == 4


# --------------------------------------------------------------- worker + fetcher e2e

def test_worker_records_outcome_through_imap_fetcher(conn):
    """The real :class:`InboxWorker` driven by the real :class:`ImapFetcher` (fake
    transport) records an outcome for an APPLIED job — the full Phase C path, offline."""
    import asyncio

    job = Job(
        source="greenhouse", source_job_id="12345",
        title="Senior Data Engineer", company="Acme",
        url="https://boards.greenhouse.io/acme/jobs/12345",
        state=JobState.APPLIED,
    )
    JobRepo(conn).add(job)

    fake = FakeIMAP({101: _eml("confirmation.eml")})
    fetcher = ImapFetcher(_creds(), InboxMessageRepo(conn), imap_factory=_factory_for(fake))
    worker = InboxWorker(settings=None, conn=conn, source=fetcher)  # settings unused here

    summary = asyncio.run(worker.run_once())

    assert summary.fetched == 1
    assert summary.outcomes_recorded == 1
    assert InboxMessageRepo(conn).last_uid("INBOX") == 101


# --------------------------------------------------------------- live (gated)

@pytest.mark.eval
def test_live_imap_fetch_is_read_only():
    """Live smoke against a real mailbox — gated on a Gmail app-password.

    Skips unless ``$AV3_IMAP_PASSWORD`` + ``$AV3_IMAP_USER`` are set, so CI / offline
    runs never touch the network. Proves a real read-only connect + a bounded fetch
    works end-to-end; it asserts nothing about message content (mailbox-dependent)."""
    user = os.environ.get("AV3_IMAP_USER", "").strip()
    pw = os.environ.get("AV3_IMAP_PASSWORD", "").strip()
    if not (user and pw):
        pytest.skip("set AV3_IMAP_USER + AV3_IMAP_PASSWORD to run the live IMAP smoke")

    from auto_applier.db import init_app_db

    # In-memory app db so the cursor repo (inbox_state) has its tables.
    conn = init_app_db(":memory:")
    try:
        creds = InboxCreds(
            host=os.environ.get("AV3_IMAP_HOST", "imap.gmail.com"),
            port=int(os.environ.get("AV3_IMAP_PORT", "993")),
            user=user, password=pw, folder="INBOX", since_days=3,
        )
        fetcher = ImapFetcher(creds, InboxMessageRepo(conn), advance_cursor=False)
        # Just drain it — a real read-only poll must not raise.
        got = list(fetcher)
        assert isinstance(got, list)
    finally:
        conn.close()
