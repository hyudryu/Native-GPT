//! Discover and manage Strands tools stored under `/tools/<tool-id>/`.

use std::path::Path;

use axum::extract::{Path as AxumPath, State};
use axum::http::StatusCode;
use axum::Json;
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};

use crate::error::ApiError;
use crate::state::SharedState;

#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct ToolManifest {
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

#[derive(Debug, Deserialize)]
pub struct UpdateTool {
    enabled: bool,
}

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
    // Clamp the timeout hint before writing so the persisted value matches
    // what we return to the caller.
    manifest.timeout_seconds = manifest.timeout_seconds.map(|v| v.min(86_400));
    let json = serde_json::to_string_pretty(&manifest)
        .map_err(|e| ApiError::internal(format!("failed to serialize manifest: {e}")))?;
    std::fs::write(dir.join("manifest.json"), format!("{json}\n"))
        .map_err(|e| ApiError::internal(e.to_string()))?;
    std::fs::write(dir.join("tool.py"), tool_code)
        .map_err(|e| ApiError::internal(e.to_string()))?;
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
    Ok(ToolSource {
        manifest,
        tool_code,
    })
}

/// Public wrapper for use by `chat::factory_system_prompt` (revision mode).
pub fn read_tool_source_public(state: &SharedState, id: &str) -> Result<ToolSource, ApiError> {
    read_tool_source(&state.repo_root, id)
}

/// Whether a tool id ships with the app (rollback-eligible). Backed by the
/// embedded built-in bundle (see `defaults` module).
fn is_factory_default(id: &str) -> bool {
    crate::defaults::is_bundled(id)
}

fn valid_id(value: &str) -> bool {
    !value.is_empty()
        && value
            .bytes()
            .all(|byte| byte.is_ascii_lowercase() || byte.is_ascii_digit() || byte == b'-')
}

fn manifests(repo_root: &Path) -> Result<Vec<(ToolManifest, String)>, ApiError> {
    let root = repo_root.join("tools");
    if !root.is_dir() {
        return Ok(Vec::new());
    }
    let entries = std::fs::read_dir(&root).map_err(|error| {
        ApiError::internal(format!("failed to read {}: {error}", root.display()))
    })?;
    let mut tools = Vec::new();
    for entry in entries {
        let entry = entry.map_err(|error| ApiError::internal(error.to_string()))?;
        let path = entry.path();
        if !path.is_dir()
            || !path.join("manifest.json").is_file()
            || !path.join("tool.py").is_file()
        {
            continue;
        }
        let manifest: ToolManifest = serde_json::from_str(
            &std::fs::read_to_string(path.join("manifest.json"))
                .map_err(|error| ApiError::internal(error.to_string()))?,
        )
        .map_err(|error| ApiError::internal(format!("invalid tool manifest: {error}")))?;
        let folder_name = path
            .file_name()
            .and_then(|value| value.to_str())
            .unwrap_or_default();
        if !valid_id(&manifest.id) || manifest.id != folder_name {
            return Err(ApiError::internal(format!(
                "tool manifest id must match its folder: {}",
                path.display()
            )));
        }
        tools.push((manifest, format!("tools/{folder_name}")));
    }
    tools.sort_by_key(|entry| entry.0.name.to_lowercase());
    Ok(tools)
}

pub async fn list_for_state(state: &SharedState) -> Result<Vec<ToolInfo>, ApiError> {
    let mut result = Vec::new();
    for (manifest, folder) in manifests(&state.repo_root)? {
        let enabled = state
            .db
            .tool_enabled(&manifest.id, manifest.default_enabled)
            .await?;
        let factory_default = is_factory_default(&manifest.id);
        result.push(ToolInfo {
            id: manifest.id,
            name: manifest.name,
            description: manifest.description,
            version: manifest.version,
            trusted: manifest.trusted,
            enabled: enabled && manifest.trusted,
            folder,
            risk: manifest.risk,
            requires_approval: manifest.requires_approval,
            network: manifest.network,
            timeout_seconds: manifest.timeout_seconds,
            factory_default,
        });
    }
    Ok(result)
}

pub async fn enabled_tool_ids(state: &SharedState) -> Result<Vec<String>, ApiError> {
    Ok(list_for_state(state)
        .await?
        .into_iter()
        .filter(|tool| tool.enabled && tool.trusted)
        .map(|tool| tool.id)
        .collect())
}

pub async fn list(State(state): State<SharedState>) -> Result<Json<Value>, ApiError> {
    Ok(Json(json!({ "tools": list_for_state(&state).await? })))
}

pub async fn patch(
    State(state): State<SharedState>,
    AxumPath(id): AxumPath<String>,
    Json(body): Json<UpdateTool>,
) -> Result<Json<Value>, ApiError> {
    let available = manifests(&state.repo_root)?;
    let Some((manifest, _)) = available
        .into_iter()
        .find(|(manifest, _)| manifest.id == id)
    else {
        return Err(ApiError::not_found(format!("tool {id} not found")));
    };
    if body.enabled && !manifest.trusted {
        return Err(ApiError::bad_request("untrusted tools cannot be enabled"));
    }
    state.db.set_tool_enabled(&id, body.enabled).await?;
    let tool = list_for_state(&state)
        .await?
        .into_iter()
        .find(|tool| tool.id == id)
        .ok_or_else(|| ApiError::not_found(format!("tool {id} not found")))?;
    Ok(Json(json!({ "tool": tool })))
}

pub async fn source(
    State(state): State<SharedState>,
    AxumPath(id): AxumPath<String>,
) -> Result<Json<Value>, ApiError> {
    let src = read_tool_source(&state.repo_root, &id)?;
    Ok(Json(
        json!({ "manifest": src.manifest, "tool_code": src.tool_code }),
    ))
}

pub async fn create(
    State(state): State<SharedState>,
    Json(body): Json<CreateTool>,
) -> Result<(StatusCode, Json<Value>), ApiError> {
    let manifest = write_tool_files(
        &state.repo_root,
        &body.id,
        body.manifest,
        &body.tool_code,
        true,
    )?;
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
    write_tool_files(
        &state.repo_root,
        &id,
        manifest_value,
        &body.tool_code,
        false,
    )?;
    let tool = list_for_state(&state)
        .await?
        .into_iter()
        .find(|tool| tool.id == id)
        .ok_or_else(|| ApiError::not_found(format!("tool {id} not found")))?;
    Ok(Json(json!({ "tool": tool })))
}

pub async fn rollback(
    State(state): State<SharedState>,
    AxumPath(id): AxumPath<String>,
) -> Result<Json<Value>, ApiError> {
    if !valid_id(&id) {
        return Err(ApiError::bad_request("invalid tool id"));
    }
    crate::defaults::restore(&state.repo_root, &id).map_err(|e| match e {
        crate::defaults::RestoreError::NotBuiltin(_) => ApiError::conflict(e.to_string()),
        crate::defaults::RestoreError::Internal(_) => ApiError::internal(e.to_string()),
    })?;
    let tool = list_for_state(&state)
        .await?
        .into_iter()
        .find(|tool| tool.id == id)
        .ok_or_else(|| ApiError::not_found(format!("tool {id} not found")))?;
    Ok(Json(json!({ "tool": tool })))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn ids_are_folder_safe() {
        assert!(valid_id("current-time"));
        assert!(!valid_id("../escape"));
        assert!(!valid_id("Uppercase"));
        assert!(!valid_id("has space"));
    }

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
            root,
            "clock",
            serde_json::json!({"id":"clock","name":"Clock","description":"x","version":"1.0.0"}),
            "x",
            true,
        )
        .unwrap_err();
        assert_eq!(err.status, axum::http::StatusCode::CONFLICT);
        // Update overwrites.
        write_tool_files(
            root,
            "clock",
            serde_json::json!({"id":"clock","name":"Clock2","description":"y","version":"1.1.0"}),
            "TOOL = None\n",
            false,
        )
        .unwrap();
        let src = read_tool_source(root, "clock").unwrap();
        assert_eq!(src.manifest.name, "Clock2");
        assert_eq!(src.tool_code, "TOOL = None\n");
    }

    #[test]
    fn rejects_bad_id_and_mismatched_manifest() {
        let dir = tempfile::tempdir().unwrap();
        let err = write_tool_files(
            dir.path(),
            "Bad Id",
            serde_json::json!({"id":"Bad Id","name":"x","description":"x","version":"1.0.0"}),
            "x",
            true,
        )
        .unwrap_err();
        assert_eq!(err.status, axum::http::StatusCode::BAD_REQUEST);
        let err = write_tool_files(
            dir.path(),
            "clock",
            serde_json::json!({"id":"other","name":"x","description":"x","version":"1.0.0"}),
            "x",
            true,
        )
        .unwrap_err();
        assert_eq!(err.status, axum::http::StatusCode::BAD_REQUEST);
    }

    #[test]
    fn clamps_timeout_before_writing_to_disk() {
        let dir = tempfile::tempdir().unwrap();
        let root = dir.path();
        let written = write_tool_files(
            root,
            "clock",
            serde_json::json!({
                "id": "clock", "name": "Clock", "description": "x", "version": "1.0.0",
                "timeout_seconds": 999_999_999,
            }),
            "TOOL = None\n",
            true,
        )
        .unwrap();
        // Returned value is clamped.
        assert_eq!(written.timeout_seconds, Some(86_400));
        // On-disk value matches the returned (clamped) value.
        let src = read_tool_source(root, "clock").unwrap();
        assert_eq!(src.manifest.timeout_seconds, Some(86_400));
    }
}
