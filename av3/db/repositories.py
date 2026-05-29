"""Repositories: typed row ↔ dataclass mapping over the app DB (spec §4).

Every job state change routes through ``domain.state.transition`` so the allowed-
transitions table is the single source of truth. Dedup is a query on ``state`` — the
Python-side join workarounds from v2 are gone (spec §4 "Drop").
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timezone

from av3.domain.models import (
    Answer,
    Application,
    Job,
    JobScore,
    SkillGap,
    utcnow_iso,
)
from av3.domain.state import (
    ApplicationStatus,
    ApplyMode,
    JobState,
    transition,
)


# --------------------------------------------------------------------------- jobs
class JobRepo:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    @staticmethod
    def _row_to_job(row: sqlite3.Row) -> Job:
        return Job(
            id=row["id"],
            source=row["source"],
            source_job_id=row["source_job_id"],
            canonical_hash=row["canonical_hash"] or "",
            title=row["title"],
            company=row["company"],
            location=row["location"] or "",
            url=row["url"] or "",
            description=row["description"] or "",
            compensation=row["compensation"] or "",
            posted_at=row["posted_at"] or "",
            ghost_score=row["ghost_score"],
            state=JobState(row["state"]),
            discovered_at=row["discovered_at"],
            updated_at=row["updated_at"],
        )

    def add(self, job: Job) -> Job:
        self.conn.execute(
            """INSERT INTO jobs (id, source, source_job_id, canonical_hash, title,
                   company, location, url, description, compensation, posted_at,
                   ghost_score, state, discovered_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                job.id, job.source, job.source_job_id, job.canonical_hash, job.title,
                job.company, job.location, job.url, job.description, job.compensation,
                job.posted_at, job.ghost_score, job.state.value, job.discovered_at,
                job.updated_at,
            ),
        )
        return job

    def upsert_discovered(self, job: Job) -> Job:
        """Insert a freshly discovered job, or return the existing row if this
        (source, source_job_id) was already discovered. Discovery is idempotent —
        re-running a cycle must not create duplicates (spec §7)."""
        existing = self.get_by_source(job.source, job.source_job_id)
        if existing is not None:
            return existing
        return self.add(job)

    def get(self, job_id: str) -> Job | None:
        row = self.conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return self._row_to_job(row) if row else None

    def get_by_source(self, source: str, source_job_id: str) -> Job | None:
        row = self.conn.execute(
            "SELECT * FROM jobs WHERE source = ? AND source_job_id = ?",
            (source, source_job_id),
        ).fetchone()
        return self._row_to_job(row) if row else None

    def list_by_state(self, state: JobState, limit: int | None = None) -> list[Job]:
        sql = "SELECT * FROM jobs WHERE state = ? ORDER BY discovered_at"
        params: tuple = (state.value,)
        if limit is not None:
            sql += " LIMIT ?"
            params += (limit,)
        return [self._row_to_job(r) for r in self.conn.execute(sql, params)]

    def set_state(self, job_id: str, new_state: JobState) -> Job:
        """Validated state change. Raises InvalidTransition on a disallowed move."""
        job = self.get(job_id)
        if job is None:
            raise KeyError(f"job {job_id} not found")
        transition(job.state, new_state)  # raises if not allowed
        now = utcnow_iso()
        self.conn.execute(
            "UPDATE jobs SET state = ?, updated_at = ? WHERE id = ?",
            (new_state.value, now, job_id),
        )
        job.state = new_state
        job.updated_at = now
        return job

    def update_fields(self, job_id: str, **fields) -> None:
        """Update non-state columns (e.g. description after a DESCRIBE, ghost_score).

        State must go through :meth:`set_state`; reject it here to keep one chokepoint.
        """
        if "state" in fields:
            raise ValueError("use set_state() for state changes")
        if not fields:
            return
        fields["updated_at"] = utcnow_iso()
        cols = ", ".join(f"{k} = ?" for k in fields)
        self.conn.execute(
            f"UPDATE jobs SET {cols} WHERE id = ?", (*fields.values(), job_id)
        )

    # --- dedup + rate-limit queries (spec §4, §7) ---------------------------
    def applied_canonical_hashes(self) -> set[str]:
        """Canonical hashes of jobs that reached APPLIED — the dedup source of truth.

        Only APPLIED counts, so an UNCONFIRMED/FAILED attempt is safely retryable and
        success never inflates (spec §5)."""
        rows = self.conn.execute(
            "SELECT DISTINCT canonical_hash FROM jobs "
            "WHERE state = ? AND canonical_hash IS NOT NULL AND canonical_hash != ''",
            (JobState.APPLIED.value,),
        )
        return {r["canonical_hash"] for r in rows}

    def company_applied_count(self, company: str, on_day: date | None = None) -> int:
        """How many jobs at ``company`` reached APPLIED on ``on_day`` (default UTC today).

        Backs the per-company/day rate limit so we never look spammy to one employer
        (spec §7). Uses the job's updated_at date as the apply date proxy. The default
        day is UTC to match the UTC timestamps we store (a local ``today`` would miss
        applies across the UTC/local midnight boundary)."""
        on_day = on_day or datetime.now(timezone.utc).date()
        row = self.conn.execute(
            "SELECT COUNT(*) AS n FROM jobs "
            "WHERE company = ? AND state = ? AND substr(updated_at, 1, 10) = ?",
            (company, JobState.APPLIED.value, on_day.isoformat()),
        ).fetchone()
        return row["n"]

    def count_by_state(self) -> dict[str, int]:
        rows = self.conn.execute("SELECT state, COUNT(*) AS n FROM jobs GROUP BY state")
        return {r["state"]: r["n"] for r in rows}


# ------------------------------------------------------------------------- scores
class ScoreRepo:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def upsert(self, score: JobScore) -> JobScore:
        self.conn.execute(
            """INSERT INTO job_scores (job_id, total, dimensions_json, model, scored_at)
               VALUES (?,?,?,?,?)
               ON CONFLICT(job_id) DO UPDATE SET
                   total=excluded.total, dimensions_json=excluded.dimensions_json,
                   model=excluded.model, scored_at=excluded.scored_at""",
            (
                score.job_id, score.total, json.dumps(score.dimensions),
                score.model, score.scored_at,
            ),
        )
        return score

    def get(self, job_id: str) -> JobScore | None:
        row = self.conn.execute(
            "SELECT * FROM job_scores WHERE job_id = ?", (job_id,)
        ).fetchone()
        if not row:
            return None
        return JobScore(
            job_id=row["job_id"],
            total=row["total"],
            dimensions=json.loads(row["dimensions_json"]) if row["dimensions_json"] else {},
            model=row["model"] or "",
            scored_at=row["scored_at"],
        )


# ------------------------------------------------------------------- applications
class ApplicationRepo:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    @staticmethod
    def _row(row: sqlite3.Row) -> Application:
        return Application(
            id=row["id"],
            job_id=row["job_id"],
            mode=ApplyMode(row["mode"]),
            status=ApplicationStatus(row["status"]),
            cover_letter_path=row["cover_letter_path"] or "",
            generated_resume_path=row["generated_resume_path"] or "",
            submitted_at=row["submitted_at"] or "",
        )

    def add(self, app: Application) -> Application:
        self.conn.execute(
            """INSERT INTO applications (id, job_id, mode, status, cover_letter_path,
                   generated_resume_path, submitted_at)
               VALUES (?,?,?,?,?,?,?)""",
            (
                app.id, app.job_id, app.mode.value, app.status.value,
                app.cover_letter_path, app.generated_resume_path, app.submitted_at,
            ),
        )
        return app

    def get(self, app_id: str) -> Application | None:
        row = self.conn.execute(
            "SELECT * FROM applications WHERE id = ?", (app_id,)
        ).fetchone()
        return self._row(row) if row else None

    def list_by_job(self, job_id: str) -> list[Application]:
        return [
            self._row(r)
            for r in self.conn.execute(
                "SELECT * FROM applications WHERE job_id = ? ORDER BY submitted_at",
                (job_id,),
            )
        ]

    def list_recent(self, limit: int = 50) -> list[Application]:
        """Most-recent applications first — backs the dashboard's history panel.

        Ordering is by ``submitted_at DESC`` with the row id as a tiebreaker
        (so two attempts in the same second still come out in insert order).
        Empty ``submitted_at`` is fine — it sorts before any real timestamp,
        which means in-flight/assisted-pending rows surface at the top of
        history when the user hasn't actually submitted them yet.
        """
        rows = self.conn.execute(
            "SELECT * FROM applications "
            "ORDER BY submitted_at DESC, id DESC LIMIT ?",
            (limit,),
        )
        return [self._row(r) for r in rows]

    def set_status(
        self, app_id: str, status: ApplicationStatus, submitted_at: str | None = None
    ) -> None:
        if submitted_at is not None:
            self.conn.execute(
                "UPDATE applications SET status = ?, submitted_at = ? WHERE id = ?",
                (status.value, submitted_at, app_id),
            )
        else:
            self.conn.execute(
                "UPDATE applications SET status = ? WHERE id = ?",
                (status.value, app_id),
            )


# -------------------------------------------------------------------- skill gaps
class SkillGapRepo:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def bump(self, skill: str) -> SkillGap:
        """Increment the gap count (or create it). Recurrence ≥ N drives the passive
        fact-bank proposal (spec §7b)."""
        now = utcnow_iso()
        self.conn.execute(
            """INSERT INTO skill_gaps (skill, count, first_seen, last_seen, status)
               VALUES (?, 1, ?, ?, 'open')
               ON CONFLICT(skill) DO UPDATE SET
                   count = count + 1, last_seen = excluded.last_seen""",
            (skill, now, now),
        )
        return self.get(skill)  # type: ignore[return-value]

    def get(self, skill: str) -> SkillGap | None:
        row = self.conn.execute(
            "SELECT * FROM skill_gaps WHERE skill = ?", (skill,)
        ).fetchone()
        if not row:
            return None
        return SkillGap(
            skill=row["skill"],
            count=row["count"],
            first_seen=row["first_seen"],
            last_seen=row["last_seen"],
            status=row["status"],
        )

    def list_open(self, min_count: int = 1) -> list[SkillGap]:
        rows = self.conn.execute(
            "SELECT * FROM skill_gaps WHERE status = 'open' AND count >= ? "
            "ORDER BY count DESC",
            (min_count,),
        )
        return [
            SkillGap(
                skill=r["skill"], count=r["count"], first_seen=r["first_seen"],
                last_seen=r["last_seen"], status=r["status"],
            )
            for r in rows
        ]


# ----------------------------------------------------------------------- answers
class AnswerRepo:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def upsert(self, ans: Answer) -> Answer:
        self.conn.execute(
            """INSERT INTO answers (question, answer, source, embedding, updated_at)
               VALUES (?,?,?,?,?)
               ON CONFLICT(question) DO UPDATE SET
                   answer=excluded.answer, source=excluded.source,
                   embedding=excluded.embedding, updated_at=excluded.updated_at""",
            (ans.question, ans.answer, ans.source, ans.embedding, ans.updated_at),
        )
        return ans

    def get(self, question: str) -> Answer | None:
        row = self.conn.execute(
            "SELECT * FROM answers WHERE question = ?", (question,)
        ).fetchone()
        if not row:
            return None
        return Answer(
            question=row["question"], answer=row["answer"], source=row["source"],
            embedding=row["embedding"], updated_at=row["updated_at"],
        )

    def all(self) -> list[Answer]:
        return [
            Answer(
                question=r["question"], answer=r["answer"], source=r["source"],
                embedding=r["embedding"], updated_at=r["updated_at"],
            )
            for r in self.conn.execute("SELECT * FROM answers ORDER BY question")
        ]
