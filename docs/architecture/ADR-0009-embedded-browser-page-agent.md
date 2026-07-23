# ADR-0009: Embedded Browser with Alibaba Page Agent

**Status:** Accepted
**Date:** 2026-07-22
**Spec:** `docs/superpowers/specs/native-gpt-browser.md`

## Context

Native GPT needs a real, persistent, agent-controllable browser rendered as a
resizable right-side panel. Arbitrary webpages must never be loaded into the
React/Tauri DOM (CSP/`X-Frame-Options` breakage, no consistent extension
environment, no real Chrome profile). Alibaba Page Agent's multi-page flow
depends on its Chromium extension, so the browser must be a real Chromium
process with the pinned extension loaded.

## Decision

A **pinned, optional Chromium component** controlled by the Rust host over the
Chrome DevTools Protocol (CDP), rendered in the React right-side panel through
an authenticated binary screencast WebSocket, using a dedicated persistent
Native GPT browser profile, and the Alibaba Page Agent extension **Hub
protocol** for natural-language automation.

1. `BrowserManager` (Rust) launches Chromium with a Native GPT-owned
   `user-data-dir`, connects over CDP, and owns tabs, tasks, screencast,
   input, permissions, downloads, and audit persistence.
2. The Page Agent Hub page is opened inside the dedicated Chromium
   (`chrome-extension://<id>/hub.html?ws=<port>`); Native GPT speaks the small
   Hub bridge protocol (`execute`/`stop`, `ready`/`result`/`error`) directly.
   The stock `@page-agent/mcp` launcher is not used.
3. Provider API keys never reach the extension. The host exposes a loopback
   OpenAI-compatible proxy (`POST /internal/page-agent/v1/chat/completions`)
   that validates short-lived task tokens and reads real keys from the OS
   keychain.
4. The UI never talks to Chromium. It talks only to the authenticated Rust
   server: REST for control, one WebSocket per viewer for events + binary
   screencast frames (never base64 in the chat WebSocket).
5. The Python Browser tool is a thin authenticated proxy to the host; it never
   launches Chromium or holds browser state, so the browser survives sidecar
   restarts.

## Implementation plan (phases)

| Phase | Deliverable |
|---|---|
| 1 | `crates/server/src/browser/` core: protocol types, profile management + locking, component install state, task model, migration `0009_browser.sql` |
| 2 | Chromium launch + CDP client + screencast pump + input dispatch |
| 3 | Page Agent Hub bridge, model proxy, permission engine, downloads |
| 4 | React `features/browser/` (panel, toolbar, tabs, viewport, banner, hidden indicator, dialogs, store/api/stream/input bridge) + `AppShell` integration + Settings section |
| 5 | `tools/browser/` Python tool (manifest, tool.py, tests) |
| 6 | `packages/protocol-types` browser events; docs |
| 7 | Unit/integration tests; `cargo test`, `pnpm test`, `pytest` green |

## Consequences

- One cross-platform architecture (Windows/macOS/Linux); no OS window
  reparenting, no iframe.
- Chromium runtime is an optional downloadable component; base install size is
  unchanged. The feature degrades gracefully to "Not installed".
- First implementation: one Chromium process per profile, one Page Agent task
  per profile, single default profile in UI (multi-profile schema from day
  one), no arbitrary user extensions, no Chrome Sync, no CAPTCHA bypass.
