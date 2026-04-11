"""Job archetype classification for scoring-time resume routing.

With multiple resumes loaded, the naive scoring flow evaluates every
resume against every job description. For N resumes × M jobs that's
O(N·M) LLM calls. Archetype routing cuts this to O(M + hits):

1. Classify the job description into one of a small set of user-defined
   archetypes (e.g. "data_analyst", "ml_engineer", "backend").
2. Score only the resumes tagged to that archetype.
3. Fall back to scoring all resumes if:
   - the classifier confidence is below ``CONFIDENCE_THRESHOLD``, or
   - no resume is tagged to the detected archetype, or
   - the archetype feature is unused (``data/archetypes.json`` missing
     or empty).

The feature is opt-in: users define their archetypes in
``data/archetypes.json`` and tag each resume's profile with one or
more archetype names. If they don't, scoring behaves exactly as it
did before this module existed.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

from auto_applier.config import DATA_DIR

logger = logging.getLogger(__name__)

ARCHETYPES_FILE = DATA_DIR / "archetypes.json"
CONFIDENCE_THRESHOLD = 0.6


@dataclass
class Archetype:
    name: str
    description: str
    keywords: list[str] = None  # type: ignore[assignment]

    def __post_init__(self):
        if self.keywords is None:
            self.keywords = []


@dataclass
class ClassificationResult:
    archetype: str  # name, or "" if classifier couldn't decide
    confidence: float  # 0.0 - 1.0
    reason: str = ""


def load_archetypes() -> list[Archetype]:
    """Load user-defined archetypes from ``data/archetypes.json``.

    Returns an empty list if the file is missing or malformed —
    callers treat that as "feature disabled, score all resumes".
    """
    if not ARCHETYPES_FILE.exists():
        return []
    try:
        raw = json.loads(ARCHETYPES_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("Failed to load archetypes.json: %s", e)
        return []

    items = raw.get("archetypes", []) if isinstance(raw, dict) else raw
    out: list[Archetype] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        name = str(it.get("name", "")).strip()
        if not name:
            continue
        out.append(Archetype(
            name=name,
            description=str(it.get("description", "")),
            keywords=list(it.get("keywords", []) or []),
        ))
    return out


def save_archetypes(archetypes: list[Archetype]) -> None:
    """Persist archetype definitions to ``data/archetypes.json``."""
    payload = {
        "archetypes": [
            {
                "name": a.name,
                "description": a.description,
                "keywords": a.keywords,
            }
            for a in archetypes
        ]
    }
    ARCHETYPES_FILE.write_text(
        json.dumps(payload, indent=2), encoding="utf-8",
    )


def resume_archetypes(profile: dict) -> list[str]:
    """Return the archetype tags from a resume profile dict.

    Profiles that predate the feature or don't have any tags return an
    empty list, which the router treats as "wildcard — scoreable for
    any archetype".
    """
    tags = profile.get("archetypes", [])
    if not isinstance(tags, list):
        return []
    return [str(t).strip() for t in tags if str(t).strip()]


def filter_resumes_by_archetype(
    resumes,  # list[tuple[label, profile_dict]]
    target: str,
) -> list:
    """Return only resumes tagged to ``target`` archetype.

    Resumes with no tags act as wildcards and are always included —
    users shouldn't be punished for half-migrating to the feature.

    An empty ``target`` returns all resumes (no filter).
    """
    if not target:
        return list(resumes)
    kept = []
    for item in resumes:
        _, profile = item
        tags = resume_archetypes(profile)
        if not tags:
            # Untagged resumes are wildcards — always scorable
            kept.append(item)
            continue
        if target in tags:
            kept.append(item)
    return kept


class ArchetypeClassifier:
    """Classifies a job description into one of the defined archetypes."""

    def __init__(self, router) -> None:
        self.router = router

    async def classify(
        self, job_description: str, archetypes: list[Archetype] | None = None,
    ) -> ClassificationResult:
        """Return the best-matching archetype and its confidence.

        Empty archetype list → empty result (feature disabled).
        Malformed LLM response → empty result (fail closed to
        full-scoring fallback).
        """
        archs = archetypes if archetypes is not None else load_archetypes()
        if not archs:
            return ClassificationResult(archetype="", confidence=0.0)

        from auto_applier.llm.prompts import CLASSIFY_JOB_ARCHETYPE
        menu = "\n".join(
            f"- {a.name}: {a.description}" for a in archs
        )
        valid_names = {a.name for a in archs}

        try:
            result = await self.router.complete_json(
                prompt=CLASSIFY_JOB_ARCHETYPE.format(
                    archetype_menu=menu,
                    job_description=job_description[:2500],
                ),
                system_prompt=CLASSIFY_JOB_ARCHETYPE.system,
            )
        except Exception as e:
            logger.debug("Archetype classification raised: %s", e)
            return ClassificationResult(archetype="", confidence=0.0)

        name = str(result.get("archetype", "")).strip()
        try:
            confidence = float(result.get("confidence", 0.0))
        except (TypeError, ValueError):
            confidence = 0.0
        confidence = max(0.0, min(1.0, confidence))

        # Guard against the LLM inventing an archetype name
        if name and name not in valid_names:
            logger.debug("Classifier returned unknown archetype '%s'", name)
            return ClassificationResult(archetype="", confidence=confidence)

        return ClassificationResult(
            archetype=name,
            confidence=confidence,
            reason=str(result.get("reason", "")),
        )
