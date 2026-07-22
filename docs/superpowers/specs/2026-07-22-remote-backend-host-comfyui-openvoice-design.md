# Remote Backend Host (Bridge) + ComfyUI + OpenVoice — Design

- **Status:** Approved (brainstormed 2026-07-22)
- **Scope:** First sub-project of the larger "remote GPU workloads" platform. Establishes a remote backend host ("bridge") running on a Linux GPU box (DGX Spark), with ComfyUI and OpenVoice as the first two managed workloads. Conversation mode and Android automation are explicitly out of scope (later specs).
- **Buildability note:** Everything is implementable and testable on Windows today against mock/fake backends. The real ComfyUI and OpenVoice workloads activate unchanged once a Linux GPU host is attached.

---

## 1. Goal

Give the agent the ability to generate images/video (ComfyUI) and cloned-voice audio (OpenVoice) by driving GPU workloads running on a remote backend host. The desktop application is a **client** of one or more registered remote hosts; it never assumes a local GPU.

The bridge is **general-purpose**: it manages *workloads* (start on demand, stop after idle, unload VRAM when idle) via a plugin API. ComfyUI and OpenVoice are the first two plugins. This makes Android (the original AgentGPT Android architecture) a future workload with no bridge rework.

## 2. Non-goals (this slice)

- Conversation mode / continuous voice UX (OpenVoice TTS is the foundation; the conversation UX is a later spec).
- The Android automation architecture (later spec; same bridge, different workload).
- A GUI for browsing/managing voice clones or generated assets (a later slice). Voice cloning is exposed via an agent tool now.
- Multi-tenant remote host serving many desktop clients (single trusted desktop client per host for now; authenticated, but not multi-tenant).

## 3. System Architecture

```
AgentGPT Desktop (Windows)
 │  axum server + React UI + Strands agent (unchanged)
 │
 │  HTTP + WebSocket, per-host bearer token (mirrors ADR-0003)
 ▼
Remote Backend Host  ← runs on DGX Spark (Linux, GPU), standalone Python service
 ├─ Workload manager (plugin registry: workload_id → Workload controller)
 │   ├─ comfyui   : spawn/stop `python main.py`, POST /free to release VRAM, poll /system_stats
 │   └─ openvoice : spawn/stop an in-repo FastAPI worker wrapping openvoice/api.py, teardown on idle
 ├─ Lifecycle: on-demand start, configurable idle timeout, health checks, per-workload state
 ├─ Voice registry: upload reference clip → extract speaker embedding → store keyed by voice_id
 ├─ Auth: 32-byte bearer token (house style), localhost + explicit-network binding, TLS for remote
 └─ HTTP + WS API: workload control + job submission + voice management + status streaming
```

### Why this shape
- **Generic workload plugin API.** ComfyUI and OpenVoice are two instances of "managed GPU subprocess." Defining the plugin contract once makes Android (and future workloads) additive.
- **Host-agnostic client.** The desktop stores remote hosts like it stores endpoints today and uses one consistent API regardless of host. This matches the original architecture doc's "one consistent API regardless of where the runtime is hosted."
- **Maps onto existing codebase patterns.** The bridge is a standalone Python service mirroring the `agentgpt-runtime` sidecar skeleton (uv + hatchling + protocol module + dispatch). The desktop stores hosts exactly like endpoints (`remote_hosts` table + keychain token per host).

### Key asymmetry between the two workloads (from integration research)
- **ComfyUI** ships a clean HTTP/WS API: `POST /prompt` (workflow graph + `client_id`), `GET /history/{id}`, `GET /view`, `POST /interrupt`, `GET /system_stats` (health + VRAM), and crucially **`POST /free {unload_models, free_memory}`** for in-process VRAM release without restarting.
- **OpenVoice** is a Python *library* (`openvoice/api.py`: `BaseSpeakerTTS`, `ToneColorConverter`) with **no built-in server and no unload API** — models stay resident until process teardown. So the bridge ships a thin FastAPI worker around `api.py`, and idle VRAM reclaim for OpenVoice means killing and respawning the worker on the next request.

---

## 4. Data Model

### 4.1 Desktop: `remote_hosts` table (mirrors `endpoints`)

```sql
-- crates/server/migrations/0005_remote_hosts.sql
CREATE TABLE remote_hosts (
  id              TEXT PRIMARY KEY,          -- UUIDv7
  name            TEXT NOT NULL,             -- "DGX Spark"
  base_url        TEXT NOT NULL,             -- https://dgx.local:8443
  tls_verify      INTEGER NOT NULL DEFAULT 1,
  has_token       INTEGER NOT NULL DEFAULT 0,-- raw token in keychain under key "host:<id>"
  status          TEXT,                      -- reachable | unreachable | unknown
  last_checked_at TEXT,
  workloads_json  TEXT,                      -- cached capability snapshot from last /health
  created_at      TEXT NOT NULL,
  updated_at      TEXT NOT NULL
);
```

Secret handling mirrors endpoints exactly: **only `has_token` boolean in the row**; raw token in the OS keychain under key `host:<id>` (prefix avoids collision with endpoint ids as the secret surface grows). `workloads_json` caches `{workload_id: {version, state, healthy}}` from the last `/health` so UI and agent see capabilities without a round-trip.

### 4.2 Desktop: `generated_assets` table (new)

Outputs (images/video/audio) are user-visible and referenceable from chat.

```sql
-- crates/server/migrations/0006_generated_assets.sql
CREATE TABLE generated_assets (
  id            TEXT PRIMARY KEY,           -- UUIDv7
  host_id       TEXT NOT NULL REFERENCES remote_hosts(id) ON DELETE CASCADE,
  workload      TEXT NOT NULL,              -- comfyui | openvoice
  kind          TEXT NOT NULL,              -- image | video | audio
  message_id    TEXT REFERENCES messages(id) ON DELETE SET NULL,
  prompt_text   TEXT,                       -- sanitized request text
  source_ref    TEXT,                       -- workflow id / voice_id
  storage_path  TEXT NOT NULL,              -- relative to app-data/assets/
  bytes         INTEGER,
  mime_type     TEXT,
  created_at    TEXT NOT NULL
);
CREATE INDEX idx_assets_message ON generated_assets(message_id);
CREATE INDEX idx_assets_host ON generated_assets(host_id);
```

Asset **bytes are stored on disk** under `app-data/assets/`, never in SQLite. The desktop writes bytes returned by the bridge and serves them via a new auth-gated `/api/assets/{id}` route.

### 4.3 Desktop: `voices` table (new)

Persistent registry of cloned-voice reference clips and their extracted embeddings.

```sql
-- crates/server/migrations/0007_voices.sql
CREATE TABLE voices (
  id            TEXT PRIMARY KEY,           -- UUIDv7, also the voice_id sent to the bridge
  name          TEXT NOT NULL,              -- user/agent-assigned label
  host_id       TEXT NOT NULL REFERENCES remote_hosts(id) ON DELETE CASCADE,
  source_kind   TEXT NOT NULL,              -- file | url
  source_ref    TEXT,                       -- local file path or original URL (sanitized)
  duration_ms   INTEGER,                    -- reference clip duration
  created_at    TEXT NOT NULL,
  last_used_at  TEXT
);
CREATE INDEX idx_voices_host ON voices(host_id);
```

The **raw reference clip and the extracted speaker embedding live on the bridge host**, not the desktop — the desktop stores only metadata. This keeps large media off the Windows box and means a re-clone doesn't re-upload. Embedding extraction happens once at registration time via OpenVoice's `ToneColorConverter.extract_se`.

### 4.4 Bridge runtime state (in-memory, not persisted)

The bridge is a stateless-on-boot process that rediscovers its workloads from config and starts none until requested (mirrors "no container runs until requested"):

```
workloads: {
  comfyui:   { state: stopped|starting|ready|busy|stopping|error, proc, since, last_health, idle_since },
  openvoice: { state: ..., proc, since, last_health, idle_since, voices_dir }
}
voices: { voice_id: {name, embedding_path, ref_clip_path, created_at} }   # file-backed under voices_dir
```

---

## 5. API Contracts

### 5.1 Bridge API (on the DGX)

All requests carry `Authorization: Bearer <host-token>`.

| Method | Path | Purpose |
|---|---|---|
| GET | `/health` | Liveness + capabilities: `{version, workloads:{comfyui:{state,healthy,version}, ...}}` |
| GET | `/workloads`, `/workloads/{id}` | List/detail workloads (state, idle config) |
| POST | `/workloads/{id}/start` | On-demand start (mark workload needed) |
| POST | `/workloads/{id}/stop` | Stop workload (flush/frees VRAM) |
| POST | `/workloads/{id}/jobs` | Submit a job; payload depends on workload |
| GET | `/workloads/{id}/jobs/{jid}` | Job status: queued/running/done/failed + result refs |
| POST | `/workloads/openvoice/voices` | Multipart: reference clip + name → extract embedding, return `{voice_id}` |
| GET | `/workloads/openvoice/voices`, `/{voice_id}` | List/get voice metadata |
| DELETE | `/workloads/openvoice/voices/{voice_id}` | Remove voice + its embedding |
| GET | `/assets/{asset_token}` | Fetch generated job output bytes (short-lived token) |
| WS | `/stream` | Realtime: job progress, state changes, health |

**ComfyUI job** — `POST /workloads/comfyui/jobs {kind:"workflow", workflow:<graph-json>}` or `{kind:"generate", prompt, model?, size?, seed?}` (bridge builds the graph). Result: `{output:[{kind:"image|video", asset_token, mime_type, bytes}]}`.

**OpenVoice job** — `POST /workloads/openvoice/jobs {text, voice_id?, accent?, speed?}`. If `voice_id` omitted, uses the default base speaker (no clone). Result: `{output:[{kind:"audio", asset_token, mime_type, bytes}]}`.

The bridge translates these to the real backends (ComfyUI `/prompt`+`/ws`+`/history`+`/view`; OpenVoice worker endpoints).

### 5.2 Desktop client API (the `/api/*` the UI and agent use)

Host registry (mirrors `/api/endpoints`):
```
GET    /api/remote-hosts
POST   /api/remote-hosts
PATCH  /api/remote-hosts/{id}
DELETE /api/remote-hosts/{id}
POST   /api/remote-hosts/{id}/test   → reachability + capability snapshot, caches workloads_json
```

Voices passthrough (proxied to the bridge with the host token):
```
GET    /api/remote-hosts/{hid}/voices
POST   /api/remote-hosts/{hid}/voices          (multipart upload; desktop streams bytes to bridge)
DELETE /api/remote-hosts/{hid}/voices/{id}
```

Asset access:
```
GET    /api/assets/{id}                        → serve generated asset bytes (auth-gated)
```

Generation requests go through **agent tools** (Section 6), not direct UI REST in this slice.

### 5.3 Protocol types (ADR-0007)

New message shapes added to `packages/protocol-types` schemas + `crates/server/src/protocol.rs` + sidecar pydantic models, kept in sync by the existing contract tests. The primary bridge transport is HTTP/WS; NDJSON is used only if the bridge is run as a supervised sidecar (not the default).

### 5.4 Security
- Bridge binds localhost + explicitly-enabled network (mirrors ADR-0003). TLS for remote connections.
- Desktop stores per-host token in keychain; `tls_verify` honored end-to-end (precedent: PR #8).
- Token flows only desktop→bridge, never to the agent. Tool arguments are sanitized before logging (existing pattern). Sensitive text (e.g. a future TTS password field) is excluded from transcripts/logs per the existing redaction discipline.

---

## 6. Agent Tools & Lifecycle

Each tool follows the existing `tools/<id>/{manifest.json,tool.py}` pattern and reuses the **already-wired approval gate** (`requires_approval: true` → `HumanInTheLoop` → `run.approval_needed`).

### 6.1 New tools

```
tools/
├── comfyui-generate/        manifest: risk=external_side_effect, requires_approval=true
├── openvoice-tts/           manifest: risk=external_side_effect, requires_approval=true
├── openvoice-register-voice/manifest: risk=external_side_effect, requires_approval=true
├── remote-host-status/      manifest: risk=read, requires_approval=false
```

- **`comfyui_generate`** — args `{host?, prompt, kind:image|video, model?, size?, seed?, workflow?}`. Resolves host (named or default), ensures ComfyUI is started via the bridge, submits the job, streams progress as `run.activity`, fetches result bytes, writes a `generated_assets` row, returns `{ok, asset_id, summary}`. Returns structured "capability unavailable" when no host is configured/enabled.
- **`openvoice_tts`** — args `{host?, text, voice_id?, accent?, speed?}`. Same lifecycle against the openvoice workload; returns an audio asset. `voice_id` references the voice registry; omit for the default base speaker.
- **`openvoice_register_voice`** — args `{host?, name, file_path}` (or `url`). Uploads the clip to the bridge, which extracts the embedding; inserts a `voices` row; returns `{ok, voice_id, summary}`. This is the path that reaches the clone endpoint in this slice (GUI deferred).
- **`remote_host_status`** — read-only: lists configured hosts + reachable workloads + their states. The agent calls this to decide whether generation is possible and which host to use. Never gated.

### 6.2 Lifecycle behavior inside the bridge (the auto-manage requirement)

Implements the spec: **start when needed, stop after timeout, unload models when idle.** Two tiers:

```
Generation requested
  → workload not ready → bridge POST /workloads/{id}/start → starting → ready
  → submit job → busy
  → job done → ready, start idle timer
  → SOFT idle timer (default 5 min) fires
      ComfyUI   → POST /free {unload_models:true, free_memory:true}   (VRAM freed, process stays warm)
      OpenVoice → tear down the worker process                        (VRAM freed; respawn next request)
  → HARD idle timer (default 15 min) fires → stop the workload process entirely
```

Two tiers because ComfyUI gets cheap in-process VRAM release via `/free`, while OpenVoice has no unload API and must drop its process. Thresholds are configurable per workload and host.

### 6.3 Tool availability gating

Workload tools register with the agent **only when**:
- A remote host is configured AND reachable AND healthy, AND
- The relevant workload is available on that host.

When unavailable, the agent gets structured capability state instead of broken tools:
```json
{"capability":"comfyui_generation","enabled":false,"status":"no_remote_host","user_action_available":"Add a remote host in Settings → Remote Hosts"}
```

### 6.4 What's testable on Windows now
- Tools, the "unavailable" path, the desktop host registry, voice registry, and asset storage: **fully real, unit + integration tested.**
- The bridge lifecycle logic: **real Python, tested against a `FakeWorkload`** that simulates start/stop/idle/VRAM-free without a GPU. Real ComfyUI/OpenVoice plug in unchanged on the DGX.

---

## 7. UI

### 7.1 Settings → Remote Hosts (mirrors the endpoints feature)
`apps/ui/src/features/remote-hosts/`:
- `RemoteHostsSection.tsx` — section card listing hosts (name, URL, status dot: reachable/unreachable/unknown, workloads). "Add host" button.
- `RemoteHostCard.tsx` — per-host row + test/delete.
- `RemoteHostFormDialog.tsx` — name, base_url, token (password, tri-state like api_key), tls_verify.
- `apps/ui/src/lib/remoteHosts.ts` — types + `apiFetch` calls + TanStack hooks (`useRemoteHosts`, `useCreateRemoteHost`, `useTestRemoteHost`, ...), mirroring `lib/endpoints.ts`.

Dropped into `pages/SettingsPage.tsx` between `<EndpointsSection/>` and `<AppearanceSection/>`.

### 7.2 Generated asset rendering in chat
Extend the existing tool-call rendering in `ChatPage.tsx` / `MarkdownMessage.tsx`: when a `comfyui_generate`/`openvoice_tts` result includes an `asset_id`, render an inline `<img>` or `<audio controls>` inline. Reuses the existing tool-event accumulator/disclosure pattern.

### 7.3 Deferred UI
A GUI for browsing/managing voices and generated assets is a later slice. Voice cloning is reachable now via the `openvoice_register_voice` agent tool.

---

## 8. Project Layout

```
apps/bridge/                     ← NEW standalone Python service for the DGX (uv + hatchling)
  src/agentgpt_bridge/
    __main__.py                  HTTP/WS entry (mirrors agent-runtime skeleton)
    protocol.py                  request/response models + types
    server.py                    HTTP dispatch
    config.py                    workloads, idle thresholds, bind/token
    auth.py                      bearer-token check (mirrors auth.rs constant-time compare)
    workloads/
      base.py                    Workload protocol (start/stop/idle/free/health/submit_job)
      comfyui.py                 wraps ComfyUI HTTP/WS API + POST /free
      openvoice.py               spawns & wraps the OpenVoice FastAPI worker; teardown on idle
      fake.py                    FakeWorkload for tests
    voices.py                    voice registry (extract_se, store, list, delete)
  tests/                         pytest: lifecycle, idle timers, job submission, voice mgmt (no GPU)
  pyproject.toml

apps/bridge/openvoice_worker/    ← thin FastAPI worker around openvoice/api.py
  server.py                      /healthz, /synthesize, /change_voice
  pyproject.toml

crates/server/migrations/
  0005_remote_hosts.sql
  0006_generated_assets.sql
  0007_voices.sql
crates/server/src/
  remote_hosts.rs                handlers (mirrors endpoints.rs): CRUD + test + voices passthrough
  assets.rs                      /api/assets/{id} serving (auth-gated)
  db.rs                          RemoteHostRow, GeneratedAssetRow, VoiceRow + CRUD
  lib.rs                         register new routes in build_router()
  chat.rs / tools.rs             capability + availability gating

apps/ui/src/features/remote-hosts/   (section, card, form dialog)
apps/ui/src/lib/remoteHosts.ts
apps/ui/src/components/             asset rendering (image/audio) extension

tools/
  comfyui-generate/        manifest.json + tool.py
  openvoice-tts/           manifest.json + tool.py
  openvoice-register-voice/manifest.json + tool.py
  remote-host-status/      manifest.json + tool.py

docs/architecture/ADR-0008-remote-backend-host.md
```

---

## 9. Testing & Verification (Windows-buildable today)

- **Rust:** `cargo test --workspace` — db CRUD (remote_hosts, generated_assets, voices), handlers, capability gating against a fake bridge, asset serving, secret rollback-on-insert-failure.
- **Python:** `pytest` — bridge lifecycle/idle/VRAM-free against `FakeWorkload`; voice registry (extract mocked); tools tested against a mock bridge client (existing `tools/**/test_*.py` pattern). `ruff` lint.
- **UI:** Vitest + `tsc --noEmit` — remoteHosts hooks; inline asset rendering.
- **Integration:** extend `tests/integration/` with a desktop → (mock bridge) round-trip: register host → test → submit comfyui job → asset stored + served; register voice → tts with it.
- **CI** already runs Rust/Python/UI/integration jobs on `windows-latest`. No Windows-only hacks in code paths; real ComfyUI/OpenVoice activate unchanged on the DGX.
- **ADR-0008** documents the bridge/workload architecture and the ComfyUI-vs-OpenVoice lifecycle asymmetry.

## 10. Open questions / follow-ups
- Voice management GUI (later slice).
- Conversation mode built on OpenVoice (later spec).
- Android automation as a third workload on this same bridge (later spec).
- Multi-host scheduling / load-aware routing (deferred; one host is enough for v1).
