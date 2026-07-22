# Tool Factory — Design Spec

**Date:** 2026-07-22
**Branch:** `feature/tool-factory`
**Status:** Approved (design phase → implementation planning)

## Summary

The Tool Factory lets users create new Strands tools and edit existing ones through the Tools page. A user describes what they want (e.g. "a tool that displays a clock", "a tool that manages a file"); an agent generates a complete, self-contained `tool.py` plus a manifest; the human reviews and hand-edits both in a form + code panel; then saves. Saved tools appear immediately in the tools list. Editing any tool (including the built-ins) is supported, and built-in tools can be rolled back to their shipped defaults.

## Goals

1. **Create** a new tool from a natural-language requirement via the agent.
2. **Edit** an existing tool: load it into the factory, hand-edit the code/manifest directly, or instruct the agent to revise it ("genetic revision").
3. **Rollback** built-in tools to their shipped factory defaults.
4. Reuse the existing agent chat pipeline (streaming, model selection, HITL) — no parallel model-calling code.

## Non-goals

- A tool marketplace / remote sharing (the existing disabled "marketplace · soon" button is out of scope).
- Server-side auto-retry of failed generations; the human fixes bad output by hand or re-runs.
- Deleting tools (can be added later; not required now).
- Detecting precisely whether a built-in has been *modified* for v1 (the Reset button shows whenever a tool is rollback-eligible; see Rollback).

## Decisions (from brainstorming)

| Decision | Choice |
|---|---|
| Entry points | "Tool Factory" button at top of Tools page; "Edit" button on each tool card |
| Factory surface | Full page with a "← Back to Tools" button |
| Generation mechanism | Reuse existing agent chat flow with a special system prompt |
| Save flow | Preview-then-save: form + editable code panel; nothing written until human hits Save |
| Trust model | Human in full control of all manifest fields, including `trusted`; no special gating |
| Edit mode `id` | Read-only (folder name is identity) |
| Editing built-ins | Allowed; no special protection |
| Code delivery | Agent calls a structured `save_tool` meta-tool (Approach 1) |
| Meta-tool behavior | Pure *proposer* — returns the proposal; the UI's Save button does the write via REST |

## Architecture overview

```
ToolsPage                       ToolFactoryPage (full page)
 ├─ "Tool Factory" button ──────▶ (create mode)
 ├─ tool cards                  ├─ requirement box (create) / revision box (edit)
 │   ├─ enable/disable          ├─ agent transcript (streamed)
 │   ├─ "Edit" button ──────────▶ (edit mode, pre-loaded) ├─ manifest form (editable)
 │   └─ "Reset to default"*     ├─ code panel (editable tool.py)
 └─ GET /api/tools              └─ Save button ──▶ POST/PUT /api/tools
                                                       │
                          (* Reset to default only on rollback-eligible cards)
                                                       ▼
                         tools/<id>/{manifest.json, tool.py}  on disk
                                                       │
                                          (next GET /api/tools re-scans)
```

Generation path (create or revise):
```
ToolFactoryPage
  └─ POST /api/conversations/{id}/messages  (factory_mode: true, system_prompt = factory prompt)
       └─ Rust chat::send_message builds RunStart{ factory_mode: true, ... }
            └─ relay::run_start → sidecar
                 └─ chat.py: load normal enabled tools + register save_tool meta-tool
                      └─ Strands Agent streams; calls save_tool(...) exactly once
                           └─ run.tool_call(save_tool, {id,name,...,tool_code})
                                └─ run.tool_result({status:"proposed", ...})
                                     └─ UI routes save_tool args into the form + code panel
                                          (human reviews/edits → Save)
```

---

## Layer 1 — Backend (Rust host, `crates/server/`)

### 1a. New write/read endpoints

Add to `build_router()` in `crates/server/src/lib.rs` (alongside existing `/api/tools`, `/api/tools/{id}`):

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/tools/{id}/source` | Read existing `manifest.json` (parsed) + `tool.py` text. 404 if folder missing. |
| `POST` | `/api/tools` | Create. Body: `{ id, manifest, tool_code }`. 409 if folder exists. Writes both files, returns `ToolInfo`. |
| `PUT` | `/api/tools/{id}` | Update. Body: `{ manifest, tool_code }`. 404 if missing. `id` from path (read-only). Overwrites both files. Returns `ToolInfo`. |
| `POST` | `/api/tools/{id}/rollback` | Restore built-in default. 409 if not in embedded bundle. Overwrites files, returns `ToolInfo`. |

### 1b. Manifest (de)serialization

`ToolManifest` in `crates/server/src/tools.rs` currently only `Deserialize`s. Derive `Serialize` too so we can write it back out. Pretty-print JSON with sorted keys to match existing file style. `Option` fields serialize as `null`/omitted per existing convention (keep `#[serde(default)]` for deserialize; add `skip_serializing_if = "Option::is_none"`).

### 1c. Shared write helper

`fn write_tool_files(repo_root, id, manifest: &ToolManifest, tool_code: &str) -> Result<(), ApiError>`:
- Reuse `valid_id()` to reject bad ids (only `[a-z0-9-]`).
- Enforce `manifest.id == id == folder name`.
- Create `tools/<id>/` if missing (create path only; never for update — update requires existing folder).
- Write `manifest.json` (pretty JSON) and `tool.py` (UTF-8). Path-traversal-safe: id is validated to match the same regex used by the Python registry (`^[a-z0-9]+(?:-[a-z0-9]+)*$`).

### 1d. Source read handler

`GET /api/tools/{id}/source` → `{ manifest: <ToolManifest>, tool_code: <string> }`. Validates `id`, reads both files, 404 if missing. Used by edit mode to populate the factory.

### 1e. `factory_default` flag on `ToolInfo`

Add `pub factory_default: bool` to `ToolInfo` (`crates/server/src/tools.rs:37`). Computed in `list_for_state` by checking whether `<id>` exists in the embedded defaults bundle (see Layer 4). Mirrors into TS `ToolInfo` in `apps/ui/src/lib/appsApi.ts:6`.

### 1f. No DB migration

Tool enable-state (`tool_settings` table) is unchanged. A created tool defaults to `enabled = manifest.default_enabled` on next scan; `trusted` is whatever the human set in the manifest.

---

## Layer 2 — Agent / sidecar (`apps/agent-runtime/`)

### 2a. `save_tool` meta-tool (pure proposer)

New file `apps/agent-runtime/src/agentgpt_runtime/tools/factory.py`:

```python
from strands import tool

@tool
def save_tool(
    id: str,
    name: str,
    description: str,
    version: str,
    risk: str,                 # "read"|"write"|"execute"|"external_side_effect"
    requires_approval: bool,
    network: str,              # "none"|"outbound"
    timeout_seconds: int,
    trusted: bool,
    tool_code: str,            # full tool.py source; must export TOOL
) -> dict:
    """Propose a tool for the Tool Factory. Call EXACTLY ONCE per request.
    Do NOT write files — this returns the proposal for the human to review.
    tool_code must: import strands, define the function, set TOOL = <fn>.
    """
    return {
        "status": "proposed",
        "manifest": {
            "id": id, "name": name, "description": description, "version": version,
            "risk": risk, "requires_approval": requires_approval,
            "network": network, "timeout_seconds": timeout_seconds, "trusted": trusted,
        },
        "tool_code": tool_code,
    }
```

The function signature *is* the schema — Strands forces structured arguments, so no free-text parsing. `save_tool` performs **no side effects**; it only returns the proposal.

### 2b. `factory_mode` in the run payload

- **Rust** `RunStart` (`crates/server/src/protocol.rs:123`): add `#[serde(default, skip_serializing_if = "std::ops::Not::not")] pub factory_mode: bool` (omits the field unless `true`, matching the codebase's "serialize-on-set" convention). Propagates automatically since `RunStart` is serialized straight to the sidecar.
- **TS** `RunStart` (`packages/protocol-types/src/index.ts:57`): add `factory_mode?: boolean`.
- **Python** `RunStartPayload` (`apps/agent-runtime/.../protocol.py:167`): add `factory_mode: bool = False`. Required because the model has `extra="forbid"`.

### 2c. Factory wiring in `chat.py` (`_stream`, `chat.py:279`)

When `payload.factory_mode`:
1. Do **not** load the normal enabled tools (factory runs produce no side effects beyond the proposal). Build the agent's tool list as `[save_tool]` only.
2. Replace `payload.system_prompt` with the factory prompt (the host may also pass it; sidecar uses host-provided prompt if present, else a built-in default).
3. No approval gate needed — `save_tool` has no side effects.

### 2d. Factory system prompt (built-in default)

Instructs the model to:
- Reason briefly (1–3 sentences) about the requested tool.
- Call `save_tool` **exactly once** with all manifest fields and complete, self-contained `tool_code`.
- `tool_code` rules: `from strands import tool`; define the function with a clear docstring (Strands uses it as the tool description); set `TOOL = <fn>`; may import stdlib and `tools/_lib/*` via the established `importlib` pattern; must be valid Python 3.12+.
- **Edit mode:** the prompt embeds the existing manifest + current `tool.py` and the user's revision instruction, and tells the model to return the *revised* full `tool_code` (not a diff).

The host (`chat.rs`) is responsible for assembling this prompt and passing it as `system_prompt`; the sidecar falls back to a built-in default if absent.

### 2e. No new streaming protocol

The proposal reaches the UI as a normal `run.tool_call` (name=`save_tool`, input=the structured args) + `run.tool_result`. The UI already renders tool calls; the factory page listens for a `save_tool` call and routes its `input` into the form + code panel.

---

## Layer 3 — Frontend (`apps/ui/`)

### 3a. Routing

Add route `/tools/factory` (and `/tools/factory/:id` for edit mode) in `apps/ui/src/App.tsx`. New page component `apps/ui/src/pages/ToolFactoryPage.tsx`. Back button navigates to `/tools`.

### 3b. Tools page changes (`ToolsPage.tsx`)

- Add a **"Tool Factory"** button at the top → navigates to `/tools/factory`.
- Add an **"Edit"** button on each tool card → `/tools/factory/:id`.
- Add a **"Reset to default"** button on cards where `tool.factory_default === true` → calls `POST /api/tools/{id}/rollback`, invalidates the `tools` query.
- The disabled "marketplace · soon" button stays as-is (out of scope).

### 3c. ToolFactoryPage layout

Full page, two regions:
- **Left/top:** requirement box (create mode) or revision box (edit mode, pre-filled context), plus a "Generate / Revise" button that kicks off an agent run.
- **Agent transcript:** streamed text + the `save_tool` tool call rendered.
- **Manifest form (editable):** id (read-only in edit, auto-derived-but-editable in create), name, description, version, risk (select), network (select), timeout_seconds (number), requires_approval (checkbox), trusted (checkbox).
- **Code panel (editable):** a `<textarea>` or lightweight code editor showing `tool.py`. Always hand-editable.
- **Save button:** validates (id matches regex in create mode; code non-empty), then `POST` (create) or `PUT` (update). On success, navigate to `/tools` and invalidate the `tools` query so the new/edited tool appears.

### 3d. Edit mode population

On mount with `:id`, fetch `GET /api/tools/{id}/source` → populate the manifest form + code panel. The agent revision box is seeded with the current manifest + code as context ("Here is the current tool … the user wants …").

### 3e. API client (`appsApi.ts`)

Add typed helpers mirroring existing patterns:
- `useToolSource(id)` → `GET /api/tools/{id}/source`
- `createTool({id, manifest, tool_code})` → `POST /api/tools`
- `updateTool(id, {manifest, tool_code})` → `PUT /api/tools/{id}`
- `rollbackTool(id)` → `POST /api/tools/{id}/rollback`

### 3f. Generation kickoff

Reuse the existing chat message-send flow (`POST /api/conversations/{id}/messages`) but mark the run as factory mode. Two options for the conversation:
- **Create mode:** a transient/ephemeral factory conversation (or reuse the most recent). The factory page subscribes to the run's WS stream and listens for the `save_tool` tool call.
- The factory page does **not** persist the requirement as a permanent chat message unless reusing an existing conversation; v1 may use a dedicated transient conversation id per factory session.

(The exact conversation-management detail is an implementation choice scoped in the plan; the contract — `factory_mode: true` on the run — is fixed.)

---

## Layer 4 — Rollback (built-in defaults)

### 4a. Embed shipped defaults in the Rust binary

Use the `include_dir` crate in `crates/server`. At build time, embed every `tools/<id>/{manifest.json,tool.py}` present in the repo into the binary as the pristine "factory default" bundle.

- **Why embed vs. DB snapshot:** a snapshot captured on first startup goes stale if a future app update changes a built-in tool (rollback would restore the *old* version). Embedded defaults always match exactly what shipped with the running binary and are immune to on-disk edits. Tools are small Python files; duplication cost is negligible.
- A tool is **rollback-eligible** iff it exists in the embedded bundle (i.e. it shipped with the app). User-created tools are not in the bundle → no Reset button.
- `factory_default: bool` on `ToolInfo` is computed by checking bundle membership (Layer 1e).

### 4b. Rollback endpoint

`POST /api/tools/{id}/rollback`:
- If `<id>` not in embedded bundle → `409` ("not a built-in tool; cannot reset").
- Else overwrite on-disk `tools/<id>/manifest.json` + `tool.py` with the bundled pristine copies.
- Return updated `ToolInfo`.

### 4c. v1 "edited" detection

For v1, show "Reset to default" on every rollback-eligible card (whether or not it's been edited). Precisely detecting "modified vs default" (e.g. hashing on-disk content vs bundled) is a follow-up. This is safe — resetting an unmodified tool is a no-op.

---

## Error handling

- **Bad `id` on create:** server returns `400` ("invalid tool id; use lowercase kebab-case"); form also validates client-side.
- **Folder exists on create:** server returns `409`; UI prompts to edit instead.
- **Malformed `tool_code` (syntax error):** the sidecar's `load_tools` will fail to import the tool on next chat run; it won't crash the host. The tools list still shows it (manifest is valid) but it will be non-functional until fixed. v1 surface: the tool appears but fails when invoked; a future improvement could syntax-check on Save.
- **Agent doesn't call `save_tool`:** the transcript completes without a tool call; the form/code panel stays empty; the human can type code by hand or re-run.
- **Rollback on non-built-in:** `409`; UI hides the Reset button for those tools (driven by `factory_default`).

## Testing

- **Rust (`crates/server`):** unit tests for `write_tool_files` (valid/invalid id, create vs existing-folder, manifest round-trip serialize/deserialize), `factory_default` computation against the embedded bundle, rollback handler (eligible + 409 path), and source-read handler.
- **Python (`apps/agent-runtime`):** `save_tool` returns the expected proposal dict; factory mode registers only `save_tool`; non-factory runs are unaffected. ruff-clean.
- **UI:** component tests for ToolFactoryPage create/edit population and Save wiring; ToolsPage Edit + Reset buttons. `tsc --noEmit` clean; `vitest run` green.
- **Protocol types:** `packages/protocol-types` contract tests updated for the new `factory_mode` field.

## Out of scope / follow-ups

- Precise "modified vs default" detection for the Reset button.
- Syntax-checking `tool_code` on Save before writing.
- Deleting tools.
- Tool marketplace / sharing.
- Versioning/history of edits (only rollback-to-shipped exists for built-ins).
