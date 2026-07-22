-- 0006_generated_assets: outputs produced by remote workloads (images, video, audio).
-- Bytes live on disk under app-data/assets/; this table holds metadata only.

CREATE TABLE generated_assets (
    id            TEXT PRIMARY KEY,
    host_id       TEXT NOT NULL REFERENCES remote_hosts(id) ON DELETE CASCADE,
    workload      TEXT NOT NULL,          -- comfyui | openvoice
    kind          TEXT NOT NULL,          -- image | video | audio
    message_id    TEXT REFERENCES messages(id) ON DELETE SET NULL,
    prompt_text   TEXT,                   -- sanitized request text
    source_ref    TEXT,                   -- workflow id / voice_id
    storage_path  TEXT NOT NULL,          -- relative to app-data/assets/
    bytes         INTEGER,
    mime_type     TEXT,
    created_at    TEXT NOT NULL
);

CREATE INDEX idx_assets_message ON generated_assets(message_id);
CREATE INDEX idx_assets_host ON generated_assets(host_id);
