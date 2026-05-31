"""Diagnostics tarball builder for ``av3 export-diagnostics`` (spec §9, Phase 5 3/M).

The support surface: "send me a tarball" instead of "send me your logs." Bundles
the local triage state — settings (secrets stripped), doctor results, recent error
events, per-stage stats, mirror-queue status, and a manifest — into a single
``diagnostics-<ts>.tar.gz`` under the data dir.

## The PII decision (resolved 3/M)

The local ``events.db`` carries FULL fidelity: un-scrubbed ``error_msg`` (may embed a
path with the username, an email, a phone) and full ``context_json`` (which for an
inferred-answer row includes the *answer value* — exactly what §9 says must never leave
the machine). Two modes:

* **Default = SCRUBBED.** Error rows route through :func:`scrub_error_event` and
  inferred-answer rows through :func:`scrub_inferred_answer_event` — the same category
  scrubbers the opt-in mirror uses — so the tarball is safe to email even to someone
  outside the small group. EEO inferred-answer rows drop entirely (§8d). The raw
  ``events.db`` file is **not** included.
* **``--raw`` = FULL.** Adds a verbatim copy of ``events.db`` and un-scrubbed error
  rows. For the in-group debug case where the recipient is the owner and the extra
  fidelity is worth it. The CLI warns loudly that this bundle is PII-bearing.

Settings are ALWAYS secret-stripped (``llm.gemini_api_key``, ``telemetry.handle``) in
both modes — a key or a raw handle in a support bundle is never acceptable.
"""

from __future__ import annotations

import io
import json
import sqlite3
import tarfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from auto_applier.config import Settings
from auto_applier.telemetry.mirror import MirrorPolicy
from auto_applier.telemetry.scrub import scrub_error_event, scrub_inferred_answer_event

__all__ = ["DiagnosticsResult", "build_diagnostics", "collect_diagnostics"]


# Settings keys stripped from the dump in BOTH modes. Dotted path into the
# model_dump() dict. A secret or a raw handle never belongs in a support bundle.
_SECRET_PATHS = (
    ("llm", "gemini_api_key"),
    ("telemetry", "handle"),
)


@dataclass(frozen=True)
class DiagnosticsResult:
    """Outcome of a diagnostics export."""

    path: Path
    raw: bool
    error_rows: int
    bytes_written: int


def _strip_secrets(dump: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of a settings dump with secret leaves removed."""
    for *parents, leaf in _SECRET_PATHS:
        node = dump
        for p in parents:
            node = node.get(p) if isinstance(node, dict) else None
            if node is None:
                break
        if isinstance(node, dict):
            node.pop(leaf, None)
    return dump


def _settings_dump(settings: Settings) -> dict[str, Any]:
    """``settings`` as a JSON-safe, secret-stripped dict.

    ``mode="json"`` so ``Path`` fields serialize to strings rather than blowing up
    ``json.dumps``. We deep-copy implicitly via model_dump (fresh dict) so stripping
    secrets can't mutate the live settings object.
    """
    return _strip_secrets(settings.model_dump(mode="json"))


def _doctor_text() -> str:
    """Render ``run_doctor()`` as plain text. Imported lazily so the diagnostics
    module doesn't pull the doctor's httpx import at module load (and so a doctor
    refactor can't break a telemetry import)."""
    from auto_applier.doctor import run_doctor

    lines = []
    for r in run_doctor():
        lines.append(f"[{r.status.value:4}] {r.name}: {r.detail}")
        if r.fix and r.status.value != "PASS":
            lines.append(f"       fix -> {r.fix}")
    return "\n".join(lines) + "\n"


def _error_rows(conn: sqlite3.Connection, *, limit: int, raw: bool) -> list[dict[str, Any]]:
    """Recent error rows from events.db. Scrubbed (default) or full (``raw``).

    Scrubbed mode routes each row through :func:`scrub_error_event` — same category
    scrubber the opt-in mirror uses — so the JSON in the bundle carries no PII.
    """
    rows = conn.execute(
        """SELECT ts, stage, platform, job_id, error_type, error_msg, context_json
           FROM events WHERE status = 'error' ORDER BY id DESC LIMIT ?""",
        (int(limit),),
    ).fetchall()
    out: list[dict[str, Any]] = []
    for r in rows:
        d = {k: r[k] for k in r.keys()}
        if raw:
            out.append(d)
        else:
            out.append(
                scrub_error_event(
                    {
                        "stage": d.get("stage"),
                        "platform": d.get("platform"),
                        "error_type": d.get("error_type"),
                        "error_msg": d.get("error_msg"),
                        "ts": d.get("ts"),
                    }
                )
            )
    return out


def _inferred_rows(conn: sqlite3.Connection, *, limit: int, raw: bool) -> list[dict[str, Any]]:
    """Recent inferred-answer rows (the §8b iteration signal). ALWAYS scrubbed
    through :func:`scrub_inferred_answer_event` even in ``--raw`` mode — the answer
    value living in ``context_json`` is the one thing §9 says never leaves the
    machine, so the bundle never carries it regardless of mode. EEO rows drop."""
    rows = conn.execute(
        """SELECT ts, context_json FROM events
           WHERE stage = 'resolver_inferred' AND status = 'ok'
           ORDER BY id DESC LIMIT ?""",
        (int(limit),),
    ).fetchall()
    out: list[dict[str, Any]] = []
    for r in rows:
        ctx = {}
        if r["context_json"]:
            try:
                ctx = json.loads(r["context_json"])
            except (json.JSONDecodeError, TypeError):
                ctx = {}
        scrubbed = scrub_inferred_answer_event(
            {
                "question_text": ctx.get("question"),
                "category": ctx.get("category"),
                "confidence": ctx.get("confidence"),
                "outcome": ctx.get("outcome"),
                "ts": r["ts"],
            }
        )
        if scrubbed:  # EEO -> {} -> dropped
            out.append(scrubbed)
    return out


def _stage_stats(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        """SELECT stage,
                  SUM(status='ok')    AS ok,
                  SUM(status='error') AS error,
                  SUM(status='skip')  AS skip,
                  AVG(duration_ms)    AS avg_ms
           FROM events GROUP BY stage ORDER BY stage"""
    ).fetchall()
    out = []
    for r in rows:
        d = {k: r[k] for k in r.keys()}
        if d.get("avg_ms") is not None:
            d["avg_ms"] = round(d["avg_ms"], 1)
        out.append(d)
    return out


def collect_diagnostics(
    settings: Settings, *, raw: bool, error_limit: int, now_iso: str | None = None
) -> dict[str, Any]:
    """Gather every diagnostic artifact into an in-memory dict, keyed by the
    filename it will get in the tarball. Pure read; opens its own short-lived
    connections. Split out from :func:`build_diagnostics` so tests can assert on
    the contents without untarring."""
    from auto_applier import __version__

    ts = now_iso or datetime.now(timezone.utc).isoformat(timespec="seconds")

    # Mirror identity + queue status (events.db owns the mirror_queue table).
    from auto_applier.telemetry.sink import EventSink

    sink = EventSink(settings.events_db_path)
    try:
        error_rows = _error_rows(sink.conn, limit=error_limit, raw=raw)
        inferred_rows = _inferred_rows(sink.conn, limit=error_limit, raw=raw)
        stats = _stage_stats(sink.conn)
        queue = sink.mirror_queue.summary()
    finally:
        sink.close()

    policy = MirrorPolicy.from_settings(settings.telemetry, __version__)

    manifest = {
        "app_version": __version__,
        "generated_at": ts,
        "scrub_mode": "raw" if raw else "scrubbed",
        "error_rows_included": len(error_rows),
        "telemetry_enabled": bool(settings.telemetry.enabled),
        # user_id is the sha256[:10] pseudonym — safe to include, it's what would
        # be mirrored anyway. The raw handle is NEVER here (stripped from settings).
        "user_id": policy.user_id,
    }

    return {
        "manifest.json": manifest,
        "settings.json": _settings_dump(settings),
        "doctor.txt": _doctor_text(),
        "events_errors.json": error_rows,
        "events_inferred.json": inferred_rows,
        "events_stats.json": stats,
        "mirror_status.json": queue,
    }


def build_diagnostics(
    settings: Settings, *, raw: bool = False, error_limit: int = 200,
    now_iso: str | None = None,
) -> DiagnosticsResult:
    """Write the diagnostics tarball to ``<data_dir>/diagnostics-<ts>.tar.gz``.

    Returns a :class:`DiagnosticsResult` with the path and a few counters for the
    CLI's summary line. The data dir is created if missing so this works on a
    fresh install where only events.db exists.
    """
    artifacts = collect_diagnostics(
        settings, raw=raw, error_limit=error_limit, now_iso=now_iso
    )

    settings.data_dir.mkdir(parents=True, exist_ok=True)
    # Filename-safe timestamp (no colons — Windows rejects them in paths).
    stamp = (now_iso or datetime.now(timezone.utc).isoformat(timespec="seconds"))
    stamp = stamp.replace(":", "").replace("+00:00", "Z")
    out_path = settings.data_dir / f"diagnostics-{stamp}.tar.gz"

    with tarfile.open(out_path, "w:gz") as tar:
        for name, content in artifacts.items():
            if name.endswith(".json"):
                data = json.dumps(content, indent=2, default=str).encode("utf-8")
            else:
                data = str(content).encode("utf-8")
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))

        # --raw: include a verbatim events.db copy (PII-bearing). The local DB is
        # full fidelity; this is the in-group deep-debug escape hatch.
        if raw and settings.events_db_path.exists():
            tar.add(settings.events_db_path, arcname="events.db")

    return DiagnosticsResult(
        path=out_path,
        raw=raw,
        error_rows=len(artifacts["events_errors.json"]),
        bytes_written=out_path.stat().st_size,
    )
