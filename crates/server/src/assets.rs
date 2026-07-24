//! REST handlers for serving generated asset bytes (`/api/assets/{id}`).
//!
//! Asset metadata lives in the `generated_assets` table; the bytes live on
//! disk under `<data_dir>/assets/`. This module resolves the metadata row,
//! reads the file, and streams it back with the stored content type. See
//! ADR-0008 and migration `0006_generated_assets.sql`.

use std::path::PathBuf;

use axum::extract::{Path, State};
use axum::http::{header, StatusCode};
use axum::response::{IntoResponse, Response};
use axum::Json;
use serde_json::json;

use crate::error::ApiError;
use crate::state::SharedState;

/// Resolve the directory holding generated asset files. Honors
/// `AGENTGPT_DATA_DIR` (same precedence as the DB path) and falls back to
/// `<repo_root>/app-data/assets/`.
pub fn assets_dir(repo_root: &std::path::Path) -> PathBuf {
    if let Ok(dir) = std::env::var("AGENTGPT_DATA_DIR") {
        if !dir.trim().is_empty() {
            return PathBuf::from(dir).join("assets");
        }
    }
    repo_root.join("app-data").join("assets")
}

/// `GET /api/assets/{id}` — serve a generated asset's bytes (auth-gated).
pub async fn serve_asset(
    State(state): State<SharedState>,
    Path(id): Path<String>,
) -> Result<Response, ApiError> {
    let row = state
        .db
        .get_asset(&id)
        .await?
        .ok_or_else(|| ApiError::not_found(format!("asset {id} not found")))?;

    let dir = assets_dir(&state.repo_root);
    // Defense-in-depth: reject storage_path values that could escape the assets
    // directory (absolute paths or parent-dir traversal). Current callers only
    // write UUID filenames, but the DB column accepts arbitrary strings.
    let storage_path = std::path::PathBuf::from(&row.storage_path);
    if storage_path.is_absolute()
        || storage_path
            .components()
            .any(|c| matches!(c, std::path::Component::ParentDir))
    {
        return Err(ApiError::not_found(format!("asset {id} not found")));
    }
    let path = dir.join(&storage_path);
    let bytes = tokio::fs::read(&path)
        .await
        .map_err(|e| ApiError::internal(format!("failed to read asset {id}: {e}")))?;

    let content_type = row
        .mime_type
        .clone()
        .unwrap_or_else(|| "application/octet-stream".to_string());

    Ok((
        StatusCode::OK,
        [
            (header::CONTENT_TYPE, content_type),
            (
                header::CACHE_CONTROL,
                "private, max-age=31536000, immutable".to_string(),
            ),
        ],
        bytes,
    )
        .into_response())
}

/// `GET /api/assets/{id}/meta` — return metadata about a generated asset.
pub async fn get_asset_meta(
    State(state): State<SharedState>,
    Path(id): Path<String>,
) -> Result<Json<serde_json::Value>, ApiError> {
    let row = state
        .db
        .get_asset(&id)
        .await?
        .ok_or_else(|| ApiError::not_found(format!("asset {id} not found")))?;
    Ok(Json(json!({
        "id": row.id,
        "host_id": row.host_id,
        "workload": row.workload,
        "kind": row.kind,
        "message_id": row.message_id,
        "prompt_text": row.prompt_text,
        "source_ref": row.source_ref,
        "bytes": row.bytes,
        "mime_type": row.mime_type,
        "created_at": row.created_at,
    })))
}
