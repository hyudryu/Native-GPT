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

/// Write asset bytes to disk and return the storage-relative path.
///
/// The filename is `<asset_id>.<ext>` to guarantee uniqueness; the full path
/// is `<assets_dir>/<asset_id>.<ext>`. `storage_path` stored in the DB is
/// relative to `assets_dir`.
pub fn write_asset_bytes(
    dir: &std::path::Path,
    asset_id: &str,
    bytes: &[u8],
    mime_type: Option<&str>,
) -> Result<(String, PathBuf), std::io::Error> {
    std::fs::create_dir_all(dir)?;
    // Reject asset_ids that contain path separators or traversal sequences.
    // Current callers generate UUIDs, but this prevents directory escape if a
    // future caller passes user-supplied input.
    if asset_id.contains('/') || asset_id.contains('\\') || asset_id.contains("..") {
        return Err(std::io::Error::new(
            std::io::ErrorKind::InvalidInput,
            "asset_id contains invalid characters",
        ));
    }
    let ext = mime_type.and_then(mime_to_ext).unwrap_or("bin");
    let filename = format!("{asset_id}.{ext}");
    let abs = dir.join(&filename);
    std::fs::write(&abs, bytes)?;
    Ok((filename, abs))
}

/// Best-effort MIME → extension mapping for the asset kinds we produce.
fn mime_to_ext(mime: &str) -> Option<&str> {
    match mime.split(';').next().unwrap_or("").trim() {
        "image/png" => Some("png"),
        "image/jpeg" => Some("jpg"),
        "image/webp" => Some("webp"),
        "image/gif" => Some("gif"),
        "video/mp4" => Some("mp4"),
        "video/webm" => Some("webm"),
        "audio/mpeg" | "audio/mp3" => Some("mp3"),
        "audio/wav" | "audio/wave" | "audio/x-wav" => Some("wav"),
        "audio/ogg" => Some("ogg"),
        "audio/flac" => Some("flac"),
        _ => None,
    }
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

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn mime_to_ext_maps_common_types() {
        assert_eq!(mime_to_ext("image/png"), Some("png"));
        assert_eq!(mime_to_ext("image/jpeg"), Some("jpg"));
        assert_eq!(mime_to_ext("audio/mpeg"), Some("mp3"));
        assert_eq!(mime_to_ext("video/mp4"), Some("mp4"));
        assert_eq!(mime_to_ext("image/png; charset=utf-8"), Some("png"));
        assert_eq!(mime_to_ext("application/octet-stream"), None);
    }

    #[test]
    fn write_and_read_asset_bytes() {
        let dir =
            std::env::temp_dir().join(format!("agentgpt-assets-test-{}", uuid::Uuid::now_v7()));
        let (rel, abs) = write_asset_bytes(&dir, "test-id", b"hello", Some("image/png")).unwrap();
        assert!(rel.starts_with("test-id."));
        assert!(abs.is_file());
        let written = std::fs::read(&abs).unwrap();
        assert_eq!(written, b"hello");
        assert!(abs.extension().unwrap() == "png");
        let _ = std::fs::remove_dir_all(&dir);
    }
}
