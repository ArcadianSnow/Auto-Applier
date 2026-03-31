"""Resume evolution engine -- tracks skill gaps and triggers updates.

The evolution engine monitors ``skill_gaps.csv`` for recurring gaps
(fields/skills that jobs ask for but the resume lacks).  When a skill
crosses the trigger threshold it surfaces an :class:`EvolutionTrigger`
so the user can confirm or dismiss the skill.  Confirmed skills get
added to the resume profile and included in future LLM prompts.
"""

import json
import logging
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from auto_applier.config import DATA_DIR, DEFAULT_EVOLUTION_TRIGGER_THRESHOLD
from auto_applier.storage.models import SkillGap
from auto_applier.storage.repository import load_all

logger = logging.getLogger(__name__)


@dataclass
class EvolutionTrigger:
    """A skill that has appeared enough times to warrant user confirmation."""

    skill_name: str
    times_seen: int
    category: str
    sample_job_titles: list = field(default_factory=list)
    resume_label: str = ""


class EvolutionEngine:
    """Monitors skill gaps and triggers resume evolution prompts.

    The engine reads ``skill_gaps.csv``, counts how often each gap
    appears, and surfaces triggers for any skill that crosses the
    configurable threshold.  Skills that have already been prompted
    (accepted or dismissed) are tracked in ``data/prompted_skills.json``
    so users are not asked again.

    Usage::

        engine = EvolutionEngine(trigger_threshold=3)
        triggers = engine.check_triggers()
        for t in triggers:
            if user_confirms(t):
                engine.mark_prompted(t.skill_name)
    """

    def __init__(
        self, trigger_threshold: int = DEFAULT_EVOLUTION_TRIGGER_THRESHOLD
    ) -> None:
        self.trigger_threshold = trigger_threshold
        self._prompted_file = DATA_DIR / "prompted_skills.json"

    # ------------------------------------------------------------------
    # Prompted-skills persistence
    # ------------------------------------------------------------------

    def _load_prompted(self) -> set[str]:
        """Load skills that have already been prompted."""
        if self._prompted_file.exists():
            try:
                with open(self._prompted_file, "r", encoding="utf-8") as f:
                    return set(json.load(f))
            except (json.JSONDecodeError, TypeError):
                logger.warning(
                    "Corrupt prompted_skills.json, starting fresh"
                )
                return set()
        return set()

    def _save_prompted(self, prompted: set[str]) -> None:
        """Persist the set of already-prompted skill names."""
        with open(self._prompted_file, "w", encoding="utf-8") as f:
            json.dump(sorted(prompted), f, indent=2)

    def mark_prompted(self, skill_name: str) -> None:
        """Mark a skill as already prompted so we don't ask again."""
        prompted = self._load_prompted()
        prompted.add(skill_name.lower())
        self._save_prompted(prompted)
        logger.info("Marked '%s' as prompted", skill_name)

    # ------------------------------------------------------------------
    # Trigger detection
    # ------------------------------------------------------------------

    def check_triggers(self) -> list[EvolutionTrigger]:
        """Check for skills that have crossed the trigger threshold.

        Returns triggers for skills that:

        1. Appear >= ``trigger_threshold`` times in ``skill_gaps.csv``
        2. Have NOT already been prompted (accepted or dismissed)

        Results are sorted by frequency (most common first).
        """
        gaps = load_all(SkillGap)
        prompted = self._load_prompted()

        # Count gaps by normalized field_label
        label_counter: Counter = Counter()
        label_category: dict[str, str] = {}
        label_resume: dict[str, str] = {}
        label_jobs: dict[str, list[str]] = {}

        for gap in gaps:
            key = gap.field_label.lower().strip()
            if key in prompted:
                continue
            label_counter[key] += 1
            label_category[key] = gap.category
            label_resume[key] = gap.resume_label
            if key not in label_jobs:
                label_jobs[key] = []
            if len(label_jobs[key]) < 3:
                label_jobs[key].append(gap.job_id)

        triggers: list[EvolutionTrigger] = []
        for skill, count in label_counter.most_common():
            if count >= self.trigger_threshold:
                triggers.append(
                    EvolutionTrigger(
                        skill_name=skill,
                        times_seen=count,
                        category=label_category.get(skill, "other"),
                        sample_job_titles=label_jobs.get(skill, []),
                        resume_label=label_resume.get(skill, ""),
                    )
                )

        logger.info(
            "Found %d evolution triggers (threshold=%d)",
            len(triggers),
            self.trigger_threshold,
        )
        return triggers

    # ------------------------------------------------------------------
    # Gap summary
    # ------------------------------------------------------------------

    def get_gap_summary(self) -> list[tuple[str, int, str]]:
        """Get all gaps sorted by frequency.

        Returns a list of ``(label, count, category)`` tuples, sorted
        from most frequent to least.  This includes already-prompted
        skills (for reporting purposes).
        """
        gaps = load_all(SkillGap)
        counter: Counter = Counter()
        categories: dict[str, str] = {}
        for gap in gaps:
            key = gap.field_label.lower().strip()
            counter[key] += 1
            categories[key] = gap.category

        return [
            (label, count, categories.get(label, "other"))
            for label, count in counter.most_common()
        ]
