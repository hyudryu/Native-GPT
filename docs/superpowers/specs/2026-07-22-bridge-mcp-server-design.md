# Bridge MCP Server — Design

- **Status:** Implemented (2026-07-22). Bridge (GX10 side): MCP tool layer in `apps/bridge/src/agentgpt_bridge/mcp_server.py` mounted at `/mcp` (streamable-http), shared helpers in `ops.py`, bearer auth on `/mcp` via `BearerAuthMiddleware` in `auth.py`, `/health` reports `mcp`/`mcp_path`, `mcp` SDK pinned `>=1.28,<2`, tests in `apps/bridge/tests/test_mcp.py`, Linux deploy via `apps/bridge/deploy-linux.sh` (root `/bridge-mcp`). Desktop side: Rust host generates `app-data/mcp_servers.json` from `remote_hosts` (`crates/server/src/mcp_servers.rs`, regenerated on CRUD) and passes it via `AGENTGPT_MCP_SERVERS`; agent-runtime loads Strands `MCPClient`s (`apps/agent-runtime/src/agentgpt_runtime/mcp_servers.py`, per-run lifecycle, `continue_on_error`), the 4 callback tools + `_lib/bridge_client.py` + Rust generation endpoints are removed, and the UI renders bridge asset URLs via a same-origin proxy (`GET /api/remote-hosts/{host_id}/assets/{token}`).
- **Scope:** Convert the remote backend host (bridge) from a custom REST API into an MCP server, and update the desktop to connect via Strands' built-in `MCPClient`. This replaces the fragile env-var callback chain (tool.py → bridge_client.py → Rust generation endpoints) with a direct agent→MCP connection.

## 1. What changes

### Bridge (GX10 side)
The bridge gains an **MCP tool layer** (`@mcp.tool()` functions) that wraps the existing `WorkloadManager`. The MCP server is mounted into the FastAPI app at `/mcp` via streamable-http transport, so a single process serves both the MCP endpoint and the REST endpoints (asset serving, health).

MCP tools exposed:
- `list_workloads()` — capability snapshot (replaces `remote_host_status` tool)
- `start_workload(workload_id)` / `stop_workload(workload_id)` — lifecycle control
- `comfyui_generate(prompt, kind?, model?, size?)` — image/video generation
- `openvoice_tts(text, voice_id?, accent?, speed?)` — speech synthesis
- `openvoice_register_voice(name, file_path)` — voice cloning (clip → embedding → voice_id)
- `list_voices()` / `delete_voice(voice_id)` — voice registry management

Binary outputs: tools return asset URLs (text) for large media; the existing `/assets/{token}` REST endpoint serves the bytes. Small images can optionally use MCP's `Image` content type.

### Desktop (Windows side)
The agent-runtime loads the bridge as an **MCP server** via Strands' `MCPClient`, configured with the host URL + token. No custom tool.py files, no `bridge_client.py`, no Rust generation endpoints.

### What's removed
- `tools/comfyui-generate/` — replaced by the bridge's `comfyui_generate` MCP tool
- `tools/openvoice-tts/` — replaced by `openvoice_tts` MCP tool
- `tools/openvoice-register-voice/` — replaced by `openvoice_register_voice` MCP tool
- `tools/remote-host-status/` — replaced by `list_workloads` MCP tool
- `tools/_lib/bridge_client.py` — no longer needed
- `crates/server/src/bridge.rs` generation helpers — no longer needed (asset serving stays)
- Rust `/api/remote-hosts/generate` and `/api/remote-hosts/tts` endpoints — removed
- The `AGENTGPT_SERVER_PORT`/`AGENTGPT_SERVER_TOKEN` env-var injection in `lib.rs` — removed

### What stays
- Bridge workload plugin architecture, lifecycle, ComfyUI/OpenVoice workers — untouched
- `/assets/{token}` REST endpoint — stays for binary fetching
- `/health` REST endpoint — stays for the "test connection" UI
- `remote_hosts` table + keychain token + Settings → Remote Hosts UI — stays
- Bridge auth (bearer token) — stays, passed via MCP `headers`

## 2. MCP server config storage

MCP server configs are stored in `app-data/mcp_servers.json` (the shape `MCPClient.load_servers` expects):
```json
{
  "mcpServers": {
    "agentgpt-bridge": {
      "url": "https://gx10:8443/mcp",
      "transport": "streamable-http",
      "headers": { "Authorization": "Bearer ${host:HOST_ID_TOKEN}" }
    }
  }
}
```
The desktop server generates this file from the `remote_hosts` table at startup (one entry per reachable host). The agent-runtime loads it via `MCPClient.load_servers(path)`.

## 3. Transport

**streamable-http** at `http(s)://<host>:<port>/mcp`. This is the right choice for a remote host — stdio would require the desktop to spawn the bridge locally, defeating the remote-GPU purpose. Strands' `MCPClient` supports this natively via `mcp.client.streamable_http`.

## 4. Approval gating

MCP tools don't have `manifest.json`, so they can't use the existing `requires_approval` flag. For v1, MCP tools run without approval (the bridge's bearer token provides auth; the user explicitly adds the host). A future enhancement can add per-server `requires_approval` via `MCPServerConfig.prefix` or `tool_filters`.
