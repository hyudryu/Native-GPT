# AgentGPT Bridge

Remote backend host service for AgentGPT Desktop. Runs on a Linux GPU box
(e.g. DGX Spark) and manages GPU workloads — currently ComfyUI (image/video
generation) and OpenVoice (voice-cloning TTS).

See `docs/architecture/ADR-0008-remote-backend-host.md` for the full
architecture rationale.

## Quick start

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
