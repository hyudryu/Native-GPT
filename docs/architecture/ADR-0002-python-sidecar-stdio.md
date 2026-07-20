# ADR-0002: Python sidecar over stdio NDJSON (unchanged from source plan)

**Status:** Accepted (2026-07-20)

## Context

Strands Agents SDK is the mandated agent loop; its Python SDK is the stable one. The runtime must be isolatable, restartable, and must not open network ports.

## Decision

The host spawns a Python sidecar and speaks versioned NDJSON over stdin/stdout (protocol v1.0, `packages/protocol-types`). Sidecar logs go to stderr only. The sidecar never binds a TCP port. Sidecar state is rehydratable from SQLite so it can be killed when idle (ADR-0004).

## Consequences

- (+) Process isolation; crash = restart, not app failure.
- (+) No extra listening port; protocol is trivially loggable/replayable.
- (−) Python packaging per-platform remains a risk (mitigated: uv-managed runtime).
