"""Cover-letter autogen — BUILD 5 (``av3 cover --generate[-all]``).

Covers the offline core (no LLM wire, no browser): the .docx renderer, single-job
generate_one (happy / no-clobber / force / empty-JD / guard-fail / LLM-error), the batch
backfill (score floor, DECIDED-only, only-missing, limit), and the no-AI-tells voice
contract on the prompt. Same fake-the-client pattern as test_optimize_worker.py.
"""

from __future__ import annotations

import asyncio
import sqlite3

from docx import Document

from auto_applier.config.settings import Settings
from auto_applier.db.repositories import JobRepo, ScoreRepo
from auto_applier.domain.models import Job, JobScore
from auto_applier.domain.state import JobState
from auto_applier.llm.prompts import GENERATE_COVER_LETTER
from auto_applier.resume.cover_autogen import (
    ERROR,
    GENERATED,
    SKIPPED_EXISTING,
    SKIPPED_GUARD,
    SKIPPED_NO_DESCRIPTION,
    _strip_ai_tells,
    backfill,
    generate_one,
    render_cover_letter_docx,
)
from auto_applier.resume.factbank import Contact, FactBank, WorkEntry
from auto_applier.resume.generate import (
    CoverLetterGenerator,
    existing_job_cover,
    job_cover_upload_path,
)

_JD = "We need a SQL Server DBA who is strong in Python and owns ETL pipelines."

# A letter that only claims bank-supported tech (SQL Server, Python) → guard PASS.
_HAPPY_BODY = (
    "I spent five years as a Database Administrator at Acme, working day to day in "
    "SQL Server and Python.\n\n"
    "My work refactoring the billing database lines up with what your team needs.\n\n"
    "I would be glad to talk it through."
)
# Claims Kubernetes/Terraform — neither in the bank → guard flags → not written.
_FABRICATED_BODY = (
    "I have deep Kubernetes and Terraform expertise running production clusters."
)


# --------------------------------------------------------------- fakes

class _CoverLLM:
    """Minimal CompletionClient: returns a fixed cover body (or raises). Counts calls so
    the no-clobber / empty-JD short-circuits can be proven to skip the LLM."""

    def __init__(self, body: str = _HAPPY_BODY, *, raise_exc: Exception | None = None):
        self._body = body
        self._raise = raise_exc
        self.calls = 0

    async def complete_json(self, prompt: str, *, system: str = "") -> dict:
        self.calls += 1
        if self._raise is not None:
            raise self._raise
        return {"body": self._body}


def _bank() -> FactBank:
    return FactBank(
        contact=Contact(
            name="Joseph Lira", email="jl@example.com",
            phone="+1 555 0100", location="Dallas, TX",
        ),
        skills=["sql", "sql server", "python", "etl"],
        work_history=[
            WorkEntry(
                company="Acme", title="Database Administrator",
                start="2019", end="2024",
                bullets=["Refactored the billing database", "Owned SQL Server backups"],
            )
        ],
        allowed_metrics=[],
    )


def _job(jid: str = "j1", *, description: str = _JD,
         company: str = "BetaCo", title: str = "Data Platform Engineer") -> Job:
    return Job(
        id=jid, source="greenhouse", source_job_id=jid, title=title,
        company=company, description=description, state=JobState.DECIDED,
    )


def _gen_one(settings: Settings, job: Job, llm: _CoverLLM, *, name: str = "Joseph Lira",
             force: bool = False):
    return asyncio.run(generate_one(
        settings, job, bank=_bank(), generator=CoverLetterGenerator(llm),
        name=name, force=force,
    ))


def _seed_scored(conn: sqlite3.Connection, *, sid: str, total: float,
                 state: JobState = JobState.DECIDED, description: str = _JD,
                 company: str = "BetaCo", title: str = "Data Platform Engineer") -> str:
    repo = JobRepo(conn)
    job = Job(source="greenhouse", source_job_id=sid, title=title,
              company=company, description=description)
    repo.add(job)
    repo.set_state(job.id, JobState.DESCRIBED)
    repo.set_state(job.id, JobState.SCORED)
    repo.set_state(job.id, JobState.DECIDED)
    if state is JobState.REVIEW:
        repo.set_state(job.id, JobState.REVIEW)
    elif state is JobState.SKIPPED:
        repo.set_state(job.id, JobState.SKIPPED)
    ScoreRepo(conn).upsert(JobScore(job_id=job.id, total=total))
    return job.id


# --------------------------------------------------------------- .docx render

def test_render_docx_is_a_complete_letter(settings, tmp_path):
    out = tmp_path / "letter.docx"
    render_cover_letter_docx(_HAPPY_BODY, _bank().contact, out)
    assert out.exists()

    doc = Document(str(out))
    texts = [p.text for p in doc.paragraphs]
    joined = "\n".join(texts)
    assert "Dear Hiring Manager," in texts
    assert "Sincerely," in texts
    assert texts.count("Joseph Lira") == 2  # header + signature
    assert "refactoring the billing database" in joined
    assert "jl@example.com | +1 555 0100 | Dallas, TX" in texts
    # author metadata is the applicant's name, not the python-docx default tell
    assert doc.core_properties.author == "Joseph Lira"


def test_render_docx_no_contact_still_renders(settings, tmp_path):
    out = tmp_path / "bare.docx"
    render_cover_letter_docx(_HAPPY_BODY, Contact(), out)
    texts = [p.text for p in Document(str(out)).paragraphs]
    assert "Dear Hiring Manager," in texts
    assert "Sincerely," in texts


# --------------------------------------------------------------- generate_one

def test_generate_one_happy_writes_docx(settings):
    llm = _CoverLLM()
    res = _gen_one(settings, _job("happy"), llm)

    assert res.status == GENERATED and res.ok
    assert llm.calls == 1
    dest = job_cover_upload_path(settings, "happy", ".docx", "Joseph Lira")
    assert dest.exists() and res.path == str(dest)
    assert dest.name == "Joseph Lira Cover Letter.docx"
    # the apply path / av3 cover would now find it
    assert existing_job_cover(settings, "happy") == dest


def test_generate_one_no_clobber_skips_and_skips_llm(settings):
    # a hand-authored letter is already assigned
    manual = job_cover_upload_path(settings, "manual", ".docx", "")
    manual.parent.mkdir(parents=True, exist_ok=True)
    manual.write_text("HAND WRITTEN", encoding="utf-8")

    llm = _CoverLLM()
    res = _gen_one(settings, _job("manual"), llm)

    assert res.status == SKIPPED_EXISTING
    assert llm.calls == 0  # never spent an LLM call
    assert manual.read_text(encoding="utf-8") == "HAND WRITTEN"  # untouched


def test_generate_one_force_overwrites_single_file(settings):
    manual = job_cover_upload_path(settings, "f", ".docx", "")
    manual.parent.mkdir(parents=True, exist_ok=True)
    manual.write_text("OLD", encoding="utf-8")

    res = _gen_one(settings, _job("f"), _CoverLLM(), force=True)
    assert res.status == GENERATED

    folder = manual.parent
    covers = sorted(p.name for p in folder.glob("*Cover Letter.*"))
    assert covers == ["Joseph Lira Cover Letter.docx"]  # exactly one, the new one
    # and it's a real docx now, not the OLD text
    assert "Dear Hiring Manager," in [p.text for p in Document(str(folder / covers[0])).paragraphs]


def test_generate_one_empty_description_skips(settings):
    llm = _CoverLLM()
    res = _gen_one(settings, _job("nodesc", description="   "), llm)
    assert res.status == SKIPPED_NO_DESCRIPTION
    assert llm.calls == 0


def test_generate_one_guard_fail_writes_nothing(settings):
    res = _gen_one(settings, _job("guard"), _CoverLLM(body=_FABRICATED_BODY))
    assert res.status == SKIPPED_GUARD
    assert "kubernetes" in res.detail.lower() or "terraform" in res.detail.lower()
    assert existing_job_cover(settings, "guard") is None  # nothing shipped


def test_generate_one_llm_error_is_isolated(settings):
    res = _gen_one(settings, _job("err"), _CoverLLM(raise_exc=RuntimeError("ollama down")))
    assert res.status == ERROR
    assert "ollama down" in res.detail
    assert existing_job_cover(settings, "err") is None


# --------------------------------------------------------------- dash strip (the #1 AI tell)

def test_strip_ai_tells_replaces_dashes():
    assert _strip_ai_tells("I did A—then B.") == "I did A, then B."
    assert _strip_ai_tells("word–word") == "word, word"
    assert "—" not in _strip_ai_tells("a — b — c")
    # paragraph breaks survive the strip (newline-safe)
    assert "\n\n" in _strip_ai_tells("Para one—done.\n\nPara two.")


def test_generate_one_strips_em_dash_even_if_llm_emits_one(settings):
    body = "I ran SQL Server—and Python—at Acme.\n\nGlad to talk."
    res = _gen_one(settings, _job("dash"), _CoverLLM(body=body))
    assert res.status == GENERATED
    txt = "\n".join(p.text for p in Document(res.path).paragraphs)
    assert "—" not in txt and "–" not in txt


# --------------------------------------------------------------- backfill

def test_backfill_only_strong_decided_jobs(settings, conn):
    strong = _seed_scored(conn, sid="strong", total=9.0)
    mid = _seed_scored(conn, sid="mid", total=8.2)
    weak = _seed_scored(conn, sid="weak", total=6.0)  # below floor — never touched

    results = asyncio.run(backfill(
        settings, conn, llm=_CoverLLM(), bank=_bank(), min_score=8.0, name="Joseph Lira",
    ))

    gen_ids = {r.job_id for r in results if r.status == GENERATED}
    assert gen_ids == {strong, mid}
    assert existing_job_cover(settings, strong) is not None
    assert existing_job_cover(settings, mid) is not None
    assert existing_job_cover(settings, weak) is None
    assert all(r.job_id != weak for r in results)


def test_backfill_skips_existing_and_respects_state(settings, conn):
    has_letter = _seed_scored(conn, sid="has", total=9.5)
    review = _seed_scored(conn, sid="rev", total=9.0, state=JobState.REVIEW)  # not DECIDED
    fresh = _seed_scored(conn, sid="fresh", total=8.5)

    # pre-assign a manual letter to `has`
    p = job_cover_upload_path(settings, has_letter, ".docx", "")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("MANUAL", encoding="utf-8")

    results = asyncio.run(backfill(
        settings, conn, llm=_CoverLLM(), bank=_bank(), min_score=8.0, name="Joseph Lira",
    ))
    by_id = {r.job_id: r.status for r in results}

    assert by_id[has_letter] == SKIPPED_EXISTING
    assert p.read_text(encoding="utf-8") == "MANUAL"  # never clobbered
    assert by_id[fresh] == GENERATED
    assert review not in by_id  # REVIEW state filtered out (DECIDED-only)


def test_backfill_limit_caps_new_letters(settings, conn):
    _seed_scored(conn, sid="a", total=9.9)
    _seed_scored(conn, sid="b", total=9.8)
    _seed_scored(conn, sid="c", total=9.7)

    results = asyncio.run(backfill(
        settings, conn, llm=_CoverLLM(), bank=_bank(), min_score=8.0, limit=1,
    ))
    assert len([r for r in results if r.status == GENERATED]) == 1


def test_backfill_empty_when_nothing_qualifies(settings, conn):
    _seed_scored(conn, sid="low", total=5.0)
    results = asyncio.run(backfill(settings, conn, llm=_CoverLLM(), bank=_bank(), min_score=8.0))
    assert results == []


# --------------------------------------------------------------- voice contract

def test_cover_prompt_enforces_no_ai_tells_voice():
    assert GENERATE_COVER_LETTER.version == "gen-cover-v2"
    sys = GENERATE_COVER_LETTER.system.lower()
    assert "em-dash" in sys
    assert "excited" in sys and "thrilled" in sys      # categorical "excited" family ban
    assert "passionate" in sys                          # a banned buzzword is named
    assert "rule of three" in sys
    # anti-overclaim: the prompt must forbid inventing soft experience the bank lacks
    assert "overclaim" in sys or "do not claim" in sys
