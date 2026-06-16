"""Résumé → fact-bank extraction EVAL HARNESS (Direction 1, the final deliverable).

The regression detector for `extract_factbank` / the `EXTRACT_FACTBANK` prompt. A prompt or
model change can silently make extraction UNFAITHFUL (invent a company/skill/number — a
fabrication-guard breach) or INCOMPLETE (drop or merge a role — the observed qwen3 trap). This
harness pins both against a small hand-checked golden set so such a regression fails a test.

Mirrors tests/eval/test_score_quality.py:
  * tests/eval/resumes/<name>.txt          — a representative résumé (synthetic, no PII)
  * tests/eval/resumes/<name>.golden.json  — its hand-checked expected fact bank (master.json shape)
  * one LIVE test (@pytest.mark.eval, skipped unless Ollama is up) runs the real local LLM
  * always-run (no-marker) tests validate the golden FILES + the evaluator OFFLINE, so a broken
    golden / evaluator surfaces on every PR — not only when someone runs `pytest -m eval`.

.txt fixtures (not PDF/DOCX) deliberately isolate extraction QUALITY from file parsing — parsing
is already covered by tests/test_extract.py.

Definitions used by the asserts:
  * FAITHFULNESS (the load-bearing invariant): every extracted ATOM — company, title, skill,
    certification, institution, degree, allowed-metric, contact.name — is token-subset-grounded
    in the résumé text (every word of the atom appears as a word in the source). Token-subset
    (not raw substring) avoids false grounding like "git" within "digital". EXCLUDES bullets
    (rephrasable prose) and dates (normalization turns "Mar 2021" into "2021-03", which has no
    "03" token in the source).
  * COMPLETENESS (vs golden): every golden role is matched to a distinct extracted role (greedy
    title/company/start-year overlap → catches a DROPPED or MERGED role); skills-coverage ratio;
    metrics-coverage (digit-anchored); contact.name present.

Run live, against your configured model, after any EXTRACT_FACTBANK / model change:
    AV3_DATA_DIR=...your data dir...  pytest -m eval -k resume
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

import httpx
import pytest

from auto_applier.config.settings import load_settings
from auto_applier.llm.complete import build_default
from auto_applier.llm.prompts import EXTRACT_FACTBANK
from auto_applier.resume.extract import extract_factbank
from auto_applier.resume.factbank import FactBank, WorkEntry


RESUMES_DIR = Path(__file__).parent / "resumes"

# Live thresholds — calibrated from a real qwen3:8b run (see .claude/unstuck/plan-resume-eval.md).
# Faithfulness is absolute (zero invented atoms); role completeness is absolute (no dropped/merged
# roles); skills / metrics allow phrasing slack since the model legitimately rewords them.
_MIN_SKILLS_COV = 0.7
_MIN_METRICS_COV = 0.5


# --------------------------------------------------------------- token grounding

_WORD = re.compile(r"[a-z0-9]+")


def _tokenset(s: str) -> set[str]:
    return set(_WORD.findall((s or "").lower()))


def _digits(s: str) -> set[str]:
    return set(re.findall(r"\d+", s or ""))


def _grounded(value: str, source_tokens: set[str]) -> bool:
    """Every word of `value` appears as a word in the source (token-subset)."""
    t = _tokenset(value)
    return bool(t) and t <= source_tokens


def _covered(target: str, candidates: list[str]) -> bool:
    """`target` is represented among `candidates` — token-subset either direction (tolerates
    'SQL Server' vs 'Microsoft SQL Server' phrasing)."""
    tt = _tokenset(target)
    if not tt:
        return True
    for c in candidates:
        ct = _tokenset(c)
        if tt <= ct or ct <= tt:
            return True
    return False


def _metric_covered(golden_metric: str, extracted_metrics: list[str]) -> bool:
    """A golden metric is covered if its numbers all appear in some extracted metric (the number
    is the anchor; wording around it varies). Number-free metrics fall back to token coverage."""
    gd = _digits(golden_metric)
    if not gd:
        return _covered(golden_metric, extracted_metrics)
    return any(gd <= _digits(m) for m in extracted_metrics)


# --------------------------------------------------------------- scoring

def faithfulness_violations(bank: FactBank, source_text: str) -> list[str]:
    """Every extracted atom must be grounded in the source. Returns the ungrounded ones (empty ⇒
    faithful) — the fabrication-guard analog: anything here is a company/skill/number the model
    introduced that the résumé does not support."""
    src = _tokenset(source_text)
    bad: list[str] = []

    def chk(label: str, value: str) -> None:
        if value and value.strip() and not _grounded(value, src):
            bad.append(f"{label}: {value!r}")

    chk("contact.name", bank.contact.name)
    for w in bank.work_history:
        chk("company", w.company)
        chk("title", w.title)
    for e in bank.education:
        chk("institution", e.institution)
        chk("degree", e.degree)
    for s in bank.skills:
        chk("skill", s)
    for c in bank.certifications:
        chk("certification", c)
    for m in bank.allowed_metrics:
        chk("metric", m)
    return bad


def _match_roles(golden_roles: list[dict], extracted_roles: list) -> list[str]:
    """Greedy match: each golden role claims one distinct extracted role by title (weight 2) +
    company (1) + start-year (1) overlap; needs at least a title match. Returns the golden roles
    that found no match — i.e. roles the model DROPPED or MERGED."""
    used: set[int] = set()
    missing: list[str] = []
    for g in golden_roles:
        best, best_i = 0, -1
        for i, e in enumerate(extracted_roles):
            if i in used:
                continue
            score = 0
            if _covered(g.get("title", ""), [e.title]):
                score += 2
            if _covered(g.get("company", ""), [e.company]):
                score += 1
            if _digits(g.get("start", "")) & _digits(e.start):
                score += 1
            if score > best:
                best, best_i = score, i
        if best >= 2 and best_i >= 0:
            used.add(best_i)
        else:
            missing.append(f'{g.get("title", "?")} @ {g.get("company", "?")}')
    return missing


def completeness_report(bank: FactBank, golden: dict) -> dict:
    g_skills = list(golden.get("skills", []))
    g_metrics = list(golden.get("allowed_metrics", []))
    skills_missing = [s for s in g_skills if not _covered(s, bank.skills)]
    metrics_missing = [m for m in g_metrics if not _metric_covered(m, bank.allowed_metrics)]
    return {
        "missing_roles": _match_roles(list(golden.get("work_history", [])), bank.work_history),
        "skills_cov": 1.0 - len(skills_missing) / len(g_skills) if g_skills else 1.0,
        "skills_missing": skills_missing,
        "metrics_cov": 1.0 - len(metrics_missing) / len(g_metrics) if g_metrics else 1.0,
        "metrics_missing": metrics_missing,
        "name_present": bool(bank.contact.name.strip()),
    }


# --------------------------------------------------------------- fixtures

def _fixture_stems() -> list[str]:
    return sorted(p.stem for p in RESUMES_DIR.glob("*.txt"))


def _load_fixture(stem: str) -> tuple[str, dict]:
    text = (RESUMES_DIR / f"{stem}.txt").read_text(encoding="utf-8")
    golden = json.loads((RESUMES_DIR / f"{stem}.golden.json").read_text(encoding="utf-8"))
    return text, golden


# --------------------------------------------------------------- LLM availability

def _ollama_reachable() -> bool:
    try:
        return httpx.get("http://localhost:11434/api/tags", timeout=2.0).status_code == 200
    except Exception:
        return False


# --------------------------------------------------------------- always-run (offline) tests

def test_fixtures_present_and_paired():
    stems = _fixture_stems()
    assert len(stems) >= 3, f"want >=3 résumé fixtures, found {stems}"
    for stem in stems:
        assert (RESUMES_DIR / f"{stem}.golden.json").exists(), f"{stem} missing its golden JSON"


def test_golden_is_self_faithful():
    """Every atom in each golden bank is literally grounded in its résumé text — proves the golden
    files are HONEST (no atom the résumé doesn't state) and the grounder works. If this fails, a
    golden file is wrong, not the model."""
    for stem in _fixture_stems():
        text, golden = _load_fixture(stem)
        viol = faithfulness_violations(FactBank.from_dict(golden), text)
        assert not viol, f"{stem}.golden.json has ungrounded atoms (fix the golden): {viol}"


def test_golden_is_self_complete():
    """Each golden bank is complete against itself — sanity that the completeness scorer matches a
    bank to its own golden (all roles, full coverage)."""
    for stem in _fixture_stems():
        _, golden = _load_fixture(stem)
        rep = completeness_report(FactBank.from_dict(golden), golden)
        assert rep["missing_roles"] == [], f"{stem}: self-match dropped roles {rep['missing_roles']}"
        assert rep["skills_cov"] == 1.0, f"{stem}: self skills_cov {rep['skills_cov']}"
        assert rep["metrics_cov"] == 1.0, f"{stem}: self metrics_cov {rep['metrics_cov']}"
        assert rep["name_present"]


def test_evaluator_flags_fabrication():
    """A bank with invented atoms must be caught (else the harness can't protect the guard)."""
    stem = _fixture_stems()[0]
    text, golden = _load_fixture(stem)
    bank = FactBank.from_dict(golden)
    bank.work_history.append(
        WorkEntry(company="Globex Hyperdyne", title="Chief Wizard", start="2099", end="Present", bullets=[]))
    bank.skills.append("Kubernetes")
    bank.allowed_metrics.append("saved $9000000")
    viol = faithfulness_violations(bank, text)
    joined = " ".join(viol)
    assert "Globex Hyperdyne" in joined
    assert "Kubernetes" in joined
    assert any("9000000" in v for v in viol)


def test_evaluator_flags_dropped_role():
    """Dropping a role must show up as a missing role (the qwen3 trap this harness exists for)."""
    _, golden = _load_fixture("career_changer")
    bank = FactBank.from_dict(golden)
    bank.work_history.pop()
    rep = completeness_report(bank, golden)
    assert rep["missing_roles"], "dropping a role should be reported as missing"


# --------------------------------------------------------------- live eval (opt-in)

@pytest.mark.eval
@pytest.mark.skipif(not _ollama_reachable(), reason="no LLM backend reachable (Ollama down)")
def test_extraction_faithful_and_complete():
    """Run the REAL local LLM on each fixture; assert faithful + complete. Uses your configured
    model (load_settings honors AV3_DATA_DIR). Failure message carries the prompt version so a
    regression points at the exact EXTRACT_FACTBANK revision."""
    settings = load_settings()
    llm = build_default(settings)
    failures: list[str] = []

    for stem in _fixture_stems():
        text, golden = _load_fixture(stem)
        bank = asyncio.run(extract_factbank(text, llm))
        viol = faithfulness_violations(bank, text)
        rep = completeness_report(bank, golden)
        if viol:
            failures.append(f"{stem}: UNFAITHFUL (invented): {viol}")
        if rep["missing_roles"]:
            failures.append(f"{stem}: dropped/merged roles: {rep['missing_roles']}")
        if rep["skills_cov"] < _MIN_SKILLS_COV:
            failures.append(f"{stem}: skills_cov {rep['skills_cov']:.2f} < {_MIN_SKILLS_COV} (missing {rep['skills_missing']})")
        if rep["metrics_cov"] < _MIN_METRICS_COV:
            failures.append(f"{stem}: metrics_cov {rep['metrics_cov']:.2f} < {_MIN_METRICS_COV} (missing {rep['metrics_missing']})")
        if not rep["name_present"]:
            failures.append(f"{stem}: contact.name empty")

    if failures:
        pytest.fail(f"Résumé extraction eval failed (prompt {EXTRACT_FACTBANK.version}):\n"
                    + "\n".join(f"  {f}" for f in failures))
