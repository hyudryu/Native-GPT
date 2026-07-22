//! Discover and manage Strands tools stored under `/tools/<tool-id>/`.

use std::path::Path;

use axum::extract::{Path as AxumPath, State};
use axum::Json;
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};

use crate::error::ApiError;
use crate::state::SharedState;

#[derive(Debug, Clone, Deserialize)]
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
    #[serde(default)]
    risk: Option<String>,
    /// True when every call must be approved by the user in the UI.
    #[serde(default)]
    requires_approval: Option<bool>,
    /// "none" | "outbound" (informational soft-sandbox policy).
    #[serde(default)]
    network: Option<String>,
    /// Per-tool default execution timeout.
    #[serde(default)]
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
}

#[derive(Debug, Deserialize)]
pub struct UpdateTool {
    enabled: bool,
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
}
