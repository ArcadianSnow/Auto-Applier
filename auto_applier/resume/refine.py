"""Post-session conversational resume improvement.

The refine flow takes the gap data accumulated during applications
and walks the user through their most common missing skills. For
each gap the user picks one of:

- "I have experience" -> LLM drafts 2-3 resume bullets from the
  user's description, user approves, bullets are saved to the
  resume profile's ``confirmed_skills`` list.
- "I'm learning this" -> added to ``learning_goals.json:learning``
  so it isn't re-prompted until marked certified.
- "Not interested" -> added to ``prompted_skills.json`` (via the
  EvolutionEngine) and ``learning_goals:not_interested`` so it's
  filtered from all future reports.
- "Skip for now" -> no state change, will appear next refine.

The bullet generation prompt has strict hallucination guardrails:
LLM may only use facts the user provides. See ``RESUME_BULLET``
in ``llm/prompts.py`` for the enforcement.
"""
from __future__ import annotations

import logging
from collections import Counter, defaultdict
from dataclasses import dataclass, field

from auto_applier.analysis import learning_goals
from auto_applier.analysis.gap_tracker import GapContext, gaps_with_context
from auto_applier.llm.prompts import RESUME_BULLET
from auto_applier.llm.router import LLMRouter
from auto_applier.resume.evolution import EvolutionEngine
from auto_applier.resume.manager import ResumeManager

logger = logging.getLogger(__name__)


@dataclass
class RefineCandidate:
    """A skill gap ready to be reviewed in the refine flow.

    Sorted by (resume, archetype) groupings so a user can batch-review
    all their analyst-resume gaps at once, then engineer-resume gaps.
    """
    skill: str
    count: int
    resume_label: str       # which resume was used on jobs with this gap
    archetype: str          # analyst, engineer, etc.
    sample_companies: list[str] = field(default_factory=list)
    sample_titles: list[str] = field(default_factory=list)
    category: str = "skill"


@dataclass
class ResumeSuggestion:
    """Proposal to create a title-focused resume.

    Fires when a single resume is being used across divergent title
    types AND scoring poorly on one of them. Carries enough context
    for the CLI to present the user with a yes/no decision.
    """
    existing_resume: str
    target_archetype: str
    evidence_count: int          # how many apps went through this mismatch
    avg_score: float             # average score on the mismatched archetype
    example_titles: list[str] = field(default_factory=list)


def collect_refine_candidates(
    min_count: int = 2,
    max_per_group: int = 3,
) -> list[RefineCandidate]:
    """Load gap data and return skills ready for user review.

    Filters:
    - Excludes skills already in learning_goals (learning/certified/
      not_interested) — user has already dealt with them.
    - Excludes skills in prompted_skills.json (via EvolutionEngine).
    - Requires at least ``min_count`` occurrences before surfacing.
    - Caps to ``max_per_group`` skills per (resume, archetype) so the
      session stays short and actionable.

    Returns candidates sorted by count descending.
    """
    contexts = gaps_with_context()
    if not contexts:
        return []

    # Build filter sets
    goal_states = learning_goals.skills_by_state()
    excluded_from_goals = (
        goal_states["learning"]
        | goal_states["certified"]
        | goal_states["not_interested"]
    )
    evolution = EvolutionEngine()
    prompted = evolution._load_prompted()
    excluded = excluded_from_goals | prompted

    # Group by (resume, archetype, skill)
    grouped: dict[tuple[str, str, str], list[GapContext]] = defaultdict(list)
    for ctx in contexts:
        key = ctx.gap.field_label.lower().strip()
        if key in excluded:
            continue
        grouping = (
            ctx.gap.resume_label or "(unknown)",
            ctx.archetype,
            key,
        )
        grouped[grouping].append(ctx)

    # Turn groupings into candidates
    candidates: list[RefineCandidate] = []
    for (resume, archetype, skill), items in grouped.items():
        if len(items) < min_count:
            continue
        # Collect example companies/titles for the user to see where
        # this skill keeps appearing
        companies: list[str] = []
        titles: list[str] = []
        seen_co: set[str] = set()
        seen_ti: set[str] = set()
        for i in items:
            if i.company and i.company not in seen_co and len(companies) < 3:
                companies.append(i.company)
                seen_co.add(i.company)
            if i.job_title and i.job_title not in seen_ti and len(titles) < 3:
                titles.append(i.job_title)
                seen_ti.add(i.job_title)
        candidates.append(RefineCandidate(
            skill=skill,
            count=len(items),
            resume_label=resume,
            archetype=archetype,
            sample_companies=companies,
            sample_titles=titles,
            category=items[0].gap.category,
        ))

    # Within each (resume, archetype), keep only top N by count
    by_group: dict[tuple[str, str], list[RefineCandidate]] = defaultdict(list)
    for c in candidates:
        by_group[(c.resume_label, c.archetype)].append(c)
    for group, items in by_group.items():
        items.sort(key=lambda c: -c.count)
        # mutate in place: keep top max_per_group
        del items[max_per_group:]

    # Flatten, sort by frequency across the pool
    flattened = [c for items in by_group.values() for c in items]
    flattened.sort(key=lambda c: -c.count)
    return flattened


async def generate_bullets(
    skill: str,
    user_description: str,
    resume_label: str,
    resume_text: str,
    router: LLMRouter,
    level: str = "intermediate",
) -> list[str]:
    """Ask the LLM for 2-3 resume bullets based on user-provided facts.

    STRICT hallucination guardrail — the LLM is instructed to use ONLY
    what the user describes. If the user says "I built a dashboard",
    the LLM may write "Built a dashboard" but NOT "Built a dashboard
    tracking 12 KPIs for a Fortune 500 retailer."

    Returns an empty list if the user description is too vague or the
    LLM fails.
    """
    if not user_description.strip():
        return []

    prompt_text = RESUME_BULLET.format(
        skill_name=skill,
        skill_level=level,
        user_context=user_description.strip(),
        resume_excerpt=resume_text[:1500] if resume_text else "(no resume loaded)",
    )

    try:
        result = await router.complete_json(
            prompt=prompt_text,
            system_prompt=(
                RESUME_BULLET.system
                + "\n\nABSOLUTE RULES - violations make the tool unusable:\n"
                "- Use ONLY facts the user provided. DO NOT invent "
                "numbers, team sizes, employers, project scopes, "
                "timelines, or outcome metrics.\n"
                "- If the user's description is vague, return an empty "
                "list rather than padding with imagined details.\n"
                "- Return ONLY a JSON array of strings, no object wrapper. "
                "Example: [\"Built X\", \"Optimized Y\"]"
            ),
        )
    except Exception as exc:
        logger.warning("Bullet generation failed: %s", exc)
        return []

    # Accept both a plain list and a {bullets: [...]} shape, for LLM
    # flexibility across backends.
    if isinstance(result, list):
        bullets = result
    elif isinstance(result, dict):
        bullets = result.get("bullets", [])
    else:
        return []
    if not isinstance(bullets, list):
        return []

    cleaned: list[str] = []
    for b in bullets:
        if isinstance(b, str) and b.strip():
            cleaned.append(b.strip())
    return cleaned[:3]


def save_confirmed_skill(
    resume_label: str,
    skill: str,
    level: str,
    bullets: list[str],
    resume_manager: ResumeManager,
) -> bool:
    """Append a confirmed skill entry to the resume profile.

    The profile JSON has a ``confirmed_skills`` array that the
    ResumeManager reads back into the resume text during scoring and
    applying. Adding here makes this skill feel "on the resume" to
    the rest of the pipeline without modifying the original file.

    Returns True on success, False if the resume doesn't exist.
    """
    profile = resume_manager.get_profile(resume_label)
    if not profile:
        logger.warning(
            "Cannot save confirmed skill — resume '%s' not found", resume_label,
        )
        return False

    confirmed = profile.get("confirmed_skills", [])
    if not isinstance(confirmed, list):
        confirmed = []

    # Check for duplicate (same skill name, case-insensitive)
    skill_lower = skill.strip().lower()
    for entry in confirmed:
        if isinstance(entry, dict) and entry.get("name", "").lower() == skill_lower:
            # Update bullets if new, keep level
            entry["bullets"] = bullets
            entry["level"] = level
            profile["confirmed_skills"] = confirmed
            resume_manager.save_profile(resume_label, profile)
            return True

    confirmed.append({
        "name": skill,
        "level": level,
        "bullets": list(bullets),
    })
    profile["confirmed_skills"] = confirmed
    resume_manager.save_profile(resume_label, profile)
    logger.info(
        "Added confirmed skill '%s' to resume '%s' with %d bullets",
        skill, resume_label, len(bullets),
    )
    return True


def check_resume_suggestion(
    min_apps_per_archetype: int = 4,
    min_avg_score_threshold: float = 7.0,
) -> list[ResumeSuggestion]:
    """Scan application history for resume-archetype mismatches.

    Suggests creating a title-focused resume when a single resume is
    being used across multiple archetypes AND scoring poorly in some
    of them. Example trigger: data_analyst resume scored <7.0 across
    4+ data_engineer-archetype jobs -> suggest creating data_engineer
    resume.

    Returns a list of ``ResumeSuggestion`` (possibly empty). Called
    from cli refine to surface the proposal.
    """
    from auto_applier.storage.models import Application, Job
    from auto_applier.storage.repository import load_all
    from auto_applier.analysis.title_archetype import classify_title

    apps = load_all(Application)
    jobs = {j.job_id: j for j in load_all(Job)}

    # Only "applied" / "dry_run" rows have meaningful scores
    # Group by (resume, archetype) -> list of scores
    groups: dict[tuple[str, str], list[int]] = defaultdict(list)
    titles_per_group: dict[tuple[str, str], list[str]] = defaultdict(list)
    for app in apps:
        if app.status not in ("applied", "dry_run"):
            continue
        if not app.resume_used or app.score <= 0:
            continue
        job = jobs.get(app.job_id)
        if job is None:
            continue
        archetype = classify_title(job.title)
        if archetype == "other":
            continue
        key = (app.resume_used, archetype)
        groups[key].append(app.score)
        if len(titles_per_group[key]) < 3:
            titles_per_group[key].append(job.title)

    suggestions: list[ResumeSuggestion] = []
    for (resume, archetype), scores in groups.items():
        if len(scores) < min_apps_per_archetype:
            continue
        avg = sum(scores) / len(scores)
        if avg >= min_avg_score_threshold:
            continue
        # Don't suggest if the resume is already named after the
        # archetype (e.g. 'data_engineer' resume for engineer jobs)
        if archetype.lower() in resume.lower():
            continue
        suggestions.append(ResumeSuggestion(
            existing_resume=resume,
            target_archetype=archetype,
            evidence_count=len(scores),
            avg_score=avg,
            example_titles=titles_per_group[(resume, archetype)],
        ))

    suggestions.sort(key=lambda s: s.avg_score)  # worst first
    return suggestions
