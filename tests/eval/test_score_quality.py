"""Scoring eval harness — calibrates v3's LLM scoring against a labeled set
(spec section 10, section 11b Phase 3 (7/M)).

> "Scoring eval harness: ``evals/golden_set.jsonl`` (a dozen job+resume pairs
>  with expected score bands) + a pytest that asserts bands. Run on every
>  prompt/model change so scoring quality is measurable."

This is the **regression detector** for the (2/M) prompt versioning. Without
it, a prompt or model change can silently shift the score distribution and
break threshold semantics (``review_min``, ``auto_apply_min``) without any
test failing. With it, prompt edits get gated on a measurable bar.

How it's wired:
  * Marked ``@pytest.mark.eval`` so it's OFF in the default test run
    (``pyproject.toml`` ``addopts``). Run via ``pytest -m eval`` when you want
    to validate scoring quality after a prompt/model change.
  * **Skip cleanly** when no LLM backend is reachable — this keeps the harness
    runnable in dev environments without Ollama installed. Live LLM is the
    point, but the absence of one mustn't break the suite.
  * Each pair declares its expected band (``low | mid | high``) with
    inclusive ``band_min``/``band_max`` ranges. Asserts the LLM's total lands
    in the expected band. **Per-prompt-version pinning:** the test reads
    ``SCORE_JD.version`` and includes it in the failure message so a band miss
    points at the exact prompt revision that regressed.
  * The canonical profile (a Python data engineer with ETL background) is
    fixed in this file rather than read from a real fact bank — keeps the
    harness reproducible across machines.

What's NOT in this sub-phase:
  * Per-axis assertions (a future enhancement could pin individual dimensions,
    not just the total — but band-level total is the first meaningful gate).
  * Auto-tuning of ``review_min`` / ``auto_apply_min`` from the golden-set
    distribution. The spec defaults (4.0 / 7.0) stay; this harness only
    detects drift, doesn't change config.
  * Statistical pass criteria (e.g. "11/12 must pass") — every pair must
    land in band. Loosen if calibration shows persistent borderline cases.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path

import httpx
import pytest

from auto_applier.config.settings import Settings
from auto_applier.db import init_app_db
from auto_applier.db.repositories import JobRepo, ScoreRepo
from auto_applier.domain.models import Job
from auto_applier.domain.state import JobState
from auto_applier.llm.complete import build_default
from auto_applier.llm.prompts import SCORE_JD
from auto_applier.pipeline.score_worker import ScoreWorker
from auto_applier.resume.factbank import Contact, EducationEntry, FactBank, WorkEntry


GOLDEN_SET_PATH = Path(__file__).parent / "golden_set.jsonl"


# --------------------------------------------------------------- canonical profile

def _profile() -> FactBank:
    """The canonical 'who the candidate is' for the eval. Fixed across pairs
    so the harness measures the LLM's JD-side judgment, not profile drift.

    Shape: senior Python data engineer with ETL / Airflow / dbt / Snowflake
    background, ~7 years experience, $150k+ salary expectation. Chosen so
    the band assignments in ``golden_set.jsonl`` have clear ground truth:
    data-engineering JDs hit HIGH, adjacent-but-not-DE roles hit MID, and
    off-target roles hit LOW.
    """
    return FactBank(
        contact=Contact(
            name="Pat Doe", email="pat@example.com", location="Remote US",
        ),
        skills=[
            "Python", "SQL", "PostgreSQL", "Snowflake",
            "Airflow", "dbt", "Spark", "PySpark",
            "AWS", "S3", "Glue", "Lambda",
            "ETL", "data modeling", "data warehousing", "streaming",
            "Docker", "Git",
        ],
        work_history=[
            WorkEntry(
                company="DataPlatformCo",
                title="Senior Data Engineer",
                start="2022",
                end="Present",
                bullets=[
                    "Owned the central data warehouse on Snowflake; designed and shipped 40+ dbt models",
                    "Led migration of legacy SSIS pipelines to Airflow + Python (12 production DAGs)",
                    "Built PySpark streaming consumer for click-stream events into S3 + Snowflake",
                    "Mentored 2 junior data engineers on data modeling + testing",
                ],
            ),
            WorkEntry(
                company="MidStartup",
                title="Data Engineer",
                start="2019",
                end="2022",
                bullets=[
                    "Built ETL from scratch for the company's first analytics stack (Airflow + PostgreSQL)",
                    "Modeled CRM + product event data into a star schema for Looker",
                    "Maintained Python tooling for 8 internal data consumers",
                ],
            ),
            WorkEntry(
                company="EarlyCareer",
                title="Software Engineer",
                start="2017",
                end="2019",
                bullets=[
                    "Backend Python services on PostgreSQL",
                    "Migrated internal reporting from CSV exports to a real dashboard pipeline",
                ],
            ),
        ],
        education=[
            EducationEntry(
                institution="State University",
                degree="B.S. Computer Science",
                start="2013",
                end="2017",
            ),
        ],
        certifications=["AWS Certified Solutions Architect - Associate"],
        allowed_metrics=[
            "40+ dbt models", "12 production DAGs", "8 internal data consumers",
            "2 junior data engineers",
        ],
        work_authorization="US citizen",
        requires_sponsorship=False,
    )


# --------------------------------------------------------------- golden set

def _load_golden_set() -> list[dict]:
    """Load + validate the labeled set. Each line is one labeled JD."""
    rows: list[dict] = []
    for line in GOLDEN_SET_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        row = json.loads(line)
        # Shape check — fail loudly here rather than mid-LLM if a field is missing.
        for field in ("id", "expected_band", "band_min", "band_max",
                      "title", "company", "description"):
            assert field in row, f"golden set row missing {field!r}: {row}"
        assert row["expected_band"] in {"low", "mid", "high"}
        assert 0.0 <= row["band_min"] <= row["band_max"] <= 10.0
        rows.append(row)
    return rows


# --------------------------------------------------------------- LLM availability

def _ollama_reachable() -> bool:
    """Quick probe: is Ollama up locally? Two-second timeout; failure means
    the eval is skipped rather than failed. We don't require a specific model
    here — the worker's defaults handle that."""
    try:
        resp = httpx.get("http://localhost:11434/api/tags", timeout=2.0)
        return resp.status_code == 200
    except Exception:
        return False


def _llm_available() -> bool:
    # Ollama is the only model tier (the Gemini cloud fallback was removed).
    return _ollama_reachable()


# --------------------------------------------------------------- the harness

@pytest.mark.eval
@pytest.mark.skipif(
    not _llm_available(),
    reason="no LLM backend reachable (Ollama down)",
)
def test_golden_set_score_bands(tmp_path):
    """Replay every golden-set pair through ScoreWorker; assert each total
    lands in the expected band.

    Per-pair failure message includes the prompt version so a band miss
    points at the exact ``SCORE_JD.version`` that regressed. Re-pin the
    expectations in ``golden_set.jsonl`` when a prompt change is intentional
    and known-good.
    """
    rows = _load_golden_set()
    assert len(rows) >= 8, "golden set should have at least 8 pairs to be meaningful"

    # Settings + DB + LLM all real (this is a live-LLM test by contract).
    settings = Settings(data_dir=tmp_path)
    conn = init_app_db(settings.app_db_path)
    try:
        llm = build_default(settings)
        worker = ScoreWorker(
            settings=settings,
            conn=conn,
            fact_bank=_profile(),
            llm_client=llm,
        )

        # Seed every JD as a DESCRIBED job so one run_once scores all of them.
        job_repo = JobRepo(conn)
        seeded: dict[str, str] = {}  # golden id -> job.id
        for row in rows:
            job = Job(
                source="eval",
                source_job_id=row["id"],
                title=row["title"],
                company=row["company"],
                description=row["description"],
            )
            job_repo.add(job)
            job_repo.set_state(job.id, JobState.DESCRIBED)
            seeded[row["id"]] = job.id

        # Score them all in one pass.
        summary = asyncio.run(worker.run_once())
        assert summary.attempted == len(rows)

        # Collect per-pair results + assert bands.
        misses: list[str] = []
        score_repo = ScoreRepo(conn)
        for row in rows:
            score = score_repo.get(seeded[row["id"]])
            assert score is not None, f"{row['id']}: missing JobScore row"
            assert SCORE_JD.version in (score.model or ""), (
                f"{row['id']}: model tag {score.model!r} missing prompt version "
                f"{SCORE_JD.version!r} — regression in version stamping"
            )
            if not (row["band_min"] <= score.total <= row["band_max"]):
                misses.append(
                    f"  {row['id']} (expected {row['expected_band']} "
                    f"[{row['band_min']:.1f}-{row['band_max']:.1f}]): "
                    f"got {score.total:.2f}"
                )
        if misses:
            pytest.fail(
                f"Score bands missed for {len(misses)}/{len(rows)} pairs "
                f"(prompt version {SCORE_JD.version}):\n" + "\n".join(misses)
            )
    finally:
        conn.close()


# --------------------------------------------------------------- stub-mode (always-runs)

def test_golden_set_loads_and_validates():
    """The golden set file itself parses + validates regardless of LLM
    availability. Runs in the default test suite (no ``@pytest.mark.eval``)
    so a broken golden set surfaces on every PR, not only when someone
    runs the eval explicitly.
    """
    rows = _load_golden_set()
    assert len(rows) >= 8
    # Sanity: at least one of each band represented.
    bands = {row["expected_band"] for row in rows}
    assert bands == {"low", "mid", "high"}, (
        f"golden set should cover all three bands, got {bands}"
    )
    # No duplicate ids.
    ids = [row["id"] for row in rows]
    assert len(ids) == len(set(ids)), f"duplicate ids in golden set: {ids}"
