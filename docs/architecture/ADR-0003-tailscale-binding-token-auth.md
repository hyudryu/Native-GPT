# ADR-0003: Tailscale-interface-only binding with token auth

**Status:** Accepted (2026-07-20)

## Context

Mobile access requires a listening port, but the host must not be reachable from arbitrary LANs or the internet.

## Decision

- The server binds `127.0.0.1` plus any interface whose address falls in Tailscale's CGNAT range `100.64.0.0/10`. No Tailscale interface → localhost only.
- Binding `0.0.0.0` is an explicit opt-in setting with a UI warning.
- All non-localhost requests require a random 32-byte bearer token (generated at first run, stored in the OS keychain, rotatable). Pairing via QR code encoding `http://<tailscale-ip>:<port>/?token=...`.
- PWA assets (`manifest.webmanifest`, service worker, icons) are served without auth so iOS can install the app; they contain no sensitive data.
- No TLS inside the tailnet (WireGuard already encrypts). Revisit if 0.0.0.0 is ever enabled.
- Strict CSP on all HTML responses; token is never written to logs.

## Consequences

- (+) Attack surface limited to the user's own tailnet.
- (−) Interface enumeration is platform-specific (mitigated: `if-addrs`/`get_if_addrs` style crate, feature-detected at startup; failure → localhost only).
