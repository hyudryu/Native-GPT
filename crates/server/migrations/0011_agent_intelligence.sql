-- 0011_agent_intelligence: planner ("Todo List"), goal supervisor, memory,
-- knowledge, and utility tables for the agent-intelligence tool family.
--
-- Numbering note: 0009/0010 are taken by the browser branch; this file is
-- numbered 0011 so both branches merge without a filename collision. The
-- migration runner tracks names in schema_migrations, so gaps are harmless.
--
-- Conventions: TEXT ids (uuid hex, tool-prefixed), RFC3339 timestamps,
-- JSON columns stored as TEXT with a `_json` suffix, soft delete via
-- `deleted_at` where rows are user-visible content.

-- ── Planner ("Todo List") ────────────────────────────────────────────────

CREATE TABLE plans (
    id TEXT PRIMARY KEY,
    run_id TEXT,
    conversation_id TEXT,
    project_id TEXT,
    goal TEXT NOT NULL,
    mode TEXT,
    status TEXT NOT NULL DEFAULT 'draft'
        CHECK (status IN ('draft', 'ready', 'running', 'paused', 'blocked', 'failed', 'completed', 'cancelled')),
    success_criteria_json TEXT,
    constraints_json TEXT,
    budget_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    started_at TEXT,
    completed_at TEXT,
    failure_json TEXT
);

CREATE INDEX idx_plans_conversation_id ON plans(conversation_id);
CREATE INDEX idx_plans_project_id ON plans(project_id);
CREATE INDEX idx_plans_run_id ON plans(run_id);
CREATE INDEX idx_plans_status ON plans(status);

CREATE TABLE plan_steps (
    id TEXT PRIMARY KEY,
    plan_id TEXT NOT NULL REFERENCES plans(id) ON DELETE CASCADE,
    position INTEGER NOT NULL,
    title TEXT NOT NULL,
    objective TEXT,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'ready', 'in_progress', 'blocked', 'failed', 'completed', 'skipped', 'cancelled')),
    dependencies_json TEXT,              -- JSON array of step ids
    required_capabilities_json TEXT,
    success_criteria_json TEXT,
    maximum_attempts INTEGER NOT NULL DEFAULT 2,
    attempts INTEGER NOT NULL DEFAULT 0,
    result_summary TEXT,
    evidence_refs_json TEXT,
    failure_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    started_at TEXT,
    completed_at TEXT
);

CREATE INDEX idx_plan_steps_plan_position ON plan_steps(plan_id, position);

CREATE TABLE plan_events (
    id TEXT PRIMARY KEY,
    plan_id TEXT NOT NULL REFERENCES plans(id) ON DELETE CASCADE,
    step_id TEXT,
    event_type TEXT NOT NULL,
    payload_json TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX idx_plan_events_plan_created ON plan_events(plan_id, created_at);

-- ── Goal supervisor ──────────────────────────────────────────────────────

CREATE TABLE goal_contracts (
    id TEXT PRIMARY KEY,
    run_id TEXT,
    conversation_id TEXT,
    goal TEXT NOT NULL,
    task_type TEXT,
    success_criteria_json TEXT NOT NULL, -- JSON array of validator specs
    required_capabilities_json TEXT,
    budgets_json TEXT,
    progress_json TEXT,                  -- latest snapshot + capped history
    status TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'completed', 'blocked', 'cancelled')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    completed_at TEXT,
    blocked_reason TEXT
);

CREATE INDEX idx_goal_contracts_conversation_id ON goal_contracts(conversation_id);
CREATE INDEX idx_goal_contracts_run_id ON goal_contracts(run_id);
CREATE INDEX idx_goal_contracts_status ON goal_contracts(status);

CREATE TABLE goal_validation_results (
    id TEXT PRIMARY KEY,
    contract_id TEXT NOT NULL REFERENCES goal_contracts(id) ON DELETE CASCADE,
    validator TEXT NOT NULL,
    passed INTEGER NOT NULL,
    detail_json TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX idx_goal_validation_contract_created
    ON goal_validation_results(contract_id, created_at);

CREATE TABLE run_recovery_attempts (
    id TEXT PRIMARY KEY,
    run_id TEXT,
    contract_id TEXT REFERENCES goal_contracts(id) ON DELETE CASCADE,
    reason TEXT,
    strategy TEXT,
    status TEXT NOT NULL DEFAULT 'requested',
    created_at TEXT NOT NULL
);

CREATE INDEX idx_run_recovery_run_id ON run_recovery_attempts(run_id);
CREATE INDEX idx_run_recovery_contract_id ON run_recovery_attempts(contract_id);

-- ── Memory ───────────────────────────────────────────────────────────────

CREATE TABLE memories (
    id TEXT PRIMARY KEY,
    scope TEXT NOT NULL DEFAULT 'global',   -- global | project | conversation
    scope_id TEXT,
    memory_key TEXT,
    content TEXT NOT NULL,
    summary TEXT,
    tags_json TEXT,
    lexical_text TEXT,
    embedding_json TEXT,
    embedding_version TEXT,
    importance REAL,
    confidence REAL,
    sensitivity TEXT,
    source_type TEXT,
    source_message_id TEXT,
    provenance_json TEXT,
    approved INTEGER NOT NULL DEFAULT 0,
    pinned INTEGER NOT NULL DEFAULT 0,
    access_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_accessed_at TEXT,
    expires_at TEXT,
    superseded_by TEXT,
    deleted_at TEXT
);

CREATE INDEX idx_memories_scope ON memories(scope, scope_id);
CREATE INDEX idx_memories_key ON memories(memory_key);
CREATE INDEX idx_memories_deleted_at ON memories(deleted_at);
CREATE INDEX idx_memories_last_accessed ON memories(last_accessed_at);

-- Full-text index over memory content. This is a STANDALONE FTS5 table (not
-- an external-content table) because memories uses TEXT primary keys and
-- FTS5 external content requires an integer rowid mapping. Triggers below
-- keep it in sync on INSERT/UPDATE/DELETE. Soft-deleted rows (deleted_at
-- set) remain indexed; queries must join memories on memory_id and filter
-- `deleted_at IS NULL`.
CREATE VIRTUAL TABLE memories_fts USING fts5(
    memory_id UNINDEXED,
    content,
    summary,
    tags,
    tokenize = 'unicode61'
);

CREATE TRIGGER memories_fts_insert AFTER INSERT ON memories BEGIN
    INSERT INTO memories_fts (memory_id, content, summary, tags)
    VALUES (new.id, new.content, COALESCE(new.summary, ''), COALESCE(new.tags_json, ''));
END;

CREATE TRIGGER memories_fts_update AFTER UPDATE ON memories BEGIN
    DELETE FROM memories_fts WHERE memory_id = old.id;
    INSERT INTO memories_fts (memory_id, content, summary, tags)
    VALUES (new.id, new.content, COALESCE(new.summary, ''), COALESCE(new.tags_json, ''));
END;

CREATE TRIGGER memories_fts_delete AFTER DELETE ON memories BEGIN
    DELETE FROM memories_fts WHERE memory_id = old.id;
END;

CREATE TABLE memory_proposals (
    id TEXT PRIMARY KEY,
    content TEXT NOT NULL,
    scope TEXT,
    scope_id TEXT,
    memory_key TEXT,
    tags_json TEXT,
    importance REAL,
    confidence REAL,
    sensitivity TEXT,
    expires_at TEXT,
    provenance_json TEXT,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'approved', 'rejected')),
    created_at TEXT NOT NULL,
    resolved_at TEXT
);

CREATE INDEX idx_memory_proposals_status ON memory_proposals(status);

-- ── Knowledge ────────────────────────────────────────────────────────────

CREATE TABLE knowledge_domains (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    description TEXT,
    parent_id TEXT REFERENCES knowledge_domains(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE knowledge_source_domains (
    source_id TEXT NOT NULL REFERENCES knowledge_sources(id) ON DELETE CASCADE,
    domain_id TEXT NOT NULL REFERENCES knowledge_domains(id) ON DELETE CASCADE,
    PRIMARY KEY (source_id, domain_id)
);

CREATE TABLE knowledge_proposals (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    content TEXT NOT NULL,
    scope TEXT,
    project_id TEXT,
    conversation_id TEXT,
    source_urls_json TEXT,
    tags_json TEXT,
    provenance_json TEXT,
    quality_score REAL,
    retention_reason TEXT,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'approved', 'rejected')),
    created_at TEXT NOT NULL,
    resolved_at TEXT
);

CREATE INDEX idx_knowledge_proposals_status ON knowledge_proposals(status);

CREATE TABLE knowledge_citations (
    id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL REFERENCES knowledge_sources(id) ON DELETE CASCADE,
    chunk_id TEXT,
    run_id TEXT,
    conversation_id TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX idx_knowledge_citations_conversation ON knowledge_citations(conversation_id);
CREATE INDEX idx_knowledge_citations_source ON knowledge_citations(source_id);

-- Extend knowledge_sources for scoped, classified, curated knowledge.
ALTER TABLE knowledge_sources ADD COLUMN conversation_id TEXT;
ALTER TABLE knowledge_sources ADD COLUMN scope TEXT NOT NULL DEFAULT 'global';
ALTER TABLE knowledge_sources ADD COLUMN tags_json TEXT;
ALTER TABLE knowledge_sources ADD COLUMN trust_class TEXT;
ALTER TABLE knowledge_sources ADD COLUMN source_quality REAL;
ALTER TABLE knowledge_sources ADD COLUMN retention_reason TEXT;
ALTER TABLE knowledge_sources ADD COLUMN pinned INTEGER NOT NULL DEFAULT 0;
ALTER TABLE knowledge_sources ADD COLUMN enabled INTEGER NOT NULL DEFAULT 1;
ALTER TABLE knowledge_sources ADD COLUMN deleted_at TEXT;

CREATE INDEX idx_knowledge_sources_scope ON knowledge_sources(scope);
CREATE INDEX idx_knowledge_sources_conversation ON knowledge_sources(conversation_id);
CREATE INDEX idx_knowledge_sources_deleted_at ON knowledge_sources(deleted_at);

-- Seed knowledge domains (fixed ids so tools can reference them stably).
INSERT INTO knowledge_domains (id, name, description, parent_id, created_at, updated_at) VALUES
    ('domain-artificial-intelligence', 'Artificial Intelligence', 'AI/ML models, techniques, tooling, and research.', NULL, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'), strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    ('domain-automotive', 'Automotive', 'Vehicles, maintenance, diagnostics, and the auto industry.', NULL, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'), strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    ('domain-fabrication', 'Fabrication', 'Making things: machining, 3D printing, electronics, woodworking.', NULL, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'), strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    ('domain-business', 'Business', 'Strategy, operations, marketing, and organizational topics.', NULL, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'), strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    ('domain-finance', 'Finance', 'Markets, investing, accounting, and personal finance.', NULL, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'), strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    ('domain-health', 'Health', 'Wellbeing, fitness, medicine, and nutrition.', NULL, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'), strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    ('domain-native-gpt', 'Native GPT', 'The Native GPT product itself: architecture, features, usage.', NULL, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'), strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    ('domain-andorra-labs', 'Andorra Labs', 'Andorra Labs company knowledge and projects.', NULL, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'), strftime('%Y-%m-%dT%H:%M:%fZ', 'now'));

-- ── Artifacts, attachments, notifications ────────────────────────────────

CREATE TABLE artifacts (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    mime_type TEXT,
    size_bytes INTEGER,
    sha256 TEXT,
    storage_path TEXT NOT NULL,
    conversation_id TEXT,
    project_id TEXT,
    created_by_tool TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    retention_policy TEXT,
    preview_path TEXT,
    source_artifact_id TEXT,
    deleted_at TEXT
);

CREATE INDEX idx_artifacts_conversation ON artifacts(conversation_id);
CREATE INDEX idx_artifacts_project ON artifacts(project_id);
CREATE INDEX idx_artifacts_deleted_at ON artifacts(deleted_at);

CREATE TABLE attachments (
    id TEXT PRIMARY KEY,
    conversation_id TEXT,
    project_id TEXT,
    artifact_id TEXT REFERENCES artifacts(id) ON DELETE SET NULL,
    name TEXT NOT NULL,
    mime_type TEXT,
    size_bytes INTEGER,
    created_at TEXT NOT NULL,
    deleted_at TEXT
);

CREATE INDEX idx_attachments_conversation ON attachments(conversation_id);

CREATE TABLE notifications (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    message TEXT,
    urgency TEXT NOT NULL DEFAULT 'normal',
    action_url TEXT,
    artifact_id TEXT,
    read INTEGER NOT NULL DEFAULT 0,
    dismissed INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE INDEX idx_notifications_unread ON notifications(read, dismissed);

-- ── Skills registry & tool grants ────────────────────────────────────────

CREATE TABLE skills (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT,
    version TEXT,
    publisher TEXT,
    type TEXT,
    trusted INTEGER NOT NULL DEFAULT 0,
    load_policy TEXT,
    prompt_file TEXT,
    tool_dependencies_json TEXT,
    service_dependencies_json TEXT,
    default_enabled INTEGER NOT NULL DEFAULT 0,
    install_path TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE skill_settings (
    skill_id TEXT NOT NULL,
    scope TEXT NOT NULL DEFAULT 'global',
    scope_id TEXT NOT NULL DEFAULT '',
    enabled INTEGER NOT NULL DEFAULT 1,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (skill_id, scope, scope_id)
);

CREATE TABLE tool_grants (
    tool_id TEXT NOT NULL,
    scope TEXT NOT NULL DEFAULT 'global',
    scope_id TEXT NOT NULL DEFAULT '',
    permissions_json TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY (tool_id, scope, scope_id)
);
