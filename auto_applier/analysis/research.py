"""Deep company research briefing generator.

Given a company name and an optional blob of source material (text
copy-pasted from a career page, a news article, a Glassdoor review,
or notes the user typed in), produces a structured interview-prep
briefing and saves it as a markdown file under ``data/research/``.

The LLM is explicitly told to stay within the source material — no
inventing claims — so users get a grounded summary, not a
hallucination. If the source lacks information for a section, the
section says "not in source" rather than filling in guesses.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from auto_applier.config import RESEARCH_DIR
from auto_applier.llm.prompts import COMPANY_RESEARCH
from auto_applier.llm.router import LLMRouter
from auto_applier.storage.dedup import normalize_company

logger = logging.getLogger(__name__)


@dataclass
class CompanyBriefing:
    company: str
    what_they_do: str
    tech_stack_signals: list[str]
    culture_signals: list[str]
    red_flags: list[str]
    questions_to_ask: list[str]
    talking_points: list[str]
    generated_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    def to_markdown(self) -> str:
        """Render the briefing as a human-readable markdown document."""
        lines = [f"# {self.company}", ""]
        lines.append(f"_Generated {self.generated_at}_")
        lines.append("")

        def _section(title: str, content):
            lines.append(f"## {title}")
            lines.append("")
            if isinstance(content, list):
                if not content:
                    lines.append("_not in source_")
                else:
                    for item in content:
                        lines.append(f"- {item}")
            else:
                lines.append(content or "_not in source_")
            lines.append("")

        _section("What they do", self.what_they_do)
        _section("Tech stack signals", self.tech_stack_signals)
        _section("Culture signals", self.culture_signals)
        _section("Red flags", self.red_flags)
        _section("Questions to ask", self.questions_to_ask)
        _section("Talking points", self.talking_points)
        return "\n".join(lines)


def briefing_path(company: str) -> Path:
    """Return the canonical path for a company's briefing file."""
    safe = normalize_company(company).replace(" ", "_") or "unknown"
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in safe)
    return RESEARCH_DIR / f"{safe}.md"


class CompanyResearcher:
    """Builds briefings from raw source material via the LLM router."""

    def __init__(self, router: LLMRouter) -> None:
        self.router = router

    async def research(
        self, company: str, source_material: str,
    ) -> CompanyBriefing | None:
        """Return a briefing, or None on LLM failure.

        Validates the response has at least ``what_they_do`` non-empty
        so an all-sections-empty shell doesn't get persisted.
        """
        if not source_material.strip():
            logger.debug("No source material for %s — refusing to invent", company)
            return None

        try:
            result = await self.router.complete_json(
                prompt=COMPANY_RESEARCH.format(
                    company_name=company,
                    source_material=source_material[:8000],
                ),
                system_prompt=COMPANY_RESEARCH.system,
            )
        except Exception as e:
            logger.debug("Research LLM call failed for %s: %s", company, e)
            return None

        what = str(result.get("what_they_do", "")).strip()
        if not what or what.lower() == "not in source":
            logger.debug("Research response had no 'what_they_do' for %s", company)
            return None

        def _list(key: str) -> list[str]:
            val = result.get(key, [])
            if not isinstance(val, list):
                return []
            return [str(v).strip() for v in val if str(v).strip()]

        return CompanyBriefing(
            company=company,
            what_they_do=what,
            tech_stack_signals=_list("tech_stack_signals"),
            culture_signals=_list("culture_signals"),
            red_flags=_list("red_flags"),
            questions_to_ask=_list("questions_to_ask"),
            talking_points=_list("talking_points"),
        )


def save_briefing(briefing: CompanyBriefing) -> Path:
    """Persist a briefing to disk as markdown + JSON side-by-side."""
    md_path = briefing_path(briefing.company)
    md_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.write_text(briefing.to_markdown(), encoding="utf-8")
    json_path = md_path.with_suffix(".json")
    json_path.write_text(json.dumps(asdict(briefing), indent=2), encoding="utf-8")
    return md_path


def load_briefing(company: str) -> CompanyBriefing | None:
    """Load a previously saved briefing, or None if missing."""
    json_path = briefing_path(company).with_suffix(".json")
    if not json_path.exists():
        return None
    try:
        data = json.loads(json_path.read_text(encoding="utf-8"))
        return CompanyBriefing(**data)
    except (json.JSONDecodeError, TypeError, OSError):
        return None
