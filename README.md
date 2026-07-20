# Native GPT

A local-first, model-agnostic AI chat application (working title: Native GPT; formerly AgentGPT Desktop): Tauri desktop shell + embedded web server, reachable from mobile browsers over Tailscale as an installable PWA. Agent loop powered by Amazon's Strands Agents SDK (Python sidecar).

## Repository layout

- `apps/ui` — React 19 + Vite web app (desktop webview + mobile PWA, one codebase)
- `apps/host` — Rust/Tauri host: axum HTTP+WS server, SQLite, keychain, sidecar supervisor
- `apps/agent-runtime` — Python Strands sidecar (NDJSON over stdio, uv-managed)
- `packages/protocol-types` — NDJSON/WS message schemas (source of truth)
- `packages/design-system` — design tokens and Tailwind theme
- `docs/architecture` — ADRs
- `scripts/` — dev runner and utilities

## Quick start (dev)

Prerequisites: Rust 1.95+, Node 24+, pnpm 9+, uv 0.11+.

```bash
pnpm install                 # UI deps
uv sync --directory apps/agent-runtime   # Python runtime deps
./scripts/dev.sh             # starts sidecar, host server, and vite dev server
```

See `docs/architecture/` for the architectural decision records.
