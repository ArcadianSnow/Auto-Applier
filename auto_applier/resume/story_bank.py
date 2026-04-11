"""STAR+Reflection interview story bank.

After every successful application the LLM produces 3 short stories
tailored to that specific job. Each story is a STAR+R record —
Situation, Task, Action, Result, Reflection — drawn from the actual
resume text so the candidate recognizes the material when they read
it back during interview prep.

Stories accumulate in ``data/story_bank.json`` with full provenance:
which job_id, which company, which resume was used, and a timestamp.
Over time the bank becomes a reusable library of 10-20 master
stories that can answer any behavioral question.

The bank is write-append-only for new stories. Users can dedupe or
prune through the CLI (``story prune``) — the module never discards
entries automatically.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from auto_applier.config import DATA_DIR
from auto_applier.llm.prompts import STAR_STORIES
from auto_applier.llm.router import LLMRouter

logger = logging.getLogger(__name__)

STORY_BANK_FILE = DATA_DIR / "story_bank.json"


@dataclass
class Story:
    title: str
    question_prompt: str
    situation: str
    task: str
    action: str
    result: str
    reflection: str
    # Provenance
    job_id: str = ""
    company: str = ""
    job_title: str = ""
    resume_label: str = ""
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


def load_bank() -> list[Story]:
    """Load the current story bank. Returns empty list if missing or invalid."""
    if not STORY_BANK_FILE.exists():
        return []
    try:
        raw = json.loads(STORY_BANK_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("Failed to load story bank: %s", e)
        return []
    items = raw if isinstance(raw, list) else raw.get("stories", [])
    out: list[Story] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        try:
            # Only keep keys that are valid Story fields
            filtered = {
                k: v for k, v in it.items()
                if k in {f.name for f in Story.__dataclass_fields__.values()}
            }
            out.append(Story(**filtered))
        except TypeError:
            continue
    return out


def save_bank(stories: list[Story]) -> None:
    """Persist the bank as a JSON list."""
    payload = [asdict(s) for s in stories]
    STORY_BANK_FILE.write_text(
        json.dumps(payload, indent=2), encoding="utf-8",
    )


def append_stories(new_stories: list[Story]) -> None:
    """Append stories to the bank, persisting immediately."""
    if not new_stories:
        return
    bank = load_bank()
    bank.extend(new_stories)
    save_bank(bank)


class StoryGenerator:
    """Generates tailored STAR+Reflection stories via the LLM router."""

    def __init__(self, router: LLMRouter) -> None:
        self.router = router

    async def generate(
        self,
        resume_text: str,
        job_description: str,
        company_name: str,
        job_title: str,
        job_id: str = "",
        resume_label: str = "",
    ) -> list[Story]:
        """Generate 3 STAR+R stories tailored to a specific job.

        Returns an empty list on LLM failure or malformed response.
        Never raises — callers hook this into APPLICATION_COMPLETE
        and failure here should not affect the application pipeline.
        """
        try:
            result = await self.router.complete_json(
                prompt=STAR_STORIES.format(
                    resume_text=resume_text[:3500],
                    job_description=job_description[:2000],
                    company_name=company_name,
                    job_title=job_title,
                ),
                system_prompt=STAR_STORIES.system,
            )
        except Exception as e:
            logger.debug("Story generation raised: %s", e)
            return []

        items = result.get("stories", [])
        if not isinstance(items, list):
            return []

        stories: list[Story] = []
        required_fields = ["situation", "task", "action", "result", "reflection"]
        for it in items:
            if not isinstance(it, dict):
                continue
            # Require all STAR+R segments to be non-empty
            if not all(str(it.get(f, "")).strip() for f in required_fields):
                continue
            stories.append(Story(
                title=str(it.get("title", "Untitled story")),
                question_prompt=str(it.get("question_prompt", "")),
                situation=str(it.get("situation", "")),
                task=str(it.get("task", "")),
                action=str(it.get("action", "")),
                result=str(it.get("result", "")),
                reflection=str(it.get("reflection", "")),
                job_id=job_id,
                company=company_name,
                job_title=job_title,
                resume_label=resume_label,
            ))
        return stories


def export_bank_markdown() -> str:
    """Render the entire bank as a human-readable markdown document."""
    bank = load_bank()
    if not bank:
        return "# Interview Story Bank\n\n_empty_\n"

    lines = ["# Interview Story Bank", ""]
    lines.append(f"{len(bank)} stories\n")
    for i, s in enumerate(bank, 1):
        lines.append(f"## {i}. {s.title}")
        lines.append("")
        if s.question_prompt:
            lines.append(f"**Answers:** {s.question_prompt}")
            lines.append("")
        if s.company or s.job_title:
            lines.append(f"_Generated for: {s.job_title} @ {s.company}_")
            lines.append("")
        lines.append(f"**Situation.** {s.situation}")
        lines.append("")
        lines.append(f"**Task.** {s.task}")
        lines.append("")
        lines.append(f"**Action.** {s.action}")
        lines.append("")
        lines.append(f"**Result.** {s.result}")
        lines.append("")
        lines.append(f"**Reflection.** {s.reflection}")
        lines.append("")
        lines.append("---")
        lines.append("")
    return "\n".join(lines)
