-- 0005_project_knowledge: scope knowledge sources to a project.
-- NULL = global source (available to all chats, unchanged behavior).
-- Non-NULL = scoped to that project only (its own chats use project + global).
-- Cascading delete mirrors how a deleted project already removes its scoped data.

ALTER TABLE knowledge_sources ADD COLUMN project_id TEXT REFERENCES projects(id) ON DELETE CASCADE;
CREATE INDEX idx_knowledge_sources_project_id ON knowledge_sources(project_id);
