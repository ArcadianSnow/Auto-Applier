"""Preflight checks (spec §3, §10).

Each check is a small function returning a :class:`CheckResult`. Runs read-only, fast,
fails closed; every FAIL/WARN carries a ``fix`` hint. ``run_doctor`` returns a non-zero
count so the CLI / CI can gate on it.

Phase 0 scope: config valid, data dir + both DBs writable, schema current, LLM backend
reachable (WARN — Gemini/rule fallback exists). Login/browser/relay checks arrive with
their subsystems (Phases 1–5).
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from enum import Enum

import httpx

from auto_applier.config import Settings, load_settings
from auto_applier.db import init_app_db
from auto_applier.telemetry import EventSink


class Status(str, Enum):
    PASS = "PASS"
    WARN = "WARN"
    FAIL = "FAIL"


@dataclass
class CheckResult:
    name: str
    status: Status
    detail: str = ""
    fix: str = ""


def check_config() -> tuple[CheckResult, Settings | None]:
    try:
        settings = load_settings()
    except Exception as exc:
        return (
            CheckResult(
                "config", Status.FAIL, f"invalid config: {exc}",
                fix="edit data/v3/user_config.json - see av3/config/settings.py for the schema",
            ),
            None,
        )
    return CheckResult("config", Status.PASS, f"data_dir={settings.data_dir}"), settings


def check_app_db(settings: Settings) -> CheckResult:
    try:
        conn = init_app_db(settings.app_db_path)
        try:
            conn.execute("CREATE TABLE IF NOT EXISTS _doctor_probe (x INTEGER)")
            conn.execute("DROP TABLE _doctor_probe")
            tables = {
                r["name"]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
        finally:
            conn.close()
    except (sqlite3.Error, OSError) as exc:
        return CheckResult(
            "app_db", Status.FAIL, f"cannot open/write {settings.app_db_path}: {exc}",
            fix="check disk space and that the data dir is writable (not read-only / locked)",
        )
    expected = {"jobs", "job_scores", "applications", "skill_gaps", "answers"}
    missing = expected - tables
    if missing:
        return CheckResult(
            "app_db", Status.FAIL, f"missing tables: {sorted(missing)}",
            fix="run `av3 init-db` to (re)apply the schema",
        )
    return CheckResult("app_db", Status.PASS, f"schema current at {settings.app_db_path}")


def check_events_db(settings: Settings) -> CheckResult:
    try:
        sink = EventSink(settings.events_db_path)
        sink.close()
    except (sqlite3.Error, OSError) as exc:
        return CheckResult(
            "events_db", Status.FAIL, f"cannot open/write {settings.events_db_path}: {exc}",
            fix="check disk space and that the data dir is writable",
        )
    return CheckResult("events_db", Status.PASS, f"writable at {settings.events_db_path}")


def check_llm(settings: Settings) -> CheckResult:
    """Ollama reachability. WARN (not FAIL) — Gemini/rule fallback exists (spec §6)."""
    url = settings.llm.ollama_host.rstrip("/") + "/api/tags"
    try:
        resp = httpx.get(url, timeout=2.0)
        resp.raise_for_status()
        models = [m.get("name", "") for m in resp.json().get("models", [])]
    except Exception as exc:
        has_gemini = bool(settings.llm.gemini_api_key)
        return CheckResult(
            "llm", Status.WARN,
            f"Ollama unreachable at {settings.llm.ollama_host} ({type(exc).__name__})"
            + (" - Gemini key present, will fall back" if has_gemini else ""),
            fix="start Ollama (`ollama serve`) or set GEMINI_API_KEY in .env",
        )
    want = settings.llm.ollama_model
    if want not in models and want.split(":")[0] not in {m.split(":")[0] for m in models}:
        return CheckResult(
            "llm", Status.WARN,
            f"Ollama up but model '{want}' not pulled (have: {models or 'none'})",
            fix=f"run `ollama pull {want}`",
        )
    return CheckResult("llm", Status.PASS, f"Ollama up, model '{want}' available")


def check_backups(settings: Settings) -> CheckResult:
    """Last backup recency check (spec §4 backups).

    PASS when the newest ``app.db.*`` snapshot is younger than 2 *
    ``retention.maintenance_interval_s`` (so a one-cycle blip doesn't trip
    monitoring). WARN when older or missing - the always-on operating model
    assumes the scheduler's maintenance hook is firing.

    WARN (not FAIL): backups are recoverable from the live DB until something
    catastrophic happens, and a fresh install legitimately has no backup yet.
    """
    from datetime import datetime, timezone

    backups_dir = settings.backups_dir
    if not backups_dir.exists():
        return CheckResult(
            "backups", Status.WARN,
            f"backups dir missing: {backups_dir}",
            fix="run `av3 backup` once to seed it (auto-created by the scheduler too)",
        )

    snaps = list(backups_dir.glob(f"{settings.app_db_path.stem}.*"))
    if not snaps:
        return CheckResult(
            "backups", Status.WARN,
            f"no app.db snapshots in {backups_dir}",
            fix="run `av3 backup` once, or start `av3 run` (the scheduler backs up automatically)",
        )

    newest = max(snaps, key=lambda p: p.stat().st_mtime)
    age_s = datetime.now(timezone.utc).timestamp() - newest.stat().st_mtime
    threshold_s = 2 * settings.retention.maintenance_interval_s
    if age_s > threshold_s:
        return CheckResult(
            "backups", Status.WARN,
            f"newest app.db snapshot is {int(age_s // 3600)}h old "
            f"(threshold {int(threshold_s // 3600)}h)",
            fix="run `av3 backup` or check that `av3 run`'s maintenance hook is firing",
        )
    return CheckResult(
        "backups", Status.PASS,
        f"newest snapshot {newest.name} ({int(age_s // 60)}m ago)",
    )


def check_relay_reachable(settings: Settings) -> CheckResult:
    """Telemetry relay reachability (spec §9, §3 doctor scope; Phase 5 4/M).

    Only meaningful when telemetry is opted in. Three cases:
      * telemetry OFF        → PASS (nothing to reach; the default, not an error).
      * ON but no relay_url  → WARN (rows queue locally but can never drain).
      * ON with relay_url    → GET ``{relay}/health``; PASS on 2xx, else WARN.

    WARN, never FAIL: telemetry is additive and opt-in (spec §9). A down relay
    must not fail ``doctor`` / break CI for users who don't even use telemetry,
    and even for opted-in users the local pipeline is unaffected — the queue just
    backs up and drains later.
    """
    tele = settings.telemetry
    if not tele.enabled:
        return CheckResult("relay", Status.PASS, "telemetry off (no relay needed)")
    if not tele.relay_url:
        return CheckResult(
            "relay", Status.WARN,
            "telemetry on but no relay_url — scrubbed rows queue locally, never sent",
            fix="set one: `av3 telemetry on --relay-url https://<your-relay>`",
        )
    health = tele.relay_url.rstrip("/") + "/health"
    try:
        resp = httpx.get(health, timeout=3.0)
        resp.raise_for_status()
    except Exception as exc:
        return CheckResult(
            "relay", Status.WARN,
            f"relay unreachable at {health} ({type(exc).__name__}) — rows will queue",
            fix="check the relay is deployed and the URL is correct; rows retry automatically",
        )
    return CheckResult("relay", Status.PASS, f"relay healthy at {health}")


def run_doctor() -> list[CheckResult]:
    """Run all checks; return results in display order."""
    results: list[CheckResult] = []
    cfg_result, settings = check_config()
    results.append(cfg_result)
    if settings is None:
        return results  # nothing else can run without valid config
    results.append(check_app_db(settings))
    results.append(check_events_db(settings))
    results.append(check_llm(settings))
    results.append(check_backups(settings))
    results.append(check_relay_reachable(settings))
    return results


def fail_count(results: list[CheckResult]) -> int:
    return sum(1 for r in results if r.status is Status.FAIL)
