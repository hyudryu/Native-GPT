# ADR-0008: Remote backend host (bridge) for GPU workloads

**Status:** Accepted (2026-07-22)

## Context

AgentGPT runs on a Windows desktop without a GPU. To support image/video
generation (ComfyUI) and voice-cloning TTS (OpenVoice), the agent needs access
to GPU compute. The GPU lives on a separate Linux host (DGX Spark).

The two workloads have different management characteristics:

- **ComfyUI** ships a clean HTTP/WS API with an in-process VRAM release
  (`POST /free`). Models auto-load per workflow; idle VRAM can be freed without
  restarting.
- **OpenVoice** is a Python library with no built-in HTTP server and no
  unload API. Models stay resident until process teardown. VRAM release
  requires killing and respawning the worker process.

Both need to start on demand (no GPU process runs until requested), stop after
idle, and unload VRAM when not actively generating — so they don't hold the
GPU while the user does other things.

## Decision

Introduce a **remote backend host** ("bridge") — a standalone Python FastAPI
service that runs on the GPU box and manages workloads through a plugin API.
The desktop application is an authenticated client of one or more bridges.

```
AgentGPT Desktop (Windows)
 │  HTTP + WS, per-host bearer token
 ▼
Remote Backend Host (bridge, Linux/GPU)
 ├─ Workload manager (plugin registry)
 │   ├─ ComfyUI   : subprocess + HTTP/WS API + POST /free
 │   └─ OpenVoice : FastAPI worker subprocess + teardown on idle
 ├─ Two-tier idle: soft (release VRAM) → hard (stop process)
 └─ Voice registry: reference clips → extracted embeddings
```

### Key design choices

1. **Generic workload plugin API.** Each workload implements `start`, `stop`,
   `soft_idle`, `submit_job`. ComfyUI and OpenVoice are two implementations.
   This makes future workloads (e.g. Android automation) additive.

2. **Two-tier idle lifecycle.** Soft idle (default 5 min) releases VRAM —
   ComfyUI calls `POST /free`, OpenVoice tears down its worker. Hard idle
   (default 15 min) stops the process entirely. Configurable per workload.

3. **Desktop is host-agnostic.** Remote hosts are stored like endpoints
   (`remote_hosts` table + keychain token per host, key `host:<id>`). The UI
   and agent tools use one consistent API regardless of where the bridge runs.

4. **Generated assets stored locally.** The bridge returns bytes via a
   short-lived asset token; the desktop fetches them, writes to
   `app-data/assets/`, stores metadata in `generated_assets`, and serves via
   `/api/assets/{id}` (auth-gated). Asset bytes never live in SQLite.

5. **Agent tools call the desktop server, not the bridge directly.** Tools
   run inside the Strands sidecar and call back to `127.0.0.1:<port>` via the
   desktop's REST API (`/api/remote-hosts/generate`, `/tts`). The desktop
   proxies to the bridge. This keeps auth centralized and allows capability
   gating (structured "unavailable" response when no host is configured).

6. **Approval-gated side effects.** `comfyui_generate` and `openvoice_tts`
   set `requires_approval: true`, reusing the existing `HumanInTheLoop`
   approval gate — no new permission system.

## Consequences

- (+) GPU workloads start/stop automatically; VRAM is released when idle.
- (+) Desktop works unchanged against any bridge host (DGX, another box, etc.).
- (+) Workload plugin API makes future GPU services (Stable Diffusion video,
  Whisper, etc.) straightforward to add.
- (+) Voice cloning works from chat immediately via the `openvoice_register_voice`
  tool; a management GUI can be added later without bridge changes.
- (−) The bridge requires a Linux GPU host; on Windows-only setups the
  workload tools return a structured "no remote host" response.
- (−) ComfyUI and OpenVoice have asymmetric VRAM management (in-process vs
  process teardown); the plugin API abstracts this but it's worth knowing.
- (−) Generated asset bytes are copied bridge→desktop→disk, adding a hop. For
  this workload (images, short audio) this is fine; very large video outputs
  might warrant streaming in a future iteration.
