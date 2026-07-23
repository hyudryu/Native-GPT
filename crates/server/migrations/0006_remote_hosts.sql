-- 0006_remote_hosts: registered remote backend hosts (the "bridge").
-- Mirrors the endpoints table: only a boolean has_token lives in the row;
-- the raw bearer token is stored in the keychain under key "host:<id>".

CREATE TABLE remote_hosts (
    id              TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    base_url        TEXT NOT NULL,
    tls_verify      INTEGER NOT NULL DEFAULT 1,
    has_token       INTEGER NOT NULL DEFAULT 0,
    status          TEXT,          -- reachable | unreachable | unknown
    last_checked_at TEXT,
    workloads_json  TEXT,           -- cached capability snapshot from last /health
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
