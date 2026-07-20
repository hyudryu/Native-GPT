/**
 * AgentGPT protocol v1.0 — TypeScript types.
 * Hand-written to mirror schemas/envelope.json and schemas/messages.json.
 * Contract tests validate these against the JSON schemas; keep them in sync.
 */

export const PROTOCOL_VERSION = "1.0" as const;

export interface Envelope<T = Record<string, unknown>> {
  protocol: typeof PROTOCOL_VERSION;
  type: string;
  request_id: string;
  sequence?: number;
  timestamp: string; // ISO 8601
  payload: T;
}

// ---- runtime lifecycle ----
export interface RuntimeHello {
  client: string;
  client_version: string;
}
export interface RuntimeHelloOk {
  runtime: string;
  runtime_version: string;
  protocol: typeof PROTOCOL_VERSION;
  capabilities?: string[];
}
export type RuntimeHealth = Record<string, never>;
export interface RuntimeHealthOk {
  status: "ok" | "degraded";
  uptime_seconds: number;
  rss_bytes: number;
}
export type RuntimeShutdown = Record<string, never>;

// ---- endpoints & models (Phase 2) ----
export interface EndpointTest {
  base_url: string;
  api_key_ref?: string;
  timeout_seconds?: number;
}
export interface ModelsList {
  base_url: string;
  api_key_ref?: string;
  model_list_path?: string;
}
export interface ModelsListOk {
  models: Array<{ id: string; raw?: Record<string, unknown> }>;
}

// ---- runs (Phase 2) ----
export interface RunStart {
  run_id: string;
  conversation_id: string;
  message_id: string;
  prompt: string;
  history: Array<{ role: "user" | "assistant"; content: string }>;
  system_prompt?: string;
  model: { base_url: string; model_id: string; api_key?: string };
}
export interface RunStarted {
  run_id: string;
  conversation_id: string;
}
export interface RunCancel {
  run_id: string;
}
export interface RunCancelled {
  run_id: string;
}
export interface RunTextDelta {
  run_id: string;
  text: string;
}
export interface RunCompleted {
  run_id: string;
  usage?: Record<string, unknown>;
}
export interface ProtocolError {
  code: string;
  message: string;
  retryable?: boolean;
}
export interface RunFailed {
  run_id: string;
  error: ProtocolError;
}

/** Message types emitted on the WS broadcast channel for Phase 0-2. */
export type StreamEventType =
  | "run.started"
  | "run.cancelled"
  | "run.text_delta"
  | "run.completed"
  | "run.failed"
  | "runtime.status";
