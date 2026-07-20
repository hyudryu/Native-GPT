-- 0001_endpoints: endpoint + model persistence (source plan §15.1, trimmed)

CREATE TABLE endpoints (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    base_url TEXT NOT NULL,
    timeout_seconds INTEGER NOT NULL DEFAULT 15,
    tls_verify INTEGER NOT NULL DEFAULT 1,
    has_api_key INTEGER NOT NULL DEFAULT 0,
    default_model_id TEXT,
    last_test_status TEXT,
    last_tested_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE models (
    id TEXT PRIMARY KEY,
    endpoint_id TEXT NOT NULL REFERENCES endpoints(id) ON DELETE CASCADE,
    remote_model_id TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'discovered',
    hidden INTEGER NOT NULL DEFAULT 0,
    capabilities_json TEXT,
    raw_json TEXT,
    last_seen_at TEXT,
    UNIQUE(endpoint_id, remote_model_id)
);

CREATE INDEX idx_models_endpoint_id ON models(endpoint_id);
