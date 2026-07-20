# ADR-0007: Hand-written protocol types with schema contract tests (codegen deferred)

**Status:** Accepted (2026-07-20)

## Context

`packages/protocol-types` defines JSON Schemas for the NDJSON/WS protocol. Rust, Python, and TypeScript all need matching types.

## Decision

For Phase 0–2, types are hand-written per language (`packages/protocol-types/src/index.ts`, Rust structs in `crates/server::protocol`, pydantic models in the sidecar). Contract tests in each language validate representative messages against the JSON Schemas, so drift fails CI. Full codegen (quicktype/datamodel-code-generator) is deferred until the message catalog stabilizes around Phase 5.

## Consequences

- (+) No codegen tooling in the critical path; schemas still enforced by tests.
- (−) Three hand-maintained type definitions — the contract tests are the guardrail.
