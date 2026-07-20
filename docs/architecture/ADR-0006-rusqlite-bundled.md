# ADR-0006: rusqlite (bundled) over sqlx

**Status:** Accepted (2026-07-20)

## Context

The host needs SQLite with FTS5, WAL mode, and zero system dependencies for bundling.

## Decision

Use `rusqlite` with the `bundled` feature (compiles SQLite + FTS5 in). `sqlx` 0.9 was rejected: heavier async machinery, MSRV 1.94 pressure, and compile-time query checking we don't need for a small command-handler codebase. Migrations run via a tiny embedded migration runner (numbered SQL files).

## Consequences

- (+) Single static dependency, portable builds.
- (−) Sync API — DB calls run on `tokio::task::spawn_blocking` where they might block.
