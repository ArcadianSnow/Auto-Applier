-- Turso (libSQL) schema for the Auto Applier v3 telemetry relay (spec §9).
-- Apply once:  turso db shell auto-applier-telemetry < schema.sql
--
-- One flat table. The scrubbed payload is stored as JSON so the schema never has
-- to track the §9 (a)/(b) field sets — query with json_extract(). user_id is
-- pulled out as a column for cheap GROUP BY during triage. NOTHING here is PII:
-- error messages are scrubbed, the answer value is never present, EEO rows never
-- arrive.

CREATE TABLE IF NOT EXISTS mirror_events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    received_at  TEXT NOT NULL,      -- relay-stamped ISO-8601 UTC
    category     TEXT NOT NULL,      -- 'error' | 'inferred_answer'
    user_id      TEXT NOT NULL,      -- sha256(handle)[:10] pseudonym, or 'anonymous'
    payload_json TEXT NOT NULL       -- the re-scrubbed row
);

CREATE INDEX IF NOT EXISTS ix_mirror_user     ON mirror_events (user_id);
CREATE INDEX IF NOT EXISTS ix_mirror_category ON mirror_events (category);
CREATE INDEX IF NOT EXISTS ix_mirror_received ON mirror_events (received_at);
