-- 0004_tool_events: persist the tool-call trace on the assistant message that
-- produced it, so reloading a conversation shows which tools the agent used.
-- Nullable: older assistant rows (and all user/system rows) stay NULL.

ALTER TABLE messages ADD COLUMN tool_events_json TEXT;
