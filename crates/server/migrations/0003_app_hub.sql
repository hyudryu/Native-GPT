-- 0003_app_hub: app-wide knowledge/RAG sources and installed tool settings.

CREATE TABLE knowledge_sources (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    source_type TEXT NOT NULL CHECK (source_type IN ('paste', 'file', 'url')),
    source_uri TEXT,
    content TEXT NOT NULL,
    chunk_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX idx_knowledge_sources_created_at
    ON knowledge_sources(created_at DESC);

CREATE TABLE knowledge_chunks (
    id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL REFERENCES knowledge_sources(id) ON DELETE CASCADE,
    position INTEGER NOT NULL,
    content TEXT NOT NULL,
    embedding_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(source_id, position)
);

CREATE INDEX idx_knowledge_chunks_source_position
    ON knowledge_chunks(source_id, position);

CREATE TABLE tool_settings (
    tool_id TEXT PRIMARY KEY,
    enabled INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);
