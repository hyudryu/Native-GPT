-- 0007_voices: registry of cloned-voice reference clips.
-- The raw clip and extracted speaker embedding live on the bridge host; this
-- table stores only metadata. voice_id is the bridge-side identifier.

CREATE TABLE voices (
    id            TEXT PRIMARY KEY,        -- UUIDv7; matches bridge voice_id
    name          TEXT NOT NULL,           -- user/agent-assigned label
    host_id       TEXT NOT NULL REFERENCES remote_hosts(id) ON DELETE CASCADE,
    source_kind   TEXT NOT NULL,           -- file | url
    source_ref    TEXT,                    -- local file path or original URL (sanitized)
    duration_ms   INTEGER,                 -- reference clip duration
    created_at    TEXT NOT NULL,
    last_used_at  TEXT
);

CREATE INDEX idx_voices_host ON voices(host_id);
