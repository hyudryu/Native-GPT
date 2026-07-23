# AgentGPT Bridge

Remote backend host service for AgentGPT Desktop. Runs on a Linux GPU box
(e.g. DGX Spark) and manages GPU workloads — currently ComfyUI (image/video
generation) and OpenVoice (voice-cloning TTS).

See `docs/architecture/ADR-0008-remote-backend-host.md` for the full
architecture rationale.

## Interfaces

- **MCP** at `POST /mcp` (streamable-http) — the primary interface for the
  desktop agent. Tools: `list_workloads`, `start_workload`, `stop_workload`,
  `comfyui_generate`, `openvoice_tts`, `openvoice_register_voice`,
  `list_voices`, `delete_voice`. Same bearer auth as REST (loopback exempt).
- **REST** — `/health` (reports `mcp: true`, `mcp_path: /mcp`),
  `/workloads/*`, `/assets/{token}` (serves generated media bytes), and the
  OpenVoice voice endpoints. Kept for asset serving and connection testing.

## Deploy to a Linux GPU server

From a repo checkout on the server:

```bash
./apps/bridge/deploy-linux.sh          # deploys to /bridge-mcp (default)
INSTALL_DIR=/opt/bridge ./apps/bridge/deploy-linux.sh
```

The script syncs the bridge source into `$INSTALL_DIR`, installs uv if
missing, creates `$INSTALL_DIR/.venv` (including the `mcp` dependency),
writes `$INSTALL_DIR/.env` with a generated `AGENTGPT_BRIDGE_TOKEN` (an
existing `.env` is never overwritten), writes an
`agentgpt-bridge.service` systemd unit, and prints the bridge URL, the `/mcp`
endpoint, and the token to paste into the desktop app (Settings → Remote
Hosts). Enable and start with:

```bash
sudo systemctl enable agentgpt-bridge.service
sudo systemctl start agentgpt-bridge.service
```

## Quick start (manual)

```bash
uv sync
# Generate a token and run:
AGENTGPT_BRIDGE_TOKEN=$(python -c "import secrets; print(secrets.token_hex(32))") \
uv run python -m agentgpt_bridge --host 0.0.0.0 --port 8443
```

The desktop app connects to this bridge as a "remote host" (Settings → Remote
Hosts). The bridge starts no workloads until requested; it starts ComfyUI or
OpenVoice on demand, unloads VRAM after a soft-idle timeout, and stops the
process after a hard-idle timeout.

## Workloads

- **comfyui** — spawned as `python main.py` (the ComfyUI server); the bridge
  talks to its HTTP/WS API. VRAM is released via `POST /free` on soft idle.
- **openvoice** — spawned as a FastAPI worker wrapping `openvoice/api.py`.
  VRAM is released by tearing down the worker process on soft idle (OpenVoice
  has no in-process unload API).

## Testing

```bash
uv run pytest
```

Tests use a `FakeWorkload` that simulates the lifecycle without a GPU.
