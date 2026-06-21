"""Goal-elicitation chat for onboarding (research/future-directions.md Direction 1, Phase B).

A scripted, multi-turn Q&A that turns a new user's plain-language answers into the structured
:class:`~auto_applier.config.settings.TargetingConfig` the pipeline already consumes (titles /
locations / remote / salary_floor / seniority) plus a soft ``preferences`` blob for later ranking.

**The design principle (the 8B mitigation, see the doc's "Cons / concerns"):** the FLOW is scripted
and deterministic — this module decides which question comes next, never the model. The local LLM's
only job is to PARSE one free-text answer into the fields for that one step (a bounded extraction,
the same posture as the résumé extractor and the copilot's evidence audit). Two consequences:

  * Every LLM step has a **deterministic fallback** (``llm=None`` or any LLM error → keyword/regex
    parse). The chat therefore NEVER fails mid-conversation the way the one-shot extract endpoint can
    502 — the worst case is a rougher parse the user edits in the form. Interactive UX must degrade
    gracefully.
  * The salary step is parsed by **regex only** (no LLM) — a number with optional ``k``/``$``/commas
    is more reliable deterministically than via a model.

This module is pure logic with the LLM injected, so it tests both ways (with a stub LLM and with
``llm=None``). It does NOT persist anything: it returns an evolving draft the wizard shows for review,
and the existing ``POST /api/onboarding/targeting`` writer remains the single writer (review-before-
save, same discipline as the résumé-upload prefill).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from auto_applier.llm.prompts import GOAL_ELICIT

__all__ = [
    "ChatStep",
    "GOAL_STEPS",
    "apply_updates",
    "detect_relocation",
    "first_step",
    "next_step_after",
    "parse_answer",
    "scan_salary",
    "step_for_key",
    "suggest_adjacent_roles",
    "summarize",
]


@dataclass(frozen=True)
class ChatStep:
    """One scripted question. ``fields`` is the JSON-key spec injected into the GOAL_ELICIT
    prompt (empty for steps parsed deterministically without the LLM)."""

    key: str
    question: str
    fields: str


GOAL_STEPS: tuple[ChatStep, ...] = (
    ChatStep(
        "roles",
        "What kind of roles are you looking for? Include the level if you know it — "
        'e.g. "senior backend engineer" or "data analyst, mid-level".',
        '  "titles": [str]   - concrete job titles to search for\n'
        '  "seniority": str  - one of junior|mid|senior|staff, or "" if unstated',
    ),
    ChatStep(
        "location",
        "Where do you want to work — fully remote, a specific city or country, or open to "
        'relocating? For example: "remote in the US" or "Amsterdam, or remote in the EU".',
        '  "locations": [str]  - cities/regions/countries to work in or relocate to; '
        "[] if remote-anywhere\n"
        '  "remote_ok": bool   - true if open to remote work\n'
        '  "onsite_ok": bool   - true if open to on-site / in-office work',
    ),
    ChatStep(
        "comp",
        "Is there a minimum salary you'd need to consider a role? Optional — say "
        '"no minimum" to skip.',
        "",  # parsed deterministically (regex); no LLM call
    ),
    ChatStep(
        "priorities",
        "Last one: what matters most in your next role, and is anything a deal-breaker? "
        "e.g. work-life balance, a specific tech stack, no on-call, visa sponsorship.",
        '  "preferences": [str]  - short phrases for what matters most or any deal-breakers',
    ),
)

_SENIORITY = {"junior", "mid", "senior", "staff"}

# Cues that the user is open to moving for a role (so on-site shouldn't be filtered out, and the
# intent shouldn't be silently dropped when it's mentioned in the location answer).
_RELOCATION_CUES = (
    "relocat", "abroad", "overseas", "visa", "sponsor", "international",
    "moving", "emigrat", "expat", "another country", "send me",
)

# Deterministic adjacent-role suggestions for a vague/narrow roles answer. The 8B model is
# unreliable at "expand this into adjacent titles," so this is a curated map (LLM stays a bounded
# parser, never the source of suggestions — the same design posture as the rest of this module).
# Each entry: (keyword needles → suggested titles). First matching family wins; already-chosen
# titles are filtered out by the caller.
_ROLE_FAMILIES: tuple[tuple[tuple[str, ...], tuple[str, ...]], ...] = (
    (("data analyst", "data analysis", "analytics", "business intelligence", "bi ",
      "data related", "something with data", " data"),
     ("Data Analyst", "Data Engineer", "Analytics Engineer", "BI Analyst", "Data Scientist")),
    (("database", "dba", "sql server", "postgres", "oracle"),
     ("Database Administrator", "Data Engineer", "Database Engineer", "Analytics Engineer")),
    (("machine learning", "ml engineer", " ml ", "deep learning", " ai "),
     ("Machine Learning Engineer", "Data Scientist", "MLOps Engineer", "AI Engineer")),
    (("devops", "sre", "site reliability", "infrastructure", "platform eng"),
     ("DevOps Engineer", "Site Reliability Engineer", "Platform Engineer", "Cloud Engineer")),
    (("frontend", "front-end", "front end", "react", "ui engineer"),
     ("Frontend Engineer", "Full Stack Engineer", "UI Engineer", "Software Engineer")),
    (("full stack", "fullstack", "full-stack"),
     ("Full Stack Engineer", "Backend Engineer", "Frontend Engineer", "Software Engineer")),
    (("backend", "back-end", "back end", "server-side", "api"),
     ("Backend Engineer", "Software Engineer", "Platform Engineer", "API Engineer")),
    (("software", "developer", "swe", "programmer", "coding", "engineer"),
     ("Software Engineer", "Backend Engineer", "Full Stack Engineer", "Platform Engineer")),
)


# --------------------------------------------------------------- flow (deterministic)


def first_step() -> ChatStep:
    return GOAL_STEPS[0]


def step_for_key(key: str) -> ChatStep | None:
    for s in GOAL_STEPS:
        if s.key == key:
            return s
    return None


def next_step_after(key: str) -> ChatStep | None:
    """The next step in the script after ``key``, or ``None`` when the chat is complete. Strictly
    ordered — the flow never branches on model output."""
    keys = [s.key for s in GOAL_STEPS]
    if key not in keys:
        return None
    i = keys.index(key)
    return GOAL_STEPS[i + 1] if i + 1 < len(GOAL_STEPS) else None


# --------------------------------------------------------------- parsing helpers


def _split_phrases(text: str) -> list[str]:
    """Split a free-text answer into trimmed, de-duplicated phrases on commas / newlines /
    semicolons. Conservative on purpose (no splitting on ' and ' / '/') so multi-word titles
    survive the deterministic fallback; the user reviews the form regardless."""
    out: list[str] = []
    seen: set[str] = set()
    for part in re.split(r"[,\n;]+", text or ""):
        p = part.strip().strip(".").strip()
        if not p:
            continue
        key = p.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(p)
    return out


def _clean_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for x in value:
        if x is None:
            continue
        s = str(x).strip()
        if not s:
            continue
        key = s.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(s)
    return out


def _clean_seniority(value: object) -> str:
    s = str(value or "").strip().lower()
    return s if s in _SENIORITY else ""


def _scan_seniority(answer: str) -> str:
    low = (answer or "").lower()
    if re.search(r"\b(staff|principal|distinguished|lead)\b", low):
        return "staff"
    if re.search(r"\b(senior|sr\.?)\b", low):
        return "senior"
    if re.search(r"\b(junior|jr\.?|entry[- ]?level|entry|new ?grad|graduate)\b", low):
        return "junior"
    if re.search(r"\b(mid|mid[- ]?level|intermediate)\b", low):
        return "mid"
    return ""


def _parse_comp(answer: str) -> dict:
    """Deterministic salary-floor parse. Honours explicit "no minimum" phrasing; otherwise pulls
    the first number, applying ``k``/``m`` suffixes and treating a bare ``< 1000`` as thousands
    ("150" → $150,000 — annual salaries aren't three-digit dollars)."""
    text = (answer or "").strip().lower()
    if not text:
        return {"salary_floor": None}
    if any(
        p in text
        for p in (
            "no min", "no minimum", "no floor", "none", "n/a", "flexible",
            "doesn't matter", "doesnt matter", "don't matter", "dont matter",
            "whatever", "any salary", "not sure", "unsure", "open to anything",
        )
    ):
        return {"salary_floor": None}
    m = re.search(r"\$?\s*(\d[\d,]*(?:\.\d+)?)\s*([km])?", text)
    if not m:
        return {"salary_floor": None}
    num = float(m.group(1).replace(",", ""))
    suffix = m.group(2)
    if suffix == "k":
        num *= 1_000
    elif suffix == "m":
        num *= 1_000_000
    elif num < 1000:
        num *= 1_000
    return {"salary_floor": int(round(num))}


def scan_salary(answer: str) -> int | None:
    """Opportunistically detect a salary figure stated in free text (e.g. "I make $82k").

    Conservative on purpose: only fires on a number carrying a ``$``/``k``/``m`` cue, or a 4+ digit
    number near a pay keyword — so "3 years" or a team size won't be misread as salary. Returns
    annual dollars, or ``None``. Used so the chat doesn't ask for a minimum it was already told."""
    text = (answer or "").lower()
    if not text:
        return None
    m = re.search(r"\$\s*(\d[\d,]*(?:\.\d+)?)\s*([km])?|\b(\d[\d,]*(?:\.\d+)?)\s*([km])\b", text)
    if m:
        if m.group(1) is not None:
            num = float(m.group(1).replace(",", "")); suffix = m.group(2)
        else:
            num = float(m.group(3).replace(",", "")); suffix = m.group(4)
        if suffix == "k":
            num *= 1_000
        elif suffix == "m":
            num *= 1_000_000
        elif num < 1000:
            num *= 1_000
        return int(round(num))
    if re.search(r"\b(salary|pay|paid|make|making|earn|earning|comp|compensation|income|wage)\b", text):
        m2 = re.search(r"\b(\d[\d,]{3,})\b", text)  # 4+ digits → looks like an annual figure
        if m2:
            return int(m2.group(1).replace(",", ""))
    return None


def detect_relocation(answer: str) -> bool:
    """True if the answer signals openness to relocating / working abroad / needing sponsorship."""
    return any(cue in (answer or "").lower() for cue in _RELOCATION_CUES)


def suggest_adjacent_roles(titles: list[str], answer: str, *, limit: int = 5) -> list[str]:
    """Deterministic adjacent-role suggestions for a vague/narrow roles answer (the "suggest, you
    confirm" widening). Returns titles NOT already chosen, capped at ``limit``; empty when nothing
    matches (no guessing — the caller only surfaces these for the user to opt into)."""
    hay = " " + (" ".join(titles) + " " + (answer or "")).lower() + " "
    have = {t.strip().lower() for t in titles}
    out: list[str] = []
    seen: set[str] = set()
    for needles, family in _ROLE_FAMILIES:
        if any(n in hay for n in needles):
            for role in family:
                key = role.lower()
                if key in have or key in seen:
                    continue
                seen.add(key)
                out.append(role)
            break  # first matching family only — keeps suggestions focused
    return out[:limit]


# --------------------------------------------------------------- per-step finalize
# Each finalize(raw, answer) merges the LLM's coerced output over the deterministic fallback, so
# finalize({}, answer) == the pure fallback (the llm=None / LLM-error path reuses the same code).


def _finalize_roles(raw: dict, answer: str) -> dict:
    titles = _clean_list(raw.get("titles")) or _split_phrases(answer)
    seniority = _clean_seniority(raw.get("seniority")) or _scan_seniority(answer)
    return {"titles": titles, "seniority": seniority}


def _finalize_location(raw: dict, answer: str) -> dict:
    low = (answer or "").lower()
    remote_default = any(
        s in low for s in ("remote", "anywhere", "wfh", "work from home")
    )
    onsite_default = any(
        s in low
        for s in ("onsite", "on-site", "on site", "in office", "in-office",
                  "office", "in person", "in-person", "hybrid")
    )
    if not remote_default and not onsite_default:
        # No signal either way → keep the wizard's permissive default (both on).
        remote_default = onsite_default = True

    locations = _clean_list(raw.get("locations"))
    if not locations:
        drop = {
            "remote", "anywhere", "onsite", "on-site", "on site", "hybrid",
            "relocate", "relocating", "open to relocation", "open to relocating",
            "wfh", "in office", "in-office", "in person", "in-person", "fully remote",
        }
        locations = [
            p for p in _split_phrases(answer)
            if p.lower() not in drop and "remote" not in p.lower()
        ]

    remote_ok = raw["remote_ok"] if isinstance(raw.get("remote_ok"), bool) else remote_default
    onsite_ok = raw["onsite_ok"] if isinstance(raw.get("onsite_ok"), bool) else onsite_default
    # Openness to relocating/abroad means on-site roles are in play (don't filter them out just
    # because the literal answer led with "remote") — and the intent must not be silently dropped.
    if detect_relocation(answer):
        onsite_ok = True
    return {"locations": locations, "remote_ok": remote_ok, "onsite_ok": onsite_ok}


def _finalize_priorities(raw: dict, answer: str) -> dict:
    prefs = _clean_list(raw.get("preferences")) or _split_phrases(answer)
    return {"preferences": prefs}


_FINALIZE = {
    "roles": _finalize_roles,
    "location": _finalize_location,
    "priorities": _finalize_priorities,
}


# --------------------------------------------------------------- public parse


async def _llm_parse(step: ChatStep, answer: str, llm) -> dict:
    # think=False + a tight num_predict: this is a structured copy-out, not reasoning, and qwen3's
    # thinking trace can run long/degenerate (the résumé-extraction finding, 2026-06-16). The API
    # think param is used, NOT the in-prompt "/no_think" token (which dropped content).
    prompt = GOAL_ELICIT.format(question=step.question, answer=answer, fields=step.fields)
    raw = await llm.complete_json(
        prompt, system=GOAL_ELICIT.system, think=False, num_predict=512,
    )
    return raw if isinstance(raw, dict) else {}


async def parse_answer(step_key: str, answer: str, llm=None) -> dict:
    """Parse a user's answer to ``step_key`` into targeting-field updates.

    ``comp`` is deterministic (regex). ``roles`` / ``location`` / ``priorities`` use the LLM as a
    bounded parser when ``llm`` is provided, falling back to keyword/split parsing on a missing LLM,
    an empty answer, or ANY LLM error — the chat never breaks mid-flow. Raises :class:`KeyError`
    for an unknown step."""
    step = step_for_key(step_key)
    if step is None:
        raise KeyError(step_key)
    answer = (answer or "").strip()
    if step_key == "comp":
        return _parse_comp(answer)
    finalize = _FINALIZE[step_key]
    if llm is None or not answer:
        updates = finalize({}, answer)
    else:
        try:
            raw = await _llm_parse(step, answer, llm)
        except Exception:  # noqa: BLE001 — degrade to the deterministic parse, never surface mid-chat
            raw = {}
        updates = finalize(raw, answer)
    # Opportunistically capture a salary the user volunteered in an earlier free-text answer (e.g.
    # "...I make $82k") so the dedicated comp step doesn't ask for it a second time.
    sal = scan_salary(answer)
    if sal is not None:
        updates = {**updates, "salary_floor": sal}
    return updates


def apply_updates(draft: dict | None, updates: dict | None) -> dict:
    """Merge a step's field updates into the running draft (a plain dict mirroring TargetingConfig).
    Lists arrive already cleaned/de-duped from :func:`parse_answer`; scalars overwrite. ``preferences``
    is the exception — it ACCUMULATES (union, de-duped) so a relocation note captured at the location
    step survives the later priorities step instead of being clobbered."""
    merged = dict(draft or {})
    for k, v in (updates or {}).items():
        if k == "preferences" and isinstance(v, list):
            existing = list(merged.get("preferences") or [])
            seen = {str(x).strip().lower() for x in existing}
            for item in v:
                key = str(item).strip().lower()
                if key and key not in seen:
                    seen.add(key)
                    existing.append(item)
            merged["preferences"] = existing
        else:
            merged[k] = v
    return merged


def summarize(draft: dict | None) -> str:
    """A friendly plain-text recap of the collected draft for the final chat turn."""
    d = draft or {}
    lines: list[str] = []

    titles = d.get("titles") or []
    lines.append("Roles: " + (", ".join(titles) if titles else "(none yet)"))

    if d.get("seniority"):
        lines.append("Level: " + str(d["seniority"]))

    where = list(d.get("locations") or [])
    if d.get("remote_ok"):
        where.append("remote")
    lines.append("Location: " + (", ".join(where) if where else "open / anywhere"))

    floor = d.get("salary_floor")
    if floor:
        lines.append(f"Min salary: ${int(floor):,}")

    prefs = d.get("preferences") or []
    if prefs:
        lines.append("Priorities: " + ", ".join(prefs))

    body = "\n".join("• " + line for line in lines)
    return (
        "Here's what I've got:\n" + body
        + "\n\nLook right? Click “Use these answers” to fill the form below, then tweak "
        "anything and Save."
    )
