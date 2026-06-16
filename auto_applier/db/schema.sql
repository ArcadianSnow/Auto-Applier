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

-- Recorded application outcomes (spec §8e outcome feedback loop). One job can accrue
-- several outcomes over time (response → interview → offer/rejection); we keep them all
-- and let analytics derive the furthest-reached stage. ``kind`` is an OutcomeKind value.
CREATE TABLE IF NOT EXISTS outcomes (
    id        TEXT PRIMARY KEY,
    job_id    TEXT NOT NULL,
    kind      TEXT NOT NULL,                  -- response | interview | offer | rejection | ghost
    noted_at  TEXT NOT NULL,
    note      TEXT,
    FOREIGN KEY (job_id) REFERENCES jobs (id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS ix_outcomes_job_id ON outcomes (job_id);
CREATE INDEX IF NOT EXISTS ix_outcomes_kind   ON outcomes (kind);

-- Known form-question answers; UPSERT-keyed by question. embedding for semantic match (§8b).
CREATE TABLE IF NOT EXISTS answers (
    question    TEXT PRIMARY KEY,
    answer      TEXT NOT NULL,
    source      TEXT NOT NULL DEFAULT 'user',   -- user | inferred | default
    embedding   BLOB,
    updated_at  TEXT NOT NULL
);

-- Inbox-side idempotency for the email outcome loop (email-outcome-loop Phase B,
-- research/future-directions.md Direction 4). One row per processed message_id so a
-- re-run never records a duplicate outcome. ``action`` is the routing decision:
--   outcome  -> a confident match+class produced an OutcomeRepo row (matched_job_id set)
--   review   -> ambiguous / no confident match -> surfaced to the human (no outcome row)
--   ignored  -> not a job-status email (newsletter / pure security-code) -> dropped
-- The outcome itself lives in the `outcomes` table (the existing analytics path); this
-- table records only that the message was seen and how it was routed.
CREATE TABLE IF NOT EXISTS inbox_messages (
    message_id      TEXT PRIMARY KEY,
    matched_job_id  TEXT,                       -- NULL when no job matched
    kind            TEXT,                       -- OutcomeKind value, or NULL (ignored/no-kind)
    action          TEXT NOT NULL,              -- 'outcome' | 'review' | 'ignored'
    noted_at        TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_inbox_messages_action ON inbox_messages (action);

-- Per-folder IMAP fetch cursor (last seen UID) so a live fetch (Phase C) only pulls new
-- mail. Kept tiny + separate from message dedup; offline `--eml` runs never touch it.
CREATE TABLE IF NOT EXISTS inbox_state (
    folder    TEXT PRIMARY KEY,
    last_uid  INTEGER NOT NULL
);
