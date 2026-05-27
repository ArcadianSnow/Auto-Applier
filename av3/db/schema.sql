-- Auto Applier v3 — main application database (app.db). Spec §4.
-- The `events` observability table lives in a SEPARATE events.db (telemetry/sink.py),
-- so it can be pruned/rotated independently and never contends with app writes.
--
-- Conventions: TEXT primary keys are internal uuids; timestamps are ISO-8601 TEXT (UTC).

CREATE TABLE IF NOT EXISTS jobs (
    id              TEXT PRIMARY KEY,           -- internal uuid
    source          TEXT NOT NULL,              -- 'greenhouse' | 'lever' | ...
    source_job_id   TEXT NOT NULL,
    canonical_hash  TEXT,                       -- normalize_title+company (cross-source dedup)
    title           TEXT NOT NULL,
    company         TEXT NOT NULL,
    location        TEXT,
    url             TEXT,
    description     TEXT,
    compensation    TEXT,
    posted_at       TEXT,
    ghost_score     REAL,
    state           TEXT NOT NULL,              -- see domain/state.py JobState
    discovered_at   TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    UNIQUE (source, source_job_id)
);

CREATE INDEX IF NOT EXISTS ix_jobs_state           ON jobs (state);
CREATE INDEX IF NOT EXISTS ix_jobs_canonical_hash  ON jobs (canonical_hash);
CREATE INDEX IF NOT EXISTS ix_jobs_company         ON jobs (company);

-- One score per job: JD scored against the master profile (spec §6b — no multi-résumé).
CREATE TABLE IF NOT EXISTS job_scores (
    job_id          TEXT PRIMARY KEY,
    total           REAL NOT NULL,
    dimensions_json TEXT,                       -- {"skills":.., "experience":.., ...}
    model           TEXT,
    scored_at       TEXT NOT NULL,
    FOREIGN KEY (job_id) REFERENCES jobs (id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS applications (
    id                    TEXT PRIMARY KEY,
    job_id                TEXT NOT NULL,
    mode                  TEXT NOT NULL,        -- ApplyMode
    status                TEXT NOT NULL,        -- ApplicationStatus
    cover_letter_path     TEXT,
    generated_resume_path TEXT,                 -- per-job résumé from the fact bank (§6b)
    submitted_at          TEXT,
    FOREIGN KEY (job_id) REFERENCES jobs (id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS ix_applications_job_id ON applications (job_id);
CREATE INDEX IF NOT EXISTS ix_applications_status ON applications (status);

CREATE TABLE IF NOT EXISTS skill_gaps (
    skill       TEXT PRIMARY KEY,
    count       INTEGER NOT NULL DEFAULT 1,
    first_seen  TEXT NOT NULL,
    last_seen   TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'open'    -- open | learning | certified | dismissed
);

-- Known form-question answers; UPSERT-keyed by question. embedding for semantic match (§8b).
CREATE TABLE IF NOT EXISTS answers (
    question    TEXT PRIMARY KEY,
    answer      TEXT NOT NULL,
    source      TEXT NOT NULL DEFAULT 'user',   -- user | inferred | default
    embedding   BLOB,
    updated_at  TEXT NOT NULL
);
