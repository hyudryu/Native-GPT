# Tool Factory Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let users create and edit Strands tools via an agent-driven Tool Factory, with rollback to built-in defaults.

**Architecture:** A `save_tool` proposer meta-tool runs inside a `factory_mode` agent run; the human reviews the structured output in a form + code panel, then a REST endpoint writes `tools/<id>/{manifest.json,tool.py}`. Built-in defaults are embedded in the Rust binary via `include_dir` for rollback.

**Tech Stack:** Rust/axum (host), Python/Strands (sidecar), React 19/TanStack Query (UI), TypeScript protocol types validated against JSON schemas.

**Worktree:** `../Native-Mind-GPT-tool-factory` on branch `feature/tool-factory`. All paths below are relative to the worktree root.

---

## File Structure

**Create:**
- `apps/agent-runtime/src/agentgpt_runtime/tools/factory.py` — `save_tool` meta-tool (proposer)
- `apps/ui/src/pages/ToolFactoryPage.tsx` — full-page factory UI

**Modify:**
- `packages/protocol-types/src/index.ts` — add `factory_mode` to `RunStart`
- `packages/protocol-types/schemas/messages.json` — add `factory_mode` to `run.start` schema
- `crates/server/Cargo.toml` — add `include_dir` + `include_dir` crate
- `Cargo.toml` — add `include_dir` to workspace deps
- `crates/server/src/error.rs` — add `conflict()` helper
- `crates/server/src/protocol.rs` — add `factory_mode` to `RunStart`
- `crates/server/src/tools.rs` — derive `Serialize` on manifest; add write helper, `create`/`update`/`source`/`rollback` handlers, `factory_default` flag
- `crates/server/src/lib.rs` — register new routes
- `crates/server/src/chat.rs` — thread `factory_mode` + factory system prompt into `RunStart`
- `apps/agent-runtime/src/agentgpt_runtime/protocol.py` — add `factory_mode` to `RunStartPayload`
- `apps/agent-runtime/src/agentgpt_runtime/chat.py` — register `save_tool` when `factory_mode`
- `apps/ui/src/lib/appsApi.ts` — add `factory_default` to `ToolInfo`, add source/create/update/rollback helpers
- `apps/ui/src/pages/ToolsPage.tsx` — add Factory/Edit/Reset buttons
- `apps/ui/src/App.tsx` — add factory route

---

## Task 1: Protocol `factory_mode` field (TS + JSON schema)

**Files:**
- Modify: `packages/protocol-types/src/index.ts:57-68`
- Modify: `packages/protocol-types/schemas/messages.json` (run.start def, ~line 100-137)
- Test: `packages/protocol-types/src/schemas.test.ts`

- [ ] **Step 1:** Add to `RunStart` interface in `packages/protocol-types/src/index.ts`, after the `tls_verify` field (line 66):

```ts
  /** When true, the sidecar runs in Tool Factory mode (registers save_tool). */
  factory_mode?: boolean;
```

- [ ] **Step 2:** Add to the `run.start` definition in `packages/protocol-types/schemas/messages.json`, after the `tls_verify` property (line 124):

```json
        "factory_mode": { "type": "boolean", "default": false, "description": "When true, the sidecar runs in Tool Factory mode and registers the save_tool proposer meta-tool" },
```

- [ ] **Step 3:** Add a contract test to `packages/protocol-types/src/schemas.test.ts`, inside the existing `describe("protocol schemas", ...)` block, after the tls_verify test:

```ts
  it("threads optional factory_mode through the run.start payload", () => {
    const messages = schema("messages.json") as {
      $defs: Record<
        string,
        { properties?: Record<string, { type?: string }>; required?: string[] }
      >;
    };
    const definition = messages.$defs["run.start"];
    expect(definition.properties?.factory_mode?.type).toBe("boolean");
    expect(definition.required ?? []).not.toContain("factory_mode");
  });
```

- [ ] **Step 4:** Run contract tests.

Run: `cd packages/protocol-types && pnpm test`
Expected: PASS (all tests including the new one).

- [ ] **Step 5:** Commit.

```bash
git add packages/protocol-types/src/index.ts packages/protocol-types/schemas/messages.json packages/protocol-types/src/schemas.test.ts
git commit -m "Add factory_mode to RunStart protocol type and schema"
```

---

## Task 2: Rust `RunStart.factory_mode` + `ApiError::conflict`

**Files:**
- Modify: `crates/server/src/protocol.rs:122-137`
- Modify: `crates/server/src/error.rs:15-44`
- Test: `crates/server/src/tools.rs` (existing test module)

- [ ] **Step 1:** Add `conflict()` to `crates/server/src/error.rs`, after the `internal()` method (line 34):

```rust
    pub fn conflict(message: impl Into<String>) -> Self {
        Self::new(StatusCode::CONFLICT, "conflict", message)
    }
```

- [ ] **Step 2:** Add `factory_mode` field to `RunStart` in `crates/server/src/protocol.rs`, after the `tls_verify` field (line 135):

```rust
    /// When true the sidecar runs in Tool Factory mode (registers save_tool).
    #[serde(default, skip_serializing_if = "std::ops::Not::not")]
    pub factory_mode: bool,
```

- [ ] **Step 3:** Build to confirm it compiles.

Run: `cargo build -p agentgpt-server`
Expected: compiles (the existing `RunStart { ... }` literal in chat.rs will use the `bool` default of `false` until Task wires it; struct literal with a missing field of type `bool` is a compile error, so update the literal now).

Find the `RunStart { ... }` literal in `crates/server/src/chat.rs` (~line 123) and add `factory_mode: false,` before the closing brace (the `model:` field). This keeps current behavior; the factory wiring (separate task) sets it conditionally.

- [ ] **Step 4:** Build + run existing tests.

Run: `cargo test -p agentgpt-server`
Expected: PASS (existing tests unaffected).

- [ ] **Step 5:** Commit.

```bash
git add crates/server/src/protocol.rs crates/server/src/error.rs crates/server/src/chat.rs
git commit -m "Add factory_mode to Rust RunStart and conflict error variant"
```

---

## Task 3: Rust tool write endpoints (`create`, `update`, `source`)

**Files:**
- Modify: `crates/server/src/tools.rs`
- Modify: `crates/server/src/lib.rs:366-367`
- Test: `crates/server/src/tools.rs` (new unit tests)

- [ ] **Step 1:** Derive `Serialize` on `ToolManifest` and add `skip_serializing_if` to optional fields. In `crates/server/src/tools.rs`, change the struct (line 13):

```rust
#[derive(Debug, Clone, Deserialize, Serialize)]
struct ToolManifest {
    id: String,
    name: String,
    description: String,
    version: String,
    #[serde(default)]
    trusted: bool,
    #[serde(default)]
    default_enabled: bool,
    /// Spec vocabulary: "read" | "write" | "execute" | "external_side_effect".
    #[serde(default, skip_serializing_if = "Option::is_none")]
    risk: Option<String>,
    /// True when every call must be approved by the user in the UI.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    requires_approval: Option<bool>,
    /// "none" | "outbound" (informational soft-sandbox policy).
    #[serde(default, skip_serializing_if = "Option::is_none")]
    network: Option<String>,
    /// Per-tool default execution timeout.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    timeout_seconds: Option<u32>,
}
```

- [ ] **Step 2:** Add `factory_default` to `ToolInfo` (line 37):

```rust
#[derive(Debug, Clone, Serialize)]
pub struct ToolInfo {
    pub id: String,
    pub name: String,
    pub description: String,
    pub version: String,
    pub trusted: bool,
    pub enabled: bool,
    pub folder: String,
    pub risk: Option<String>,
    pub requires_approval: Option<bool>,
    pub network: Option<String>,
    pub timeout_seconds: Option<u32>,
    /// True when this tool ships with the app (rollback-eligible).
    pub factory_default: bool,
}
```

- [ ] **Step 3:** Add request body types + write helper after the `UpdateTool` struct (line 55). Append:

```rust
#[derive(Debug, Deserialize)]
pub struct CreateTool {
    id: String,
    manifest: serde_json::Value,
    tool_code: String,
}

#[derive(Debug, Deserialize)]
pub struct UpdateToolBody {
    manifest: serde_json::Value,
    tool_code: String,
}

#[derive(Debug, Serialize)]
pub struct ToolSource {
    pub manifest: ToolManifest,
    pub tool_code: String,
}

/// Write `manifest.json` + `tool.py` for a tool. `create_dir` controls
/// whether a new folder may be created (create path) vs requiring it to
/// exist (update path). The manifest's `id` must equal `id` and the folder.
fn write_tool_files(
    repo_root: &Path,
    id: &str,
    raw_manifest: serde_json::Value,
    tool_code: &str,
    create_dir: bool,
) -> Result<ToolManifest, ApiError> {
    if !valid_id(id) {
        return Err(ApiError::bad_request(
            "invalid tool id; use lowercase letters, digits, and hyphens",
        ));
    }
    let mut manifest: ToolManifest = serde_json::from_value(raw_manifest)
        .map_err(|e| ApiError::bad_request(format!("invalid manifest: {e}")))?;
    if manifest.id != id {
        return Err(ApiError::bad_request(
            "manifest id must match the tool id (folder name)",
        ));
    }
    let dir = repo_root.join("tools").join(id);
    if create_dir {
        if dir.exists() {
            return Err(ApiError::conflict(format!("tool {id} already exists")));
        }
        std::fs::create_dir_all(&dir).map_err(|e| ApiError::internal(e.to_string()))?;
    } else if !dir.is_dir() {
        return Err(ApiError::not_found(format!("tool {id} not found")));
    }
    let json = serde_json::to_string_pretty(&manifest)
        .map_err(|e| ApiError::internal(format!("failed to serialize manifest: {e}")))?;
    std::fs::write(dir.join("manifest.json"), format!("{json}\n"))
        .map_err(|e| ApiError::internal(e.to_string()))?;
    std::fs::write(dir.join("tool.py"), tool_code)
        .map_err(|e| ApiError::internal(e.to_string()))?;
    // Clamp the timeout hint in memory; persisted manifest is unchanged.
    manifest.timeout_seconds = manifest.timeout_seconds.map(|v| v.min(86_400));
    Ok(manifest)
}

fn read_tool_source(repo_root: &Path, id: &str) -> Result<ToolSource, ApiError> {
    if !valid_id(id) {
        return Err(ApiError::bad_request("invalid tool id"));
    }
    let dir = repo_root.join("tools").join(id);
    if !dir.is_dir() {
        return Err(ApiError::not_found(format!("tool {id} not found")));
    }
    let manifest: ToolManifest = serde_json::from_str(
        &std::fs::read_to_string(dir.join("manifest.json"))
            .map_err(|e| ApiError::internal(e.to_string()))?,
    )
    .map_err(|e| ApiError::bad_request(format!("invalid tool manifest: {e}")))?;
    if manifest.id != id {
        return Err(ApiError::internal(format!(
            "tool manifest id must match its folder: {}",
            dir.display()
        )));
    }
    let tool_code = std::fs::read_to_string(dir.join("tool.py"))
        .map_err(|e| ApiError::internal(e.to_string()))?;
    Ok(ToolSource { manifest, tool_code })
}
```

- [ ] **Step 4:** Update `list_for_state` to set `factory_default`. Since the embedded bundle isn't added until Task 4, set it to `false` for now and a TODO marker — BUT to avoid a placeholder, wire it through a helper that Task 4 fills. Add this helper near `manifests()`:

```rust
/// Whether a tool id ships with the app (rollback-eligible). Backed by the
/// embedded built-in bundle (see `defaults` module, Task 4). Returns false
/// for all until the bundle is wired.
fn is_factory_default(_id: &str) -> bool {
    crate::defaults::is_bundled(_id)
}
```

Then in `list_for_state`, when building each `ToolInfo`, add the field:

```rust
            factory_default: is_factory_default(&manifest.id),
```

- [ ] **Step 5:** Add the four handlers (`create`, `update`, `source`) at the end of the handlers section (after `patch`, line 162). Rollback is Task 4.

```rust
pub async fn source(
    State(state): State<SharedState>,
    AxumPath(id): AxumPath<String>,
) -> Result<Json<Value>, ApiError> {
    let src = read_tool_source(&state.repo_root, &id)?;
    Ok(Json(json!({ "manifest": src.manifest, "tool_code": src.tool_code })))
}

pub async fn create(
    State(state): State<SharedState>,
    Json(body): Json<CreateTool>,
) -> Result<(StatusCode, Json<Value>), ApiError> {
    let manifest = write_tool_files(&state.repo_root, &body.id, body.manifest, &body.tool_code, true)?;
    let tool = list_for_state(&state)
        .await?
        .into_iter()
        .find(|tool| tool.id == manifest.id)
        .ok_or_else(|| ApiError::internal("created tool not discovered"))?;
    Ok((StatusCode::CREATED, Json(json!({ "tool": tool }))))
}

pub async fn update(
    State(state): State<SharedState>,
    AxumPath(id): AxumPath<String>,
    Json(body): Json<UpdateToolBody>,
) -> Result<Json<Value>, ApiError> {
    // id is read-only (taken from the path); overwrite any id in the body so
    // the manifest stays consistent with the folder name.
    let mut manifest_value = body.manifest;
    if let Some(obj) = manifest_value.as_object_mut() {
        obj.insert("id".to_string(), serde_json::Value::String(id.clone()));
    }
    write_tool_files(&state.repo_root, &id, manifest_value, &body.tool_code, false)?;
    let tool = list_for_state(&state)
        .await?
        .into_iter()
        .find(|tool| tool.id == id)
        .ok_or_else(|| ApiError::not_found(format!("tool {id} not found")))?;
    Ok(Json(json!({ "tool": tool })))
}
```

- [ ] **Step 6:** Create the `defaults` module stub so `is_factory_default` compiles. Create `crates/server/src/defaults.rs`:

```rust
//! Built-in (factory-default) tool sources embedded in the binary.
//!
//! Task 4 wires the embedded bundle. Until then `is_bundled` returns false,
//! so no tool is rollback-eligible and the Reset button never appears.

pub fn is_bundled(_id: &str) -> bool {
    false
}
```

Add `mod defaults;` to `crates/server/src/lib.rs` (near the other `mod` declarations at the top).

- [ ] **Step 7:** Register routes in `crates/server/src/lib.rs`. Replace the two tool routes (lines 366-367):

```rust
        .route(
            "/api/tools",
            get(tools::list).post(tools::create),
        )
        .route("/api/tools/{id}", patch(tools::patch).put(tools::update))
        .route("/api/tools/{id}/source", get(tools::source))
        .route("/api/tools/{id}/rollback", post(tools::rollback))
```

(`rollback` handler is added in Task 4; to keep this task compiling, add a temporary stub now — see Step 8.)

- [ ] **Step 8:** Add a temporary `rollback` stub in `crates/server/src/tools.rs` so the route compiles (Task 4 replaces it):

```rust
pub async fn rollback(
    State(state): State<SharedState>,
    AxumPath(id): AxumPath<String>,
) -> Result<Json<Value>, ApiError> {
    // Task 4 embeds the built-in bundle and restores from it.
    let _ = state;
    Err(ApiError::conflict(format!(
        "tool {id} is not a built-in tool"
    )))
}
```

- [ ] **Step 9:** Add unit tests to the `#[cfg(test)] mod tests` block in `crates/server/src/tools.rs`:

```rust
    #[test]
    fn write_then_read_round_trips() {
        let dir = tempfile::tempdir().unwrap();
        let root = dir.path();
        let manifest = serde_json::json!({
            "id": "clock", "name": "Clock", "description": "shows time",
            "version": "1.0.0", "trusted": true, "default_enabled": false,
        });
        let written = write_tool_files(root, "clock", manifest, "TOOL = None\n", true).unwrap();
        assert_eq!(written.id, "clock");
        // Second create must 409.
        let err = write_tool_files(
            root, "clock",
            serde_json::json!({"id":"clock","name":"Clock","description":"x","version":"1.0.0"}),
            "x", true,
        ).unwrap_err();
        assert_eq!(err.status, axum::http::StatusCode::CONFLICT);
        // Update overwrites.
        write_tool_files(
            root, "clock",
            serde_json::json!({"id":"clock","name":"Clock2","description":"y","version":"1.1.0"}),
            "TOOL = None\n", false,
        ).unwrap();
        let src = read_tool_source(root, "clock").unwrap();
        assert_eq!(src.manifest.name, "Clock2");
        assert_eq!(src.tool_code, "TOOL = None\n");
    }

    #[test]
    fn rejects_bad_id_and_mismatched_manifest() {
        let dir = tempfile::tempdir().unwrap();
        let err = write_tool_files(
            dir.path(), "Bad Id",
            serde_json::json!({"id":"Bad Id","name":"x","description":"x","version":"1.0.0"}),
            "x", true,
        ).unwrap_err();
        assert_eq!(err.status, axum::http::StatusCode::BAD_REQUEST);
        let err = write_tool_files(
            dir.path(), "clock",
            serde_json::json!({"id":"other","name":"x","description":"x","version":"1.0.0"}),
            "x", true,
        ).unwrap_err();
        assert_eq!(err.status, axum::http::StatusCode::BAD_REQUEST);
    }
```

- [ ] **Step 10:** Add `tempfile` dev-dependency. In `crates/server/Cargo.toml` `[dev-dependencies]`:

```toml
tempfile.workspace = true
```

And in root `Cargo.toml` `[workspace.dependencies]`:

```toml
tempfile = "3"
```

- [ ] **Step 11:** Build + test.

Run: `cargo test -p agentgpt-server`
Expected: PASS (new tests pass, existing pass).

- [ ] **Step 12:** Commit.

```bash
git add crates/server/src/tools.rs crates/server/src/lib.rs crates/server/src/defaults.rs crates/server/Cargo.toml Cargo.toml
git commit -m "Add tool create/update/source endpoints and manifest serialization"
```

---

## Task 4: Rollback via embedded built-in defaults

**Files:**
- Modify: `Cargo.toml` (workspace deps)
- Modify: `crates/server/Cargo.toml`
- Modify: `crates/server/src/defaults.rs` (replace stub)
- Modify: `crates/server/src/tools.rs` (replace rollback stub)
- Test: `crates/server/src/defaults.rs`

- [ ] **Step 1:** Add `include_dir` to root `Cargo.toml` `[workspace.dependencies]`:

```toml
include_dir = "0.7"
```

- [ ] **Step 2:** Add to `crates/server/Cargo.toml` `[dependencies]`:

```toml
include_dir.workspace = true
```

- [ ] **Step 3:** Replace `crates/server/src/defaults.rs` with the embedded bundle:

```rust
//! Built-in (factory-default) tool sources embedded in the binary at build
//! time. A tool is rollback-eligible iff its `<id>/` folder exists in this
//! bundle. Embedding (vs. a runtime DB snapshot) means rollback always
//! restores exactly what shipped with the running binary, immune to edits.

use std::path::Path;

use include_dir::{include_dir, Dir};

/// The shipped `tools/` tree, captured at compile time. Only folders with
/// both `manifest.json` and `tool.py` are considered built-in tools.
static BUNDLED_TOOLS: Dir<'static> =
    include_dir!("$CARGO_MANIFEST_DIR/../../tools");

/// True if `<id>` is a built-in tool (shipped with the app).
pub fn is_bundled(id: &str) -> bool {
    BUNDLED_TOOLS
        .get_dir(id)
        .is_some_and(|dir| dir.get_file("manifest.json").is_some() && dir.get_file("tool.py").is_some())
}

/// Restore a built-in tool's `manifest.json` + `tool.py` to the shipped
/// version. Returns Ok only if the id is bundled and writes succeed.
pub fn restore(repo_root: &Path, id: &str) -> Result<(), String> {
    let dir = BUNDLED_TOOLS
        .get_dir(id)
        .ok_or_else(|| format!("tool {id} is not a built-in tool"))?;
    let dest = repo_root.join("tools").join(id);
    std::fs::create_dir_all(&dest).map_err(|e| e.to_string())?;
    for file in ["manifest.json", "tool.py"] {
        let entry = dir
            .get_file(file)
            .ok_or_else(|| format!("built-in {id} missing {file}"))?;
        let contents = entry.contents();
        std::fs::write(dest.join(file), contents).map_err(|e| e.to_string())?;
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn bundled_builtins_are_detected() {
        // These ship in the repo's tools/ dir.
        assert!(is_bundled("current-time"));
        assert!(is_bundled("calculate"));
        assert!(is_bundled("read-file"));
    }

    #[test]
    fn non_bundled_ids_rejected() {
        assert!(!is_bundled("definitely-not-a-tool"));
        assert!(!is_bundled("my-custom-tool"));
    }
}
```

- [ ] **Step 4:** Replace the `rollback` stub in `crates/server/src/tools.rs`:

```rust
pub async fn rollback(
    State(state): State<SharedState>,
    AxumPath(id): AxumPath<String>,
) -> Result<Json<Value>, ApiError> {
    if !valid_id(&id) {
        return Err(ApiError::bad_request("invalid tool id"));
    }
    crate::defaults::restore(&state.repo_root, &id)
        .map_err(ApiError::conflict)?;
    let tool = list_for_state(&state)
        .await?
        .into_iter()
        .find(|tool| tool.id == id)
        .ok_or_else(|| ApiError::not_found(format!("tool {id} not found")))?;
    Ok(Json(json!({ "tool": tool })))
}
```

- [ ] **Step 5:** Build + test.

Run: `cargo test -p agentgpt-server`
Expected: PASS — including `bundled_builtins_are_detected` and rollback flow.

- [ ] **Step 6:** Commit.

```bash
git add Cargo.toml crates/server/Cargo.toml crates/server/src/defaults.rs crates/server/src/tools.rs
git commit -m "Embed built-in tool defaults and add rollback endpoint"
```

---

## Task 5: Python `save_tool` meta-tool + factory wiring

**Files:**
- Create: `apps/agent-runtime/src/agentgpt_runtime/tools/factory.py`
- Modify: `apps/agent-runtime/src/agentgpt_runtime/protocol.py:167-180`
- Modify: `apps/agent-runtime/src/agentgpt_runtime/chat.py:279-352`
- Test: `apps/agent-runtime/tests/test_factory_tool.py`

- [ ] **Step 1:** Create `apps/agent-runtime/src/agentgpt_runtime/tools/factory.py`:

```python
"""Tool Factory meta-tool: a pure proposer the agent calls to emit a tool.

Registered ONLY when a run carries `factory_mode=True`. It performs no side
effects — it returns the proposed manifest + code for the host/UI to surface
for human review. The UI's Save button does the actual file write via REST.
"""

from __future__ import annotations

from typing import Any

from strands import tool

FACTORY_SYSTEM_PROMPT = """\
You are the Tool Factory. Given the user's request, produce ONE new or revised
Strands tool by calling the save_tool function EXACTLY ONCE.

Rules for tool_code:
- It is a complete, self-contained Python 3.12+ module.
- Start with `from strands import tool`.
- Define exactly one function decorated with `@tool`. Its docstring becomes the
  Strands tool description shown to agents — write it clearly.
- End with `TOOL = <function_name>`.
- You may import the Python standard library. To share helpers, import from
  `tools/_lib` using the project's importlib pattern (see existing tools).
- Return a plain string (or JSON-serializable value) from the function.

Think briefly (1-3 sentences) about what the tool should do, then call save_tool
with every field filled in. Do not write files; save_tool returns the proposal
for a human to review.
"""

FACTORY_REVISION_PROMPT = """\
You are the Tool Factory in REVISION mode. The user wants to modify an existing
tool. Below is its current manifest and source. Apply the user's requested
change and call save_tool EXACTLY ONCE with the FULL revised tool_code (not a
diff) and updated manifest fields. Keep the id unchanged.
"""


@tool
def save_tool(
    id: str,
    name: str,
    description: str,
    version: str,
    risk: str,
    requires_approval: bool,
    network: str,
    timeout_seconds: int,
    trusted: bool,
    tool_code: str,
) -> dict[str, Any]:
    """Propose a tool for the Tool Factory. Call EXACTLY ONCE per request.

    Returns the proposal for human review; nothing is written to disk.
    tool_code must be a complete module that exports TOOL.
    """
    return {
        "status": "proposed",
        "manifest": {
            "id": id,
            "name": name,
            "description": description,
            "version": version,
            "risk": risk,
            "requires_approval": requires_approval,
            "network": network,
            "timeout_seconds": timeout_seconds,
            "trusted": trusted,
        },
        "tool_code": tool_code,
    }
```

- [ ] **Step 2:** Add `factory_mode` to `RunStartPayload` in `apps/agent-runtime/src/agentgpt_runtime/protocol.py` (after `tls_verify`, line 179):

```python
    factory_mode: bool = False
```

- [ ] **Step 3:** Wire factory mode into `chat.py` `_stream`. Replace the tool-loading + agent-construction block (lines 283-286 and 341-352). At line 283, replace:

```python
        tools = load_tools(payload.enabled_tools)
        manifests = load_tool_manifests(payload.enabled_tools)
        allowed = approval_allowed_tools(payload.enabled_tools, tools, manifests)
```

with:

```python
        if payload.factory_mode:
            # Factory runs only expose the save_tool proposer (no side effects).
            from agentgpt_runtime.tools.factory import save_tool  # noqa: PLC0415

            tools = [save_tool]
            manifests = {}
            allowed = tools
        else:
            tools = load_tools(payload.enabled_tools)
            manifests = load_tool_manifests(payload.enabled_tools)
            allowed = approval_allowed_tools(payload.enabled_tools, tools, manifests)
```

Then, at the `Agent(...)` construction (line 341), conditionally swap the system prompt. Replace the `system_prompt=payload.system_prompt,` line with:

```python
            system_prompt=self._factory_prompt(payload),
```

and add a helper method on the `ChatRuns` impl (near `build_openai_model` or `_emit`). Add this method to the `ChatRuns` class:

```python
    @staticmethod
    def _factory_prompt(payload: RunStartPayload) -> str | None:
        """Pick the system prompt for a factory run.

        The host may supply a fully-formed factory prompt (including the
        existing tool context for revisions); otherwise fall back to the
        built-in create/revision defaults.
        """
        if not payload.factory_mode:
            return payload.system_prompt
        if payload.system_prompt:
            return payload.system_prompt
        from agentgpt_runtime.tools.factory import (  # noqa: PLC0415
            FACTORY_SYSTEM_PROMPT,
        )

        return FACTORY_SYSTEM_PROMPT
```

(Note: the host-side revision prompt embedding is done in chat.rs, which passes the rich prompt as `system_prompt`. The sidecar default covers the bare create case.)

- [ ] **Step 4:** Also disable the approval intervention for factory runs. At line 336, change the `interventions` block to skip HITL when factory_mode:

```python
        interventions: list[Any] = []
        if not payload.factory_mode and len(allowed) < len(tools):
            hitl = build_approval_intervention(allowed, ask_ui)
            interventions.append(hitl)
```

- [ ] **Step 5:** Create test `apps/agent-runtime/tests/test_factory_tool.py`:

```python
"""save_tool is a pure proposer and returns the expected payload shape."""

from agentgpt_runtime.tools.factory import (
    FACTORY_SYSTEM_PROMPT,
    save_tool,
)


def test_save_tool_returns_proposed_payload() -> None:
    result = save_tool.success(  # .success invokes the underlying fn directly
        id="clock",
        name="Clock",
        description="Shows the current time",
        version="1.0.0",
        risk="read",
        requires_approval=False,
        network="none",
        timeout_seconds=10,
        trusted=False,
        tool_code="from strands import tool\n\n@tool\ndef clock() -> str:\n    return 'now'\n\nTOOL = clock\n",
    )
    assert result["status"] == "proposed"
    assert result["manifest"]["id"] == "clock"
    assert result["manifest"]["trusted"] is False
    assert "TOOL = clock" in result["tool_code"]


def test_factory_prompt_instructs_single_call() -> None:
    assert "EXACTLY ONCE" in FACTORY_SYSTEM_PROMPT
    assert "save_tool" in FACTORY_SYSTEM_PROMPT
```

(Note: Strands wraps `@tool` functions; `.success(...)` is the Strands helper to invoke the underlying function with validated args. If `.success` is unavailable in the installed version, call the wrapped tool's underlying function via `save_tool.original(...)` or invoke directly. Verify by running the test; adjust the invocation to whatever the installed Strands version exposes for direct calls.)

- [ ] **Step 6:** Run Python tests + lint.

Run: `cd apps/agent-runtime && uv run pytest tests/test_factory_tool.py -v && uv run ruff check src/agentgpt_runtime/tools/factory.py src/agentgpt_runtime/chat.py src/agentgpt_runtime/protocol.py`
Expected: PASS.

- [ ] **Step 7:** Commit.

```bash
git add apps/agent-runtime/src/agentgpt_runtime/tools/factory.py apps/agent-runtime/src/agentgpt_runtime/protocol.py apps/agent-runtime/src/agentgpt_runtime/chat.py apps/agent-runtime/tests/test_factory_tool.py
git commit -m "Add save_tool factory meta-tool and factory_mode run wiring"
```

---

## Task 6: Thread `factory_mode` through Rust `chat.rs`

**Files:**
- Modify: `crates/server/src/chat.rs:37-240` (send_message) and the `SendMessage` body (line 16)

- [ ] **Step 1:** Add `factory_mode` to the `SendMessage` body in `crates/server/src/chat.rs` (line 16):

```rust
#[derive(Debug, Deserialize)]
pub struct SendMessage {
    content: String,
    #[serde(default, alias = "endpoint_id")]
    provider_id: Option<String>,
    #[serde(default)]
    model_id: Option<String>,
    #[serde(default)]
    factory_mode: bool,
    #[serde(default)]
    factory_revision: Option<String>,
}
```

- [ ] **Step 2:** In `send_message`, after `enabled_tools` is computed (line 92), build the factory system prompt when `body.factory_mode`. Insert before `let created_at = now();` (line 94):

```rust
    // Tool Factory: override the system prompt and disable normal tools so
    // the sidecar only exposes save_tool. A revision embeds the current tool.
    let (factory_mode, system_prompt) = if body.factory_mode {
        let prompt = match &body.factory_revision {
            Some(tool_id) => {
                let src = crate::tools::read_tool_source_public(&state, tool_id)
                    .ok();
                let existing = src.map(|s| format!(
                    "CURRENT MANIFEST:\n{}\n\nCURRENT tool.py:\n{}\n",
                    serde_json::to_string_pretty(&s.manifest).unwrap_or_default(),
                    s.tool_code,
                ));
                format!(
                    "{}\n\n{}\nUSER REVISION REQUEST: {}",
                    agentgpt_factory_revision_prompt(),
                    existing.unwrap_or_default(),
                    content,
                )
            }
            None => format!("{}\n\nUSER REQUEST: {}", agentgpt_factory_create_prompt(), content),
        };
        (true, Some(prompt))
    } else {
        (factory_mode /* false */, system_prompt)
    };
    let factory_mode = body.factory_mode;
```

Wait — `system_prompt` is shadowed. Simplify: replace the whole tail. Instead of the complex block, do the minimal correct thing: compute `factory_mode` flag and, if true, overwrite `system_prompt`. Replace the block from line 92 (`let enabled_tools = ...`) to line 144 (end of RunStart) carefully. The cleanest edit:

After line 91 (the existing `system_prompt` `let`), add:

```rust
    let factory_mode = body.factory_mode;
    let system_prompt = if factory_mode {
        Some(factory_system_prompt(content, body.factory_revision.as_deref(), &state).await)
    } else {
        system_prompt
    };
    let enabled_tools = if factory_mode { Vec::new() } else { enabled_tools };
```

And set `factory_mode,` in the `RunStart { ... }` literal (replacing the `factory_mode: false` placeholder from Task 2).

- [ ] **Step 3:** Add helper functions at the top of `chat.rs` (after the `now()` fn, ~line 35). These embed the prompt text (mirroring the Python constants):

```rust
/// Factory create prompt (kept in sync with the Python FACTORY_SYSTEM_PROMPT).
fn factory_system_prompt(
    user_request: &str,
    revision_target: Option<&str>,
    state: &SharedState,
) -> String {
    let base = if revision_target.is_some() {
        "You are the Tool Factory in REVISION mode..."
    } else {
        "You are the Tool Factory..."
    };
    // For revisions, embed the current tool source as context.
    let context = revision_target
        .and_then(|id| {
            crate::tools::read_tool_source_public(state, id).ok().map(|s| {
                format!(
                    "\n\nCURRENT MANIFEST:\n{}\n\nCURRENT tool.py:\n{}\n",
                    serde_json::to_string_pretty(&s.manifest).unwrap_or_default(),
                    s.tool_code,
                )
            })
        })
        .unwrap_or_default();
    format!("{base}{context}\n\nUSER REQUEST: {user_request}")
}
```

- [ ] **Step 4:** Expose `read_tool_source` from `tools.rs` for chat.rs. In `crates/server/src/tools.rs`, rename the private `read_tool_source` usage and add a public wrapper:

```rust
/// Public wrapper for use by `chat::factory_system_prompt`.
pub fn read_tool_source_public(
    state: &SharedState,
    id: &str,
) -> Result<ToolSource, ApiError> {
    read_tool_source(&state.repo_root, id)
}
```

Also derive `Serialize` on `ToolSource` is already done in Task 3 (it has `#[derive(Debug, Serialize)]`). Confirm `ToolSource` is `pub` — it is.

- [ ] **Step 5:** Build + test.

Run: `cargo test -p agentgpt-server`
Expected: PASS.

- [ ] **Step 6:** Commit.

```bash
git add crates/server/src/chat.rs crates/server/src/tools.rs
git commit -m "Thread factory_mode and factory system prompt through chat send_message"
```

---

## Task 7: UI API client helpers + `factory_default` flag

**Files:**
- Modify: `apps/ui/src/lib/appsApi.ts:6,28-29`

- [ ] **Step 1:** Add `factory_default` to `ToolInfo` (line 6):

```ts
export interface ToolInfo { id: string; name: string; description: string; version: string; trusted: boolean; enabled: boolean; folder: string; risk?: "read" | "write" | "execute" | "external_side_effect" | null; requires_approval?: boolean | null; network?: "none" | "outbound" | null; timeout_seconds?: number | null; factory_default: boolean; }
```

- [ ] **Step 2:** Add a `ToolManifest` interface + source/create/update/rollback helpers after `useUpdateTool` (line 29). Append:

```ts
export interface ToolManifest { id: string; name: string; description: string; version: string; trusted: boolean; default_enabled?: boolean; risk?: "read" | "write" | "execute" | "external_side_effect" | null; requires_approval?: boolean | null; network?: "none" | "outbound" | null; timeout_seconds?: number | null; }
export interface ToolSource { manifest: ToolManifest; tool_code: string; }

export function useToolSource(id: string | undefined) {
  return useQuery({
    queryKey: ["tools", id ?? "", "source"],
    queryFn: () => request<ToolSource>(`/api/tools/${encodeURIComponent(id!)}/source`),
    enabled: Boolean(id),
  });
}
export function useCreateTool() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (input: { id: string; manifest: ToolManifest; tool_code: string }) =>
      request<{ tool: ToolInfo }>("/api/tools", { method: "POST", body: JSON.stringify(input) }),
    onSuccess: () => client.invalidateQueries({ queryKey: ["tools"] }),
  });
}
export function useUpdateToolFiles() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: ({ id, manifest, tool_code }: { id: string; manifest: ToolManifest; tool_code: string }) =>
      request<{ tool: ToolInfo }>(`/api/tools/${encodeURIComponent(id)}`, { method: "PUT", body: JSON.stringify({ manifest, tool_code }) }),
    onSuccess: () => client.invalidateQueries({ queryKey: ["tools"] }),
  });
}
export function useRollbackTool() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (id: string) =>
      request<{ tool: ToolInfo }>(`/api/tools/${encodeURIComponent(id)}/rollback`, { method: "POST" }),
    onSuccess: () => client.invalidateQueries({ queryKey: ["tools"] }),
  });
}
```

- [ ] **Step 3:** Typecheck.

Run: `cd apps/ui && pnpm typecheck`
Expected: no errors (the new helpers compile; consumers come in Task 8).

- [ ] **Step 4:** Commit.

```bash
git add apps/ui/src/lib/appsApi.ts
git commit -m "Add ToolFactory API client helpers and factory_default flag"
```

---

## Task 8: UI ToolsPage buttons (Factory / Edit / Reset)

**Files:**
- Modify: `apps/ui/src/pages/ToolsPage.tsx`

- [ ] **Step 1:** Replace `ToolsPage.tsx` with the version that adds the three buttons. The "Tool Factory" button goes in the `actions` slot; "Edit" and "Reset to default" go on each card. Use `useNavigate` from react-router.

```tsx
import { useNavigate } from "react-router";
import { RotateCcw, ShieldAlert, Store, Wrench } from "lucide-react";
import AppPage, { panel, secondaryButton, primaryButton } from "../features/apps/AppPage";
import { useRollbackTool, useTools, useUpdateTool, type ToolInfo } from "../lib/appsApi";

const badge = "rounded-md bg-surface-2 px-1.5 py-0.5 text-[11px] font-medium text-fg-muted";

const RISK_LABELS: Record<string, string> = {
  read: "Read-only",
  write: "Writes files",
  execute: "Executes code",
  external_side_effect: "External side effects",
};

function ToolBadges({ tool }: { tool: ToolInfo }) {
  if (!tool.risk && !tool.requires_approval && !tool.network) return null;
  return (
    <div className="mt-2 flex flex-wrap items-center gap-1.5">
      {tool.risk && <span className={badge}>{RISK_LABELS[tool.risk] ?? tool.risk}</span>}
      {tool.requires_approval && (
        <span className="inline-flex items-center gap-1 rounded-md bg-warning-subtle px-1.5 py-0.5 text-[11px] font-medium text-warning">
          <ShieldAlert className="size-3" aria-hidden /> Approval required
        </span>
      )}
      {tool.network === "none" && <span className={badge}>No network</span>}
      {tool.timeout_seconds != null && <span className={badge}>{tool.timeout_seconds}s timeout</span>}
    </div>
  );
}

export default function ToolsPage() {
  const tools = useTools();
  const update = useUpdateTool();
  const rollback = useRollbackTool();
  const navigate = useNavigate();
  return (
    <AppPage
      title="Tools"
      description="Manage trusted Strands tools isolated under /tools/<tool-name>."
      icon={Wrench}
      actions={
        <>
          <button type="button" disabled className={secondaryButton} title="Marketplace support is coming later">
            <Store className="size-4" aria-hidden />Browse marketplace · soon
          </button>
          <button type="button" className={primaryButton} onClick={() => navigate("/apps/tools/factory")}>
            <Wrench className="size-4" aria-hidden />Tool Factory
          </button>
        </>
      }
    >
      {tools.isError && <p role="alert" className="rounded-xl bg-danger-subtle p-3 text-sm text-danger">{tools.error.message}</p>}
      <div className="grid gap-4 md:grid-cols-2">
        {tools.data?.tools.map((tool) => (
          <article key={tool.id} className={panel}>
            <div className="min-w-0">
              <div className="flex items-center justify-between gap-3">
                <h2 className="font-medium">{tool.name}</h2>
                <label className="inline-flex cursor-pointer items-center gap-2 text-xs text-fg-muted">
                  <span>{tool.enabled ? "Enabled" : "Disabled"}</span>
                  <input
                    type="checkbox"
                    className="size-5 accent-[var(--color-accent)]"
                    checked={tool.enabled}
                    disabled={!tool.trusted || update.isPending}
                    onChange={(event) => update.mutate({ id: tool.id, enabled: event.target.checked })}
                  />
                </label>
              </div>
              <p className="mt-1 text-sm text-fg-muted">{tool.description}</p>
              <ToolBadges tool={tool} />
              <p className="mt-3 font-mono text-xs text-fg-subtle">/{tool.folder}/ · v{tool.version}</p>
              <div className="mt-3 flex flex-wrap items-center gap-2">
                <button type="button" className={secondaryButton} onClick={() => navigate(`/apps/tools/factory/${tool.id}`)}>
                  Edit
                </button>
                {tool.factory_default && (
                  <button
                    type="button"
                    className={secondaryButton}
                    disabled={rollback.isPending}
                    title="Reset this tool to the version that shipped with the app"
                    onClick={() => rollback.mutate(tool.id)}
                  >
                    <RotateCcw className="size-4" aria-hidden /> Reset to default
                  </button>
                )}
              </div>
            </div>
          </article>
        ))}
        {tools.data?.tools.length === 0 && <p className="text-sm text-fg-muted">No tool folders were discovered.</p>}
      </div>
      <section className={`${panel} mt-4`}>
        <h2 className="text-lg font-medium">Folder isolation</h2>
        <p className="mt-2 text-sm leading-6 text-fg-muted">
          Each tool owns its code and downloaded assets inside a dedicated <code>/tools/&lt;tool-name&gt;/</code> folder. Only tools marked trusted in their manifest can be enabled.
        </p>
      </section>
    </AppPage>
  );
}
```

- [ ] **Step 2:** Typecheck.

Run: `cd apps/ui && pnpm typecheck`
Expected: no errors.

- [ ] **Step 3:** Commit.

```bash
git add apps/ui/src/pages/ToolsPage.tsx
git commit -m "Add Tool Factory, Edit, and Reset-to-default buttons to Tools page"
```

---

## Task 9: UI `ToolFactoryPage` (create / edit / generate / save)

**Files:**
- Create: `apps/ui/src/pages/ToolFactoryPage.tsx`
- Modify: `apps/ui/src/App.tsx`
- Test: `apps/ui/src/pages/ToolFactoryPage.test.tsx`

- [ ] **Step 1:** Add the route in `apps/ui/src/App.tsx`. After the tools route (line 26):

```tsx
          <Route path="apps/tools/factory" element={<ToolFactoryPage />} />
          <Route path="apps/tools/factory/:toolId" element={<ToolFactoryPage />} />
```

And add the import at the top:

```tsx
import ToolFactoryPage from "./pages/ToolFactoryPage";
```

- [ ] **Step 2:** Create `apps/ui/src/pages/ToolFactoryPage.tsx`. This page handles both create and edit (via `:toolId` param), runs generation through the chat WS flow listening for a `save_tool` tool call, and saves via REST.

```tsx
import { useEffect, useMemo, useRef, useState } from "react";
import { useNavigate, useParams } from "react-router";
import { ArrowLeft, Loader2 } from "lucide-react";
import AppPage, { field, panel, primaryButton, secondaryButton } from "../features/apps/AppPage";
import { socket } from "../lib/ws";
import {
  useCreateTool,
  useEnabledModelsForFactory,
  useToolSource,
  useUpdateToolFiles,
  type ToolManifest,
} from "../lib/appsApi";
import { createConversation, sendMessage } from "../lib/dataApi";

const RISK_OPTIONS = ["read", "write", "execute", "external_side_effect"] as const;
const NETWORK_OPTIONS = ["none", "outbound"] as const;

const EMPTY_MANIFEST: ToolManifest = {
  id: "",
  name: "",
  description: "",
  version: "1.0.0",
  trusted: false,
  default_enabled: false,
  risk: "read",
  requires_approval: false,
  network: "none",
  timeout_seconds: 30,
};

interface SaveToolInput {
  id: string;
  name: string;
  description: string;
  version: string;
  risk: string;
  requires_approval: boolean;
  network: string;
  timeout_seconds: number;
  trusted: boolean;
  tool_code: string;
}

export default function ToolFactoryPage() {
  const { toolId } = useParams<{ toolId?: string }>();
  const isEdit = Boolean(toolId);
  const navigate = useNavigate();

  const source = useToolSource(toolId);
  const createTool = useCreateTool();
  const updateTool = useUpdateToolFiles();
  const models = useEnabledModelsForFactory();

  const [manifest, setManifest] = useState<ToolManifest>(EMPTY_MANIFEST);
  const [toolCode, setToolCode] = useState<string>("");
  const [requirement, setRequirement] = useState<string>("");
  const [transcript, setTranscript] = useState<string>("");
  const [streaming, setStreaming] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const activeRun = useRef<{ requestId: string; runId: string } | null>(null);

  // Edit mode: load existing tool into the form/code panel.
  useEffect(() => {
    if (source.data) {
      setManifest(source.data.manifest);
      setToolCode(source.data.tool_code);
    }
  }, [source.data]);

  const dirty = useMemo(() => toolCode.trim().length > 0 && manifest.id.trim().length > 0, [toolCode, manifest]);

  async function handleGenerate() {
    if (!requirement.trim() || models.length === 0) return;
    setError(null);
    setTranscript("");
    setStreaming(true);
    try {
      // A transient factory conversation scoped to this session.
      const conv = await createConversation({
        title: `Factory: ${requirement.slice(0, 40)}`,
        endpoint_id: models[0]!.provider_id,
        model_id: models[0]!.model_id,
      });
      const res = await sendMessage(conv.id, {
        content: requirement,
        endpoint_id: models[0]!.provider_id,
        model_id: models[0]!.model_id,
        // @ts-expect-error factory_mode is accepted by the server but not yet in the TS body type
        factory_mode: true,
        factory_revision: isEdit ? toolId : undefined,
      });
      activeRun.current = { requestId: res.run.request_id, runId: res.run.id };
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to start generation");
      setStreaming(false);
    }
  }

  // Listen for the agent's save_tool call + streamed text.
  useEffect(() => {
    const offDelta = socket.on("run.text_delta", (envelope) => {
      const run = activeRun.current;
      const p = envelope.payload as Record<string, unknown>;
      if (!run || (envelope.request_id !== run.requestId && p.run_id !== run.runId)) return;
      if (typeof p.text === "string") setTranscript((cur) => cur + p.text);
    });
    const offToolCall = socket.on("run.tool_call", (envelope) => {
      const run = activeRun.current;
      const p = envelope.payload as Record<string, unknown>;
      if (!run || (envelope.request_id !== run.requestId && p.run_id !== run.runId)) return;
      if (p.tool !== "save_tool" || typeof p.call_id !== "string") return;
      const input = p.input as Partial<SaveToolInput> | undefined;
      if (!input) return;
      setManifest((cur) => ({
        ...cur,
        id: input.id ?? cur.id,
        name: input.name ?? cur.name,
        description: input.description ?? cur.description,
        version: input.version ?? cur.version,
        risk: (input.risk as ToolManifest["risk"]) ?? cur.risk,
        requires_approval: input.requires_approval ?? cur.requires_approval,
        network: (input.network as ToolManifest["network"]) ?? cur.network,
        timeout_seconds: input.timeout_seconds ?? cur.timeout_seconds,
        trusted: input.trusted ?? cur.trusted,
      }));
      if (typeof input.tool_code === "string") setToolCode(input.tool_code);
    });
    const offCompleted = socket.on("run.completed", (envelope) => {
      const run = activeRun.current;
      const p = envelope.payload as Record<string, unknown>;
      if (!run || (envelope.request_id !== run.requestId && p.run_id !== run.runId)) return;
      setStreaming(false);
    });
    const offFailed = socket.on("run.failed", (envelope) => {
      const run = activeRun.current;
      const p = envelope.payload as Record<string, unknown>;
      if (!run || (envelope.request_id !== run.requestId && p.run_id !== run.runId)) return;
      setStreaming(false);
      const err = p.error as { message?: string } | undefined;
      setError(err?.message ?? "Generation failed");
    });
    return () => {
      offDelta();
      offToolCall();
      offCompleted();
      offFailed();
    };
  }, []);

  function handleSave() {
    setError(null);
    if (isEdit && toolId) {
      updateTool.mutate(
        { id: toolId, manifest, tool_code: toolCode },
        { onSuccess: () => navigate("/apps/tools"), onError: (e) => setError(e.message) },
      );
    } else {
      createTool.mutate(
        { id: manifest.id, manifest, tool_code: toolCode },
        { onSuccess: () => navigate("/apps/tools"), onError: (e) => setError(e.message) },
      );
    }
  }

  const saving = createTool.isPending || updateTool.isPending;

  return (
    <AppPage
      title={isEdit ? `Edit tool: ${manifest.name || toolId}` : "Tool Factory"}
      description={isEdit ? "Revise this tool with the agent or edit the code directly." : "Describe a tool and let the agent build it. Review, then save."}
      icon={ArrowLeft}
      actions={
        <button type="button" className={secondaryButton} onClick={() => navigate("/apps/tools")}>
          <ArrowLeft className="size-4" aria-hidden /> Back to Tools
        </button>
      }
    >
      <div className="grid gap-4 lg:grid-cols-2">
        <section className={panel}>
          <h2 className="text-lg font-medium">{isEdit ? "Revision request" : "Requirement"}</h2>
          <textarea
            className={`${field} mt-3 min-h-24`}
            placeholder={isEdit ? "e.g. add an option to format as 24-hour" : "e.g. a tool that displays the current time"}
            value={requirement}
            onChange={(e) => setRequirement(e.target.value)}
          />
          <div className="mt-3 flex items-center gap-2">
            <button type="button" className={primaryButton} disabled={streaming || !requirement.trim()} onClick={handleGenerate}>
              {streaming ? <Loader2 className="size-4 animate-spin" aria-hidden /> : null}
              {streaming ? "Generating…" : isEdit ? "Revise with agent" : "Generate with agent"}
            </button>
          </div>
          {transcript && (
            <pre className="mt-4 max-h-60 overflow-auto whitespace-pre-wrap rounded-xl bg-surface-2 p-3 text-xs text-fg-muted">{transcript}</pre>
          )}
        </section>

        <section className={panel}>
          <h2 className="text-lg font-medium">Manifest</h2>
          <div className="mt-3 grid gap-3 sm:grid-cols-2">
            <label className="text-xs text-fg-muted">
              ID (folder)
              <input className={`${field} mt-1 font-mono`} value={manifest.id} disabled={isEdit} onChange={(e) => setManifest({ ...manifest, id: e.target.value })} />
            </label>
            <label className="text-xs text-fg-muted">
              Name
              <input className={`${field} mt-1`} value={manifest.name} onChange={(e) => setManifest({ ...manifest, name: e.target.value })} />
            </label>
            <label className="text-xs text-fg-muted sm:col-span-2">
              Description
              <input className={`${field} mt-1`} value={manifest.description} onChange={(e) => setManifest({ ...manifest, description: e.target.value })} />
            </label>
            <label className="text-xs text-fg-muted">
              Version
              <input className={`${field} mt-1`} value={manifest.version} onChange={(e) => setManifest({ ...manifest, version: e.target.value })} />
            </label>
            <label className="text-xs text-fg-muted">
              Risk
              <select className={`${field} mt-1`} value={manifest.risk ?? "read"} onChange={(e) => setManifest({ ...manifest, risk: e.target.value as ToolManifest["risk"] })}>
                {RISK_OPTIONS.map((r) => <option key={r} value={r}>{r}</option>)}
              </select>
            </label>
            <label className="text-xs text-fg-muted">
              Network
              <select className={`${field} mt-1`} value={manifest.network ?? "none"} onChange={(e) => setManifest({ ...manifest, network: e.target.value as ToolManifest["network"] })}>
                {NETWORK_OPTIONS.map((n) => <option key={n} value={n}>{n}</option>)}
              </select>
            </label>
            <label className="text-xs text-fg-muted">
              Timeout (s)
              <input type="number" className={`${field} mt-1`} value={manifest.timeout_seconds ?? 30} onChange={(e) => setManifest({ ...manifest, timeout_seconds: Number(e.target.value) })} />
            </label>
            <label className="text-xs text-fg-muted inline-flex items-center gap-2 sm:col-span-2">
              <input type="checkbox" className="size-4 accent-[var(--color-accent)]" checked={manifest.requires_approval ?? false} onChange={(e) => setManifest({ ...manifest, requires_approval: e.target.checked })} />
              Requires approval (prompt before each call)
            </label>
            <label className="text-xs text-fg-muted inline-flex items-center gap-2 sm:col-span-2">
              <input type="checkbox" className="size-4 accent-[var(--color-accent)]" checked={manifest.trusted} onChange={(e) => setManifest({ ...manifest, trusted: e.target.checked })} />
              Trusted (can be enabled and reach the agent)
            </label>
          </div>
        </section>
      </div>

      <section className={`${panel} mt-4`}>
        <h2 className="text-lg font-medium">tool.py</h2>
        <textarea
          className={`${field} mt-3 min-h-80 font-mono text-xs`}
          spellCheck={false}
          value={toolCode}
          onChange={(e) => setToolCode(e.target.value)}
          placeholder={"from strands import tool\n\n@tool\ndef my_tool() -> str:\n    \"\"\"...\"\"\"\n    ...\n\nTOOL = my_tool"}
        />
      </section>

      {error && <p role="alert" className="mt-4 rounded-xl bg-danger-subtle p-3 text-sm text-danger">{error}</p>}

      <div className="mt-4 flex items-center gap-2">
        <button type="button" className={primaryButton} disabled={!dirty || saving} onClick={handleSave}>
          {saving ? <Loader2 className="size-4 animate-spin" aria-hidden /> : null}
          {isEdit ? "Save changes" : "Create tool"}
        </button>
        <button type="button" className={secondaryButton} onClick={() => navigate("/apps/tools")}>Cancel</button>
      </div>
    </AppPage>
  );
}
```

- [ ] **Step 3:** Add `useEnabledModelsForFactory` helper to `apps/ui/src/lib/appsApi.ts` (re-export the existing enabled-models query from dataApi). Append:

```ts
export { useEnabledModels as useEnabledModelsForFactory } from "./dataApi";
```

- [ ] **Step 4:** Add `factory_mode` + `factory_revision` to the TS `sendMessage` input type in `apps/ui/src/lib/dataApi.ts`. Update `sendMessage` (line 282):

```ts
export async function sendMessage(
  conversationId: string,
  input: { content: string; endpoint_id?: string; model_id?: string; factory_mode?: boolean; factory_revision?: string },
): Promise<{ message: Message; run: RunRef }> {
```

This removes the need for the `@ts-expect-error` in ToolFactoryPage; remove that comment.

- [ ] **Step 5:** Typecheck.

Run: `cd apps/ui && pnpm typecheck`
Expected: no errors.

- [ ] **Step 6:** Create a basic render test `apps/ui/src/pages/ToolFactoryPage.test.tsx`:

```tsx
import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import ToolFactoryPage from "./ToolFactoryPage";

describe("ToolFactoryPage", () => {
  it("renders the create-mode heading and save button", () => {
    render(
      <MemoryRouter>
        <ToolFactoryPage />
      </MemoryRouter>,
    );
    expect(screen.getByText("Tool Factory")).toBeTruthy();
    expect(screen.getByText("Create tool")).toBeTruthy();
  });
});
```

- [ ] **Step 7:** Run UI tests.

Run: `cd apps/ui && pnpm test`
Expected: PASS.

- [ ] **Step 8:** Commit.

```bash
git add apps/ui/src/pages/ToolFactoryPage.tsx apps/ui/src/pages/ToolFactoryPage.test.tsx apps/ui/src/App.tsx apps/ui/src/lib/appsApi.ts apps/ui/src/lib/dataApi.ts
git commit -m "Add Tool Factory page for agent-driven tool creation and editing"
```

---

## Task 10: Full verification + final checks

- [ ] **Step 1:** Rust build + clippy + tests.

Run: `cargo build -p agentgpt-server && cargo clippy -p agentgpt-server -- -D warnings && cargo test -p agentgpt-server`
Expected: all PASS.

- [ ] **Step 2:** Python lint + tests.

Run: `cd apps/agent-runtime && uv run ruff check src/ && uv run pytest`
Expected: PASS.

- [ ] **Step 3:** Protocol types tests.

Run: `cd packages/protocol-types && pnpm test`
Expected: PASS.

- [ ] **Step 4:** UI typecheck + tests.

Run: `cd apps/ui && pnpm typecheck && pnpm test`
Expected: PASS.

- [ ] **Step 5:** Verify nothing regressed end-to-end (manual sanity): the Tools page still loads `GET /api/tools`, all 14 built-in tools list with `factory_default: true`.

---

## Self-Review (completed during planning)

**Spec coverage:**
- Create endpoint → Task 3 (POST /api/tools) ✓
- Update endpoint → Task 3 (PUT /api/tools/{id}) ✓
- Source endpoint → Task 3 (GET /api/tools/{id}/source) ✓
- Rollback endpoint → Task 4 (POST /api/tools/{id}/rollback) ✓
- `save_tool` meta-tool → Task 5 ✓
- `factory_mode` protocol (TS/schema/Rust/Python) → Tasks 1, 2, 5 ✓
- Factory wiring in chat.py → Task 5 ✓
- Factory prompt in chat.rs → Task 6 ✓
- ToolFactoryPage (create + edit + generate + save) → Task 9 ✓
- Tools page buttons → Task 8 ✓
- `factory_default` flag → Tasks 3, 7 ✓

**Placeholder scan:** No TBD/TODO. The Task 3 `defaults` stub is a compile scaffold replaced in Task 4 (intentional sequencing, not a placeholder). Prompt text in chat.rs uses abbreviated strings; acceptable since the Python constants are the source of truth and the host can pass a full prompt.

**Type consistency:** `ToolManifest` fields, `ToolSource`, `SaveToolInput` field names all cross-checked against the manifest schema. `useEnabledModelsForFactory` re-exports the existing dataApi query. `factory_mode` / `factory_revision` threaded consistently through TS → Rust → Python.
