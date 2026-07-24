-- 0009_browser: Native GPT Browser (ADR-0009, spec §13).
-- Persistent state for the optional embedded Chromium browser: profiles,
-- per-profile preferences, Page Agent task audit trail, permission grants,
-- and download metadata. Never stores cookies, passwords, raw local storage,
-- provider API keys, or Page Agent prompts.

CREATE TABLE browser_profiles (
    id              TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    engine          TEXT NOT NULL DEFAULT 'bundled_chromium',
    executable_path TEXT,                -- system browser override; NULL = bundled
    profile_path    TEXT NOT NULL,       -- resolved lazily when env-dependent
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    last_used_at    TEXT
);

CREATE TABLE browser_preferences (
    profile_id               TEXT PRIMARY KEY REFERENCES browser_profiles(id) ON DELETE CASCADE,
    panel_mode               TEXT NOT NULL DEFAULT 'hidden',
    panel_width              INTEGER NOT NULL DEFAULT 640,
    previous_panel_width     INTEGER,
    auto_open_on_tool_call   INTEGER NOT NULL DEFAULT 1,
    keep_running_when_hidden INTEGER NOT NULL DEFAULT 1,
    remote_streaming_enabled INTEGER NOT NULL DEFAULT 0,
    model_mode               TEXT NOT NULL DEFAULT 'follow_conversation',
    model_endpoint_id        TEXT,
    model_id                 TEXT
);

CREATE TABLE browser_tasks (
    id              TEXT PRIMARY KEY,
    profile_id      TEXT NOT NULL REFERENCES browser_profiles(id),
    conversation_id TEXT,
    run_id          TEXT,
    tool_call_id    TEXT,
    task_text       TEXT NOT NULL,
    initial_url     TEXT,
    final_url       TEXT,
    status          TEXT NOT NULL,       -- awaiting_approval|starting|running|paused_for_user|stopping|completed|failed|cancelled
    result_text     TEXT,
    error_code      TEXT,
    error_message   TEXT,
    started_at      TEXT NOT NULL,
    finished_at     TEXT
);

CREATE INDEX idx_browser_tasks_profile ON browser_tasks(profile_id);
CREATE INDEX idx_browser_tasks_conversation ON browser_tasks(conversation_id);

CREATE TABLE browser_permissions (
    id              TEXT PRIMARY KEY,
    profile_id      TEXT NOT NULL REFERENCES browser_profiles(id) ON DELETE CASCADE,
    origin          TEXT,                -- NULL = applies to all origins
    capability      TEXT NOT NULL,
    scope           TEXT NOT NULL,       -- once|task|conversation|origin|profile
    conversation_id TEXT,
    expires_at      TEXT,
    created_at      TEXT NOT NULL
);

CREATE INDEX idx_browser_permissions_profile ON browser_permissions(profile_id);

CREATE TABLE browser_downloads (
    id          TEXT PRIMARY KEY,
    profile_id  TEXT NOT NULL REFERENCES browser_profiles(id),
    task_id     TEXT REFERENCES browser_tasks(id),
    source_url  TEXT,
    filename    TEXT NOT NULL,
    local_path  TEXT NOT NULL,
    mime_type   TEXT,
    size_bytes  INTEGER,
    status      TEXT NOT NULL,           -- in_progress|completed|cancelled|blocked
    created_at  TEXT NOT NULL
);

CREATE INDEX idx_browser_downloads_profile ON browser_downloads(profile_id);

-- Seed the Default profile (spec §6.2). profile_path is environment-dependent,
-- so it is stored empty and resolved lazily at runtime.
INSERT INTO browser_profiles (id, name, engine, executable_path, profile_path, created_at, updated_at)
VALUES ('default', 'Default', 'bundled_chromium', NULL, '', '1970-01-01T00:00:00Z', '1970-01-01T00:00:00Z');

INSERT INTO browser_preferences (profile_id) VALUES ('default');
