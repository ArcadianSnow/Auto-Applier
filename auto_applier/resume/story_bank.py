"""STAR+Reflection interview story bank (spec §11 Phase 6 extras — on-demand only).

Generates 3 short stories tailored to a specific job, each a STAR+R record —
Situation, Task, Action, Result, Reflection — drawn ONLY from the master fact bank
so the candidate recognizes the material when they read it back during interview
prep. The same fabrication rule as résumé generation applies: bank facts may be
selected/recombined/rephrased, never invented (the LLM is instructed; results with
unverifiable numbers should be treated as prep notes, not submitted claims).

Stories accumulate in ``story_bank.json`` under the data dir (file-grain, like
artifacts — the bank is a personal prep library, not pipeline state). The bank is
append-only from generation; the user prunes by editing/deleting the file. Over
time it becomes a reusable library of master stories that can answer any
behavioral question.

On-demand only (spec §11): nothing in the pipeline calls this — the user runs
``av3 stories generate <job_id>`` when they have an interview coming.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from pathlib import Path

from auto_applier.llm.complete import CompletionClient
from auto_applier.llm.prompts import STAR_STORIES
from auto_applier.resume.factbank import FactBank
from auto_applier.resume.generate import build_bank_facts, format_allowed_metrics
from auto_applier.domain.models import utcnow_iso

logger = logging.getLogger(__name__)

#: STAR+R segments every story must carry non-empty.
_REQUIRED_SEGMENTS = ("situation", "task", "action", "result", "reflection")


@dataclass
class Story:
    """One STAR+R story with provenance back to the job it was generated for."""

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
    created_at: str = field(default_factory=utcnow_iso)


# --------------------------------------------------------------------- persistence

def load_bank(path: Path | str) -> list[Story]:
    """Load the story bank. Missing/invalid file → empty list (never raises)."""
    path = Path(path)
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Failed to load story bank %s: %s", path, exc)
        return []
    items = raw if isinstance(raw, list) else raw.get("stories", [])
    valid_fields = set(Story.__dataclass_fields__)
    out: list[Story] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        try:
            out.append(Story(**{k: v for k, v in it.items() if k in valid_fields}))
        except TypeError:
            continue
    return out


def save_bank(path: Path | str, stories: list[Story]) -> None:
    """Persist the bank as a JSON list (parent dirs created)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps([asdict(s) for s in stories], indent=2), encoding="utf-8"
    )


def append_stories(path: Path | str, new_stories: list[Story]) -> None:
    """Append stories to the bank on disk. No-op for an empty list."""
    if not new_stories:
        return
    bank = load_bank(path)
    bank.extend(new_stories)
    save_bank(path, bank)


# --------------------------------------------------------------------- generation

class StoryGenerator:
    """Generates tailored STAR+R stories from the fact bank via the local LLM."""

    def __init__(self, llm: CompletionClient) -> None:
        self._llm = llm

    async def generate(
        self,
        bank: FactBank,
        job_description: str,
        *,
        company: str = "",
        title: str = "",
        job_id: str = "",
    ) -> list[Story]:
        """Generate 3 STAR+R stories for one job. Returns ``[]`` on any LLM
        failure or malformed reply — never raises (an interview-prep nicety must
        not crash a CLI session). Every empty return logs why."""
        prompt = STAR_STORIES.format(
            bank_facts=build_bank_facts(bank),
            allowed_metrics=format_allowed_metrics(bank),
            company=company or "the company",
            title=title or "the role",
            job_description=job_description[:4000],
        )
        try:
            payload = await self._llm.complete_json(prompt, system=STAR_STORIES.system)
        except Exception as exc:  # noqa: BLE001 — deliberate catch-all (CompletionError, HTTP, parse)
            logger.warning("Story generation failed for job %s: %s", job_id or "?", exc)
            return []

        items = payload.get("stories", []) if isinstance(payload, dict) else []
        if not isinstance(items, list) or not items:
            logger.info(
                "Story reply had no usable 'stories' list — none generated for job %s",
                job_id or "?",
            )
            return []

        stories: list[Story] = []
        for it in items:
            if not isinstance(it, dict):
                continue
            if not all(str(it.get(seg, "")).strip() for seg in _REQUIRED_SEGMENTS):
                continue  # require every STAR+R segment non-empty
            stories.append(Story(
                title=str(it.get("title", "Untitled story")),
                question_prompt=str(it.get("question_prompt", "")),
                situation=str(it.get("situation", "")),
                task=str(it.get("task", "")),
                action=str(it.get("action", "")),
                result=str(it.get("result", "")),
                reflection=str(it.get("reflection", "")),
                job_id=job_id,
                company=company,
                job_title=title,
            ))
        if not stories:
            logger.info(
                "All %d candidate stories filtered (missing STAR+R segments) for job %s",
                len(items), job_id or "?",
            )
        return stories


# --------------------------------------------------------------------- export

def export_bank_markdown(stories: list[Story]) -> str:
    """Render the bank as a human-readable markdown document for prep reading."""
    if not stories:
        return "# Interview Story Bank\n\n_empty_\n"
    lines = ["# Interview Story Bank", "", f"{len(stories)} stories", ""]
    for i, s in enumerate(stories, 1):
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


__all__ = [
    "Story",
    "StoryGenerator",
    "append_stories",
    "export_bank_markdown",
    "load_bank",
    "save_bank",
]
