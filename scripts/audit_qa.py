"""Audit form-fill answers in a run log against deterministic ground truth.

Compares every (question, answer) pair the form filler chose against
what *should* have been answered given the candidate profile in
data/user_config.json + data/answers.json. Pure pattern-matching, no
LLM — so it runs offline and produces the same report twice in a row.

Run:
    python scripts/audit_qa.py [path/to/run.log]
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path


def latest_log() -> Path:
    logs = sorted(
        Path("data/logs").glob("run-*.log"),
        key=lambda p: p.stat().st_mtime,
    )
    if not logs:
        raise SystemExit("No run logs found in data/logs/")
    return logs[-1]


def parse_log(log_path: Path) -> list[dict]:
    """Return per-field events with the final outcome.

    Prefers the canonical ``FIELD_RESULT`` log line (one per field,
    emitted at the end of every ``fill_field`` return path) — when
    present, every field is captured with no parser blind spots.
    Falls back to the legacy fill_field/matched/apply_answer trace
    parser for older logs that predate the canonical line.
    """
    text = log_path.read_text(encoding="utf-8", errors="replace")

    # Canonical line first.
    canonical_pattern = re.compile(
        r"FIELD_RESULT label=(?P<label>.+?) "
        r"type=(?P<type>\w+) applied=(?P<applied>True|False) "
        r"source=(?P<source>\S+) answer=(?P<answer>.+)$",
    )
    canonical: list[dict] = []
    for line in text.split("\n"):
        m = canonical_pattern.search(line)
        if not m:
            continue
        # label and answer come through repr-quoted; strip the quotes.
        label = m.group("label").strip()
        if (label.startswith("'") and label.endswith("'")) or (
            label.startswith('"') and label.endswith('"')
        ):
            label = label[1:-1]
        ans = m.group("answer").strip()
        if (ans.startswith("'") and ans.endswith("'")) or (
            ans.startswith('"') and ans.endswith('"')
        ):
            ans = ans[1:-1]
        canonical.append({
            "label": label,
            "type": m.group("type"),
            "applied": m.group("applied") == "True",
            "final_source": m.group("source"),
            "final_answer": ans,
            "matches": [],
        })

    if canonical:
        by_label: dict[str, dict] = {}
        for e in canonical:
            by_label[e["label"]] = e
        return list(by_label.values())

    # Legacy parser — for logs from before FIELD_RESULT existed.
    events: list[dict] = []
    current: dict | None = None
    for line in text.split("\n"):
        m = re.search(r"fill_field: label=(.+?) type=(\w+)", line)
        if m:
            if current:
                events.append(current)
            label = m.group(1).strip().strip("'\"")
            current = {
                "label": label,
                "type": m.group(2),
                "matches": [],
                "final_answer": None,
                "final_source": None,
                "applied": None,
            }
            continue
        if not current:
            continue
        m = re.search(r"matched ([\w.]+) (?:->|→) '(.+?)'", line)
        if m:
            current["matches"].append((m.group(1), m.group(2)))
            continue
        m = re.search(r"LLM returned (?:->|→) '(.+?)'", line)
        if m:
            current["matches"].append(("LLM", m.group(1)))
            continue
        m = re.search(
            r"apply_answer returned (True|False) for .*?\(via ([^)]+)\)",
            line,
        )
        if m:
            current["applied"] = m.group(1) == "True"
            current["final_source"] = m.group(2).strip()
            for src, val in reversed(current["matches"]):
                if src.lower().startswith(
                    m.group(2).split()[0].lower()
                ):
                    current["final_answer"] = val
                    break
            continue
        if "NO ANSWER FOUND" in line:
            current["final_answer"] = "(skipped — gap)"
            current["final_source"] = "GAP"
            continue
        m = re.search(
            r"Failed to fill field '(.+?)' \((\w+)\): (.+)$", line,
        )
        if m and current and current["label"].startswith(m.group(1)[:30]):
            current["fill_error"] = m.group(3)

    if current:
        events.append(current)

    by_label = {}
    for e in events:
        by_label[e["label"]] = e
    return list(by_label.values())


# ---------------------------------------------------------------------------
# Deterministic ground truth
# ---------------------------------------------------------------------------


YES_NO_RULES: list[tuple[str, str]] = [
    # (regex on lowered question, expected answer)
    (r"\b18\b.*\b(year|age|or older)", "Yes"),
    (r"at least 18", "Yes"),
    (r"legally? authorized to work", "Yes"),
    (r"authorized to work in (the )?(us|united states|this country)",
     "Yes"),
    (r"will (you|your).*(require|need).*sponsorship", "No"),
    (r"require (visa )?sponsorship", "No"),
    (r"(visa|h-?1b|h1b) sponsorship", "No"),
    (r"willing to (relocate|move)", "Yes"),
    (r"(open to|willing to) (work )?contract", "Yes"),
    (r"have (a )?valid driver'?s? license", "Yes"),
    (r"willing to undergo (a )?background check", "Yes"),
    (r"convicted of (a )?(felony|crime)", "No"),
    (r"have you (ever )?been (convicted|terminated)", "No"),
    (r"previously work(ed)? (for|at) (this|our) company", "No"),
    (r"former(ly)? employ(ed|ee) (at|of) (this|our)", "No"),
    (r"willing.*reliably commute", "Yes"),
    (r"reliably commute", "Yes"),
    (r"remote", "Yes"),
    (r"english", "Yes"),
]


SUBSTRING_RULES: dict[str, str] = {
    # question substring → answer (looked up in personal_info)
    "first name": "first_name",
    "last name": "last_name",
    "full name": "name",
    "email": "email",
    "phone": "phone",
    "city": "city",
    "state": "state",
    "zip code": "zip_code",
    "postal code": "postal_code",
    "street address": "street_address",
    "country": "country",
    "linkedin": "linkedin_url",
    "github": "github_url",
    "portfolio": "portfolio_url",
    "website": "portfolio_url",
    "salary": "desired_salary",
    "compensation": "desired_salary",
    "years of experience": "years_experience",
    "years of professional": "years_experience",
}


def expected_for(question: str, profile: dict, answers: dict) -> tuple[str, str]:
    """Return (expected_answer, source). source is one of 'rule',
    'personal_info', 'answers.json', 'free-text', 'unknown'."""
    q = question.lower()

    # 1. Rule-based yes/no
    for pattern, ans in YES_NO_RULES:
        if re.search(pattern, q):
            return ans, "rule"

    # 2. answers.json substring containment (either direction)
    for stored_q, stored_a in answers.items():
        sq = stored_q.lower()
        if sq in q or q in sq:
            return stored_a, "answers.json"

    # 3. Personal info substrings
    for sub, key in SUBSTRING_RULES.items():
        if sub in q:
            v = profile.get(key, "")
            if v:
                return v, f"personal_info[{key}]"

    # 4. Generic year-N questions
    if "how many years" in q or "years of experience" in q:
        return profile.get("years_experience", ""), "personal_info[years_experience]"

    # 5. Source-attribution
    if any(p in q for p in ("how did you hear", "where did you hear", "how did you find")):
        return "Indeed/Dice/ZipRecruiter", "free-text"

    # 6. Start date
    if "start date" in q or "earliest start" in q or "when can you start" in q:
        return "Two weeks from offer acceptance", "free-text"

    return "(no rule)", "unknown"


def semantic_match(actual: str, expected: str, field_type: str) -> str:
    a = (actual or "").strip().lower()
    e = (expected or "").strip().lower()
    if not a or "skipped" in a:
        return "MISMATCH"
    if e == "(no rule)":
        return "UNKNOWN"
    if a == e:
        return "OK"
    if e in ("yes", "no") and a in ("yes", "no"):
        return "OK" if a == e else "MISMATCH"
    if field_type == "number":
        try:
            return "OK" if float(re.sub(r"[^\d.]", "", a)) == float(re.sub(r"[^\d.]", "", e)) else "MISMATCH"
        except (ValueError, ZeroDivisionError):
            pass
    if e in a or a in e:
        return "OK"
    if len(e) > 5 and len(a) > 5:
        ea = set(re.findall(r"\w+", e))
        aa = set(re.findall(r"\w+", a))
        if ea & aa and len(ea & aa) >= max(2, min(len(ea), len(aa)) // 2):
            return "WEAK"
    return "MISMATCH"


def main(log_path: Path) -> None:
    cfg = json.load(open("data/user_config.json", encoding="utf-8"))
    profile = cfg.get("personal_info", {})
    answers = json.load(open("data/answers.json", encoding="utf-8"))

    events = parse_log(log_path)
    interesting = [
        e for e in events
        if e["type"] in ("text", "radio", "select", "textarea", "number")
        and len(e["label"]) > 5
    ]

    rows = []
    for e in interesting:
        actual = e["final_answer"] or ""
        expected, exp_src = expected_for(e["label"], profile, answers)
        match = semantic_match(actual, expected, e["type"])
        rows.append({
            "label": e["label"],
            "type": e["type"],
            "actual": actual,
            "actual_source": e["final_source"] or "?",
            "applied": e["applied"],
            "expected": expected,
            "expected_source": exp_src,
            "match": match,
            "fill_error": e.get("fill_error"),
        })

    # Output
    out = []
    out.append(f"# Q-A audit -- {log_path.name}\n")
    out.append(f"Total fields: {len(events)}\n")
    out.append(f"Audited (real questions): {len(rows)}\n\n")

    counts = {"OK": 0, "MISMATCH": 0, "WEAK": 0, "UNKNOWN": 0}
    for r in rows:
        counts[r["match"]] += 1
    out.append(
        f"OK={counts['OK']}  MISMATCH={counts['MISMATCH']}  "
        f"WEAK={counts['WEAK']}  UNKNOWN={counts['UNKNOWN']}\n\n"
    )

    out.append("## Summary table\n\n")
    out.append("| Match | Type | Question | Actual | Source | Expected | Expected source |\n")
    out.append("|---|---|---|---|---|---|---|\n")
    severity = {"MISMATCH": 0, "WEAK": 1, "UNKNOWN": 2, "OK": 3}
    for r in sorted(rows, key=lambda x: severity[x["match"]]):
        sym = {"OK": "[OK]", "MISMATCH": "[BUG]", "WEAK": "[?]",
               "UNKNOWN": "[--]"}[r["match"]]
        q = r["label"][:60].replace("|", "\\|").replace("\n", " ")
        a = (r["actual"] or "")[:30].replace("|", "\\|")
        x = (r["expected"] or "")[:30].replace("|", "\\|")
        out.append(
            f"| {sym} | {r['type']} | {q} | {a} | {r['actual_source']} | "
            f"{x} | {r['expected_source']} |\n"
        )

    out.append("\n## Mismatches (likely bugs)\n\n")
    mismatches = [r for r in rows if r["match"] == "MISMATCH"]
    if not mismatches:
        out.append("None.\n")
    for r in mismatches:
        out.append(f"### {r['label']}\n")
        out.append(f"- **Type**: {r['type']}\n")
        out.append(
            f"- **Actual**: {r['actual']!r} (via {r['actual_source']})\n"
        )
        out.append(
            f"- **Expected**: {r['expected']!r} (per {r['expected_source']})\n"
        )
        if r["fill_error"]:
            out.append(f"- **Fill error**: {r['fill_error']}\n")
        out.append("\n")

    text = "".join(out)
    print(text)


if __name__ == "__main__":
    log = Path(sys.argv[1]) if len(sys.argv) > 1 else latest_log()
    main(log)
