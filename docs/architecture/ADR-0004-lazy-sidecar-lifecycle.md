# ADR-0004: Lazy sidecar lifecycle for low dormant footprint

**Status:** Accepted (2026-07-20)

## Context

The app must use minimal RAM/CPU when dormant. A resident Python runtime with Strands loaded costs hundreds of MB even when idle.

## Decision

The host spawns the Python sidecar lazily (first chat/model operation) and terminates it after a configurable idle timeout (default 10 min). Dormant app = Rust host (< 80 MB RSS target) + webview. All durable state lives in SQLite; sidecar holds no state that isn't rehydratable. A "keep runtime warm" setting overrides the timeout. The host also watchdogs sidecar RSS and offers restart past a configurable ceiling.

## Consequences

- (+) Dormant footprint ~Rust host only.
- (−) First message after idle pays sidecar startup latency (mitigated: "starting runtime…" UI state).
