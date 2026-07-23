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
  /** When false, the sidecar skips TLS certificate verification. Default true. */
  tls_verify?: boolean;
}
export interface ModelsList {
  base_url: string;
  api_key_ref?: string;
  model_list_path?: string;
  /** When false, the sidecar skips TLS certificate verification. Default true. */
  tls_verify?: boolean;
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
  enabled_tools?: string[];
  /** When false, the sidecar skips TLS certificate verification. Default true. */
  tls_verify?: boolean;
  /** When true, the sidecar runs in Tool Manager mode (registers save_tool). */
  factory_mode?: boolean;
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
/** A requires_approval tool call is paused until the user decides (sidecar → UI). */
export interface RunApprovalNeeded {
  run_id: string;
  approval_id: string;
  tool: string;
  input: Record<string, unknown>;
  prompt: string;
}
/** The user's decision for a pending approval prompt (UI → sidecar). */
export interface RunApprove {
  approval_id: string;
  approved: boolean;
  reason?: string;
}
/** Acknowledgement of run.approve; resolved=false means the approval_id was unknown. */
export interface RunApproveOk {
  resolved: boolean;
}
/** The prompt closed (approved, denied, or auto-denied on cancel) — dismiss it (sidecar → UI). */
export interface RunApprovalResolved {
  run_id: string;
  approval_id: string;
  approved: boolean;
}
export interface RunTextDelta {
  run_id: string;
  text: string;
}
/** A concise description of what the agent is doing before it has an answer. */
export interface RunActivity {
  run_id: string;
  message: string;
  source?: string;
}
/** A tool invocation is starting: the model selected a tool and supplied arguments. */
export interface RunToolCall {
  run_id: string;
  call_id: string;
  tool: string;
  input: Record<string, unknown>;
}
/** Structured error embedded in a `run.tool_result` payload when a tool failed. */
export interface ToolResultError {
  code: string;
  message: string;
}
/**
 * A tool invocation finished. Mirrors the standard tool result schema minus
 * artifacts/citations/warnings (added when artifact tools exist).
 */
export interface RunToolResult {
  run_id: string;
  call_id: string;
  tool: string;
  ok: boolean;
  summary: string;
  data?: Record<string, unknown>;
  error?: ToolResultError | null;
  retryable?: boolean;
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
  | "run.activity"
  | "run.tool_call"
  | "run.tool_result"
  | "run.approval_needed"
  | "run.approve"
  | "run.approval_resolved"
  | "run.text_delta"
  | "run.completed"
  | "run.failed"
  | "runtime.status";
