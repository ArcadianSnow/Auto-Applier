"""Per-platform cooldown registry.

When a platform trips a CAPTCHA / login wall, retrying immediately on
the next continuous-run cycle just trips the same detection again and
digs the fingerprint hole deeper. The state below records "platform X
is in cooldown until ISO timestamp Y" and the engine consults it
before scheduling work for that platform.

Persisted to ``data/.platform_pauses.json`` so a process restart
respects pauses set in the previous run. Cleared automatically once
the cooldown elapses; can be cleared manually via ``cli unpause``.
"""
from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from auto_applier.config import DATA_DIR

logger = logging.getLogger(__name__)

PAUSES_FILE = DATA_DIR / ".platform_pauses.json"
DEFAULT_COOLDOWN_HOURS = 4

_LOCK = threading.Lock()


@dataclass(frozen=True)
class PauseRecord:
    platform: str
    paused_until: datetime  # UTC
    reason: str

    def is_active(self, now: Optional[datetime] = None) -> bool:
        return (now or datetime.now(timezone.utc)) < self.paused_until

    def remaining(self, now: Optional[datetime] = None) -> timedelta:
        return max(
            timedelta(0),
            self.paused_until - (now or datetime.now(timezone.utc)),
        )


def _load_raw() -> dict:
    if not PAUSES_FILE.exists():
        return {}
    try:
        return json.loads(PAUSES_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("platform_pauses: unreadable %s: %s", PAUSES_FILE, exc)
        return {}


def _save_raw(data: dict) -> None:
    """Atomic-write the pauses file so a crash mid-write can't corrupt it."""
    PAUSES_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = PAUSES_FILE.with_suffix(PAUSES_FILE.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp, PAUSES_FILE)


def _record_from_dict(platform: str, raw: dict) -> Optional[PauseRecord]:
    try:
        ts = raw["paused_until"]
        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return PauseRecord(
            platform=platform,
            paused_until=dt,
            reason=raw.get("reason", ""),
        )
    except (KeyError, ValueError, TypeError):
        return None


def list_active(now: Optional[datetime] = None) -> list[PauseRecord]:
    """Return every still-active pause, dropping expired entries from disk."""
    now = now or datetime.now(timezone.utc)
    with _LOCK:
        data = _load_raw()
        active: list[PauseRecord] = []
        changed = False
        keep: dict = {}
        for platform, raw in (data or {}).items():
            rec = _record_from_dict(platform, raw)
            if rec is None:
                changed = True
                continue
            if rec.is_active(now):
                active.append(rec)
                keep[platform] = raw
            else:
                # Expired — drop from disk so the file doesn't grow forever.
                changed = True
        if changed:
            _save_raw(keep)
        return active


def is_paused(platform: str, now: Optional[datetime] = None) -> Optional[PauseRecord]:
    """Return the active PauseRecord for ``platform``, or None."""
    for rec in list_active(now):
        if rec.platform == platform:
            return rec
    return None


def pause(
    platform: str,
    reason: str,
    cooldown_hours: float = DEFAULT_COOLDOWN_HOURS,
) -> PauseRecord:
    """Mark ``platform`` paused for ``cooldown_hours`` from now."""
    until = datetime.now(timezone.utc) + timedelta(hours=cooldown_hours)
    rec = PauseRecord(platform=platform, paused_until=until, reason=reason)
    with _LOCK:
        data = _load_raw()
        data[platform] = {
            "paused_until": until.isoformat(),
            "reason": reason,
        }
        _save_raw(data)
    logger.warning(
        "platform_pauses: %s paused for %.1fh (reason: %s)",
        platform, cooldown_hours, reason,
    )
    return rec


def unpause(platform: str) -> bool:
    """Manually clear a pause. Returns True if a record was removed."""
    with _LOCK:
        data = _load_raw()
        if platform not in data:
            return False
        del data[platform]
        _save_raw(data)
    logger.info("platform_pauses: %s manually unpaused", platform)
    return True


def unpause_all() -> int:
    """Clear every active pause. Returns count removed."""
    with _LOCK:
        data = _load_raw()
        count = len(data)
        if count:
            _save_raw({})
    return count


def format_remaining(rec: PauseRecord) -> str:
    """Human-readable 'Xh Ym' string for the remaining cooldown."""
    remaining = rec.remaining()
    total_minutes = int(remaining.total_seconds() // 60)
    hours, minutes = divmod(total_minutes, 60)
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"
