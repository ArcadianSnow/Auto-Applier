"""Grounded company-research briefings (spec §11 Phase 6 extras — on-demand only).

Given a company name and a blob of *user-provided* source material (text pasted
from a career page, a news article, a review site, or the user's own notes),
produces a structured interview-prep briefing and saves it as markdown + JSON
under ``research/`` in the data dir.

The LLM is explicitly told to stay within the source material — no inventing
claims — so the user gets a grounded summary, not a hallucination. A section the
source doesn't cover says "not in source" / stays empty rather than filling in
guesses.

Zero-egress by construction: the source material is pasted by the user and the
model is local Ollama — this feature never fetches anything from the network
(the product's zero-cost/local-first rule, spec §2).
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from pathlib import Path

from auto_applier.domain.dedup import normalize
from auto_applier.domain.models import utcnow_iso
from auto_applier.llm.complete import CompletionClient
from auto_applier.llm.prompts import COMPANY_RESEARCH

logger = logging.getLogger(__name__)


@dataclass
class CompanyBriefing:
    company: str
    what_they_do: str
    tech_stack_signals: list[str] = field(default_factory=list)
    culture_signals: list[str] = field(default_factory=list)
    red_flags: list[str] = field(default_factory=list)
    questions_to_ask: list[str] = field(default_factory=list)
    talking_points: list[str] = field(default_factory=list)
    generated_at: str = field(default_factory=utcnow_iso)

    def to_markdown(self) -> str:
        """Render the briefing as a human-readable markdown document."""
        lines = [f"# {self.company}", "", f"_Generated {self.generated_at}_", ""]

        def _section(title: str, content: str | list[str]) -> None:
            lines.append(f"## {title}")
            lines.append("")
            if isinstance(content, list):
                if not content:
                    lines.append("_not in source_")
                else:
                    lines.extend(f"- {item}" for item in content)
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


def briefing_path(research_dir: Path | str, company: str) -> Path:
    """Canonical markdown path for a company's briefing (JSON sits beside it)."""
    safe = normalize(company).replace(" ", "_") or "unknown"
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in safe)
    return Path(research_dir) / f"{safe}.md"


class CompanyResearcher:
    """Builds briefings from user-pasted source material via the local LLM."""

    def __init__(self, llm: CompletionClient) -> None:
        self._llm = llm

    async def research(
        self, company: str, source_material: str
    ) -> CompanyBriefing | None:
        """Return a briefing, or ``None`` on empty source / LLM failure / an
        all-empty reply. Never raises — a prep nicety must not crash a session.

        Requires ``what_they_do`` non-empty (and not the "not in source"
        sentinel) so an all-sections-empty shell never gets persisted.
        """
        if not source_material.strip():
            logger.info("No source material for %s — refusing to invent", company)
            return None
        prompt = COMPANY_RESEARCH.format(
            company_name=company,
            source_material=source_material[:8000],
        )
        try:
            payload = await self._llm.complete_json(prompt, system=COMPANY_RESEARCH.system)
        except Exception as exc:  # noqa: BLE001 — deliberate catch-all (CompletionError, HTTP, parse)
            logger.warning("Research LLM call failed for %s: %s", company, exc)
            return None
        if not isinstance(payload, dict):
            return None

        what = str(payload.get("what_they_do", "")).strip()
        if not what or what.lower() == "not in source":
            logger.info("Research reply had no grounded 'what_they_do' for %s", company)
            return None

        def _list(key: str) -> list[str]:
            val = payload.get(key, [])
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


def save_briefing(research_dir: Path | str, briefing: CompanyBriefing) -> Path:
    """Persist a briefing as markdown + JSON side-by-side. Returns the md path."""
    md_path = briefing_path(research_dir, briefing.company)
    md_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.write_text(briefing.to_markdown(), encoding="utf-8")
    md_path.with_suffix(".json").write_text(
        json.dumps(asdict(briefing), indent=2), encoding="utf-8"
    )
    return md_path


def load_briefing(research_dir: Path | str, company: str) -> CompanyBriefing | None:
    """Load a previously saved briefing, or ``None`` if missing/corrupt."""
    json_path = briefing_path(research_dir, company).with_suffix(".json")
    if not json_path.exists():
        return None
    try:
        data = json.loads(json_path.read_text(encoding="utf-8"))
        valid = set(CompanyBriefing.__dataclass_fields__)
        return CompanyBriefing(**{k: v for k, v in data.items() if k in valid})
    except (json.JSONDecodeError, TypeError, OSError):
        return None


__all__ = [
    "CompanyBriefing",
    "CompanyResearcher",
    "briefing_path",
    "load_briefing",
    "save_briefing",
]
