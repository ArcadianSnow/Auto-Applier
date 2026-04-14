"""Learning goals tracker — skills the user is actively working on.

Lives in ``data/learning_goals.json``. Three states per skill:

- ``learning``: actively studying / practicing. Shown with a marker
  in gap reports so the user doesn't get re-nagged about it every
  time the refine chat runs.
- ``certified``: user has completed learning and confirmed competency.
  Candidate for the refine chat to propose adding as a confirmed
  skill on the resume.
- ``not_interested``: user dismissed this skill. Filtered out of
  gap reports and the refine chat.

No time-tracking, no due dates, no progress bars — deliberately
simple. The value is the filter, not a study schedule.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from auto_applier.config import DATA_DIR

logger = logging.getLogger(__name__)


VALID_STATES = {"learning", "certified", "not_interested"}

# Path to the JSON file — computed lazily so tests can monkeypatch
# DATA_DIR before first read.
def _goals_path() -> Path:
    return DATA_DIR / "learning_goals.json"


def _normalize(skill: str) -> str:
    return (skill or "").strip().lower()


def _load() -> dict[str, dict]:
    """Load the learning goals file. Returns empty dict if missing/corrupt.

    Shape on disk:
        {
          "python": {"state": "certified", "added_at": "2026-..."},
          "tableau": {"state": "learning", "added_at": "2026-..."},
          ...
        }
    """
    path = _goals_path()
    if not path.exists():
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("learning_goals.json is unreadable: %s", exc)
        return {}
    if not isinstance(raw, dict):
        return {}
    # Filter out entries with invalid state
    cleaned: dict[str, dict] = {}
    for skill, data in raw.items():
        if not isinstance(skill, str) or not isinstance(data, dict):
            continue
        state = data.get("state")
        if state not in VALID_STATES:
            continue
        cleaned[skill.lower()] = {
            "state": state,
            "added_at": data.get("added_at", ""),
        }
    return cleaned


def _save(goals: dict[str, dict]) -> None:
    path = _goals_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(goals, f, indent=2, sort_keys=True)


def set_state(skill: str, state: str) -> None:
    """Set the state of a skill in the learning tracker.

    Raises ValueError if state isn't one of VALID_STATES.
    Creates the entry if missing, updates otherwise.
    """
    if state not in VALID_STATES:
        raise ValueError(
            f"Invalid state '{state}'. Must be one of: {sorted(VALID_STATES)}"
        )
    key = _normalize(skill)
    if not key:
        raise ValueError("Skill name cannot be empty")

    goals = _load()
    goals[key] = {
        "state": state,
        "added_at": datetime.now(timezone.utc).isoformat(),
    }
    _save(goals)
    logger.info("Learning goal: '%s' -> %s", key, state)


def get_state(skill: str) -> str | None:
    """Return the state of a skill, or None if not tracked."""
    key = _normalize(skill)
    return _load().get(key, {}).get("state")


def list_goals(state: str | None = None) -> list[tuple[str, str]]:
    """Return all tracked goals, optionally filtered by state.

    Returns list of ``(skill, state)`` tuples sorted alphabetically by
    skill name. When ``state`` is None, returns all skills regardless
    of state.
    """
    goals = _load()
    result: list[tuple[str, str]] = []
    for skill, data in sorted(goals.items()):
        if state and data.get("state") != state:
            continue
        result.append((skill, data.get("state", "")))
    return result


def remove(skill: str) -> bool:
    """Remove a skill from tracking entirely. Returns True if removed."""
    key = _normalize(skill)
    goals = _load()
    if key not in goals:
        return False
    del goals[key]
    _save(goals)
    logger.info("Learning goal removed: '%s'", key)
    return True


def skills_by_state() -> dict[str, set[str]]:
    """Group skills by their state. Convenient for gap filtering.

    Returns dict like::

        {
          "learning":        {"tableau", "kubernetes"},
          "certified":       {"python"},
          "not_interested":  {"rust"},
        }
    """
    goals = _load()
    out: dict[str, set[str]] = {
        "learning": set(),
        "certified": set(),
        "not_interested": set(),
    }
    for skill, data in goals.items():
        state = data.get("state")
        if state in out:
            out[state].add(skill)
    return out
