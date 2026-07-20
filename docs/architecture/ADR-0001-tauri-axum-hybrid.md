# ADR-0001: Tauri shell + embedded axum web server (hybrid)

**Status:** Accepted (2026-07-20)

## Context

The app must work as a desktop application AND be reachable from a phone over Tailscale. The source plan (rev 2) specified Tauri with NDJSON-over-stdio only and explicitly "no open localhost port" — written before the mobile requirement existed.

## Decision

The Rust host embeds an axum HTTP + WebSocket server. The React UI is served as static files and talks to the host exclusively over HTTP/WS. The Tauri desktop webview loads `http://127.0.0.1:<port>`; mobile browsers load `http://<tailscale-ip>:<port>`. One UI codebase, one API. Tauri IPC is reserved for desktop-only extras (window controls, keychain UX).

## Consequences

- (+) Mobile support with zero extra UI work; PWA installable on iOS/Android.
- (+) Desktop and mobile share live state through the same server.
- (−) One TCP port is open (mitigated in ADR-0003).
- (−) Tauri custom title bar still implemented natively per platform.
