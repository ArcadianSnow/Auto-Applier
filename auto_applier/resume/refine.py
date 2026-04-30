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

    ``skill`` is the cleaned skill name (e.g. "Power BI"), used both
    for display to the user and as the storage key on the resume
    profile. ``raw_label`` is the original question text from the
    form (e.g. "Years of experience with Power BI") — kept so the
    user can see exactly how the question was asked.
    """
    skill: str
    count: int
    resume_label: str       # which resume was used on jobs with this gap
    archetype: str          # analyst, engineer, etc.
    sample_companies: list[str] = field(default_factory=list)
    sample_titles: list[str] = field(default_factory=list)
    category: str = "skill"
    raw_label: str = ""     # the original form-field label


# Question phrasings that wrap the actual skill name. Stripped from
# the front of the field label to extract a clean skill name (e.g.
# "Years of experience with Power BI" → "Power BI"). Order matters:
# longest-prefix-first so we don't strip a partial match.
_SKILL_PREFIX_PATTERNS = (
    "how many years of experience do you have with",
    "how many years of experience do you have using",
    "how many years of experience with",
    "how many years of experience using",
    "how many years experience with",
    "years of experience with",
    "years of experience using",
    "years experience with",
    "years experience using",
    "do you have experience with",
    "do you have experience using",
    "do you have experience in",
    "do you have hands-on experience with",
    "are you experienced with",
    "are you experienced in",
    "are you familiar with",
    "are you proficient with",
    "are you proficient in",
    "are you skilled in",
    "experience with",
    "experience using",
    "experience in",
    "proficiency with",
    "proficiency in",
    "knowledge of",
    "familiarity with",
)

# Trailing fragments to strip after the skill name.
_SKILL_SUFFIX_PATTERNS = (
    "?",
    " (years)",
    " (in years)",
    " in years",
    " — required",
    " - required",
    "  *",
    " *",
    "*",
    " required",
    " (required)",
    " years",
)


def _extract_skill_name(raw_label: str) -> str:
    """Pull a clean skill name out of a question-form field label.

    Examples:
      "Years of experience with Power BI"        → "Power BI"
      "How many years of experience with SQL?"   → "SQL"
      "Are you familiar with dbt?"               → "dbt"
      "AWS"                                      → "AWS"  (already clean)
      "Voluntary self identification questions"  → returned unchanged
                                                   (handled upstream by the
                                                    skill-shape filter, but
                                                    if it slips through we
                                                    don't want to mangle it)

    Falls back to title-casing the raw label when no pattern matches.
    """
    s = raw_label.strip()
    lower = s.lower()
    # Strip trailing punctuation / boilerplate first so prefix
    # matchers don't have to handle every variant.
    for suffix in _SKILL_SUFFIX_PATTERNS:
        if lower.endswith(suffix):
            s = s[: len(s) - len(suffix)].rstrip()
            lower = s.lower()
    # Strip the prefix.
    for prefix in _SKILL_PREFIX_PATTERNS:
        if lower.startswith(prefix):
            s = s[len(prefix):].strip()
            break
    # Defensive: if stripping left us empty, fall back to the original.
    if not s:
        s = raw_label.strip()
    # Title-case very-short results so "sql" becomes "SQL"-ish.
    # Lots of tech names are acronyms; keep the user's original
    # casing if it had any uppercase letters.
    if not any(c.isupper() for c in s):
        s = s.title()
    return s


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

    # Group by (resume, normalized-skill-name) — count occurrences
    # across all archetypes. Two questions worded slightly differently
    # but asking about the same skill ("Years of experience with SQL"
    # vs "How many years of experience do you have with SQL?") collapse
    # to the same group via the extractor. Archetype becomes metadata
    # (the most common one for this skill on this resume).
    grouped: dict[tuple[str, str], list[tuple[GapContext, str]]] = defaultdict(list)
    for ctx in contexts:
        raw_label = ctx.gap.field_label.strip()
        clean_skill = _extract_skill_name(raw_label)
        key = clean_skill.lower().strip()
        if key in excluded or raw_label.lower().strip() in excluded:
            continue
        grouping = (ctx.gap.resume_label or "(unknown)", key)
        grouped[grouping].append((ctx, raw_label))

    # Turn groupings into candidates
    candidates: list[RefineCandidate] = []
    for (resume, skill_key), items in grouped.items():
        if len(items) < min_count:
            continue
        # All items in a group share a normalized skill name. Use
        # the first raw_label's extracted form as the display name
        # (preserves original casing — "Power BI" not "power bi").
        clean_skill = _extract_skill_name(items[0][1])
        # Most common archetype for this skill+resume becomes the
        # surfaced label (used for grouping the session display).
        archetype_counts: Counter = Counter(i[0].archetype for i in items)
        primary_archetype = archetype_counts.most_common(1)[0][0]

        companies: list[str] = []
        titles: list[str] = []
        seen_co: set[str] = set()
        seen_ti: set[str] = set()
        for ctx, _raw in items:
            if ctx.company and ctx.company not in seen_co and len(companies) < 3:
                companies.append(ctx.company)
                seen_co.add(ctx.company)
            if ctx.job_title and ctx.job_title not in seen_ti and len(titles) < 3:
                titles.append(ctx.job_title)
                seen_ti.add(ctx.job_title)
        candidates.append(RefineCandidate(
            skill=clean_skill,
            count=len(items),
            resume_label=resume,
            archetype=primary_archetype,
            sample_companies=companies,
            sample_titles=titles,
            category=items[0][0].gap.category,
            raw_label=items[0][1],
        ))

    # Within each (resume, primary_archetype), cap to top N by count
    # so the session stays focused. A skill that primarily appears
    # in analyst jobs counts toward analyst's quota; one in engineer
    # jobs counts toward engineer's quota.
    by_group: dict[tuple[str, str], list[RefineCandidate]] = defaultdict(list)
    for c in candidates:
        by_group[(c.resume_label, c.archetype)].append(c)
    for group, items in by_group.items():
        items.sort(key=lambda c: -c.count)
        del items[max_per_group:]

    # Flatten, sort by frequency overall
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
        # The base RESUME_BULLET system prompt now carries the full
        # hallucination guard. No need to override here — keeps the
        # GUI evolution panel and CLI refine on the same guardrails.
        result = await router.complete_json(
            prompt=prompt_text,
            system_prompt=RESUME_BULLET.system,
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
