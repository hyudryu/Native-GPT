-- 0002_phase3: projects, conversations, messages, runs, and local search.

CREATE TABLE projects (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    instructions TEXT NOT NULL DEFAULT '',
    endpoint_id TEXT REFERENCES endpoints(id) ON DELETE SET NULL,
    model_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK (model_id IS NULL OR endpoint_id IS NOT NULL),
    FOREIGN KEY (endpoint_id, model_id)
        REFERENCES models(endpoint_id, remote_model_id) ON DELETE SET NULL
);

CREATE TABLE conversations (
    id TEXT PRIMARY KEY,
    project_id TEXT REFERENCES projects(id) ON DELETE SET NULL,
    title TEXT NOT NULL,
    endpoint_id TEXT REFERENCES endpoints(id) ON DELETE SET NULL,
    model_id TEXT,
    archived_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK (model_id IS NULL OR endpoint_id IS NOT NULL),
    FOREIGN KEY (endpoint_id, model_id)
        REFERENCES models(endpoint_id, remote_model_id) ON DELETE SET NULL
);

CREATE INDEX idx_conversations_project_id ON conversations(project_id);
CREATE INDEX idx_conversations_archived_at ON conversations(archived_at);

CREATE TABLE messages (
    id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    role TEXT NOT NULL CHECK (role IN ('system', 'user', 'assistant', 'tool')),
    content TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'completed',
    created_at TEXT NOT NULL
);

CREATE INDEX idx_messages_conversation_id_created_at
    ON messages(conversation_id, created_at, id);

CREATE TABLE runs (
    id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    user_message_id TEXT REFERENCES messages(id) ON DELETE SET NULL,
    assistant_message_id TEXT REFERENCES messages(id) ON DELETE SET NULL,
    status TEXT NOT NULL,
    endpoint_id TEXT REFERENCES endpoints(id) ON DELETE SET NULL,
    model_id TEXT,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    usage_json TEXT,
    error_json TEXT,
    CHECK (model_id IS NULL OR endpoint_id IS NOT NULL),
    FOREIGN KEY (endpoint_id, model_id)
        REFERENCES models(endpoint_id, remote_model_id) ON DELETE SET NULL
);

CREATE INDEX idx_runs_conversation_id_started_at
    ON runs(conversation_id, started_at);

CREATE VIRTUAL TABLE conversation_search USING fts5(
    conversation_id UNINDEXED,
    title,
    message_content,
    tokenize = 'unicode61'
);

CREATE TRIGGER conversation_search_insert
AFTER INSERT ON conversations BEGIN
    INSERT INTO conversation_search (conversation_id, title, message_content)
    VALUES (new.id, new.title, '');
END;

CREATE TRIGGER conversation_search_update
AFTER UPDATE OF title ON conversations BEGIN
    DELETE FROM conversation_search WHERE conversation_id = old.id;
    INSERT INTO conversation_search (conversation_id, title, message_content)
    VALUES (
        new.id,
        new.title,
        COALESCE((
            SELECT group_concat(content, ' ')
            FROM messages
            WHERE conversation_id = new.id
        ), '')
    );
END;

CREATE TRIGGER conversation_search_delete
AFTER DELETE ON conversations BEGIN
    DELETE FROM conversation_search WHERE conversation_id = old.id;
END;

CREATE TRIGGER message_search_insert
AFTER INSERT ON messages BEGIN
    DELETE FROM conversation_search WHERE conversation_id = new.conversation_id;
    INSERT INTO conversation_search (conversation_id, title, message_content)
    SELECT
        c.id,
        c.title,
        COALESCE((
            SELECT group_concat(content, ' ')
            FROM messages
            WHERE conversation_id = c.id
        ), '')
    FROM conversations c
    WHERE c.id = new.conversation_id;
END;

CREATE TRIGGER message_search_update
AFTER UPDATE OF content ON messages BEGIN
    DELETE FROM conversation_search WHERE conversation_id = new.conversation_id;
    INSERT INTO conversation_search (conversation_id, title, message_content)
    SELECT
        c.id,
        c.title,
        COALESCE((
            SELECT group_concat(content, ' ')
            FROM messages
            WHERE conversation_id = c.id
        ), '')
    FROM conversations c
    WHERE c.id = new.conversation_id;
END;

CREATE TRIGGER message_search_delete
AFTER DELETE ON messages BEGIN
    DELETE FROM conversation_search WHERE conversation_id = old.conversation_id;
    INSERT INTO conversation_search (conversation_id, title, message_content)
    SELECT
        c.id,
        c.title,
        COALESCE((
            SELECT group_concat(content, ' ')
            FROM messages
            WHERE conversation_id = c.id
        ), '')
    FROM conversations c
    WHERE c.id = old.conversation_id;
END;
