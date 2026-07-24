-- Per-endpoint thinking-mode parameter overrides (thinking modes spec §1.1).
-- JSON objects merged into the chat-completions request for thinking_mode
-- off/high, replacing the sidecar's built-in profile tables when set.
ALTER TABLE endpoints ADD COLUMN thinking_off_params_json TEXT;
ALTER TABLE endpoints ADD COLUMN thinking_high_params_json TEXT;
