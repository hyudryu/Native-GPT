# AgentGPT Protocol — v1.0

Two transports, one message format:

1. **Host ↔ Python sidecar:** NDJSON over stdin/stdout. One JSON envelope per line. Sidecar logs go to stderr only.
2. **UI ↔ host:** WebSocket (`/ws`), JSON text frames with the same envelope.

## Envelope

See `schemas/envelope.json`. Every message carries `protocol` ("1.0"), `type`, `request_id`, `timestamp`, `payload`. Streaming events add a monotonic `sequence`.

## Rules

- Requests expect exactly one terminal response: either a typed `.ok` message or `error` with the same `request_id`.
- `run.start` opens an event stream (run.text_delta, run.completed, run.failed, …) sharing its `request_id`.
- `run.cancel` is best-effort; the stream ends with `run.failed` (code `cancelled`) or `run.completed`.
- Unknown message types must be ignored, not fatal. Protocol major-version mismatch is fatal at handshake.
- Payloads must validate against `schemas/messages.json`. Types are hand-written in Rust/Python/TS for Phase 0–1; contract tests validate both directions against these schemas (codegen deferred — see ADR-0007).

## Phase 0 message set

`runtime.hello`, `runtime.health`, `runtime.shutdown` (+ `.ok` responses, `error`).
Phase 2 adds: `endpoint.test`, `models.list`, `run.start`, `run.cancel`, `run.text_delta`, `run.completed`, `run.failed`.
