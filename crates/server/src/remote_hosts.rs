//! REST handlers for remote backend hosts (`/api/remote-hosts...`).
//!
//! Remote hosts ("bridges") are managed exactly like endpoints: only a boolean
//! `has_token` lives in the row; the raw bearer token is stored in the
//! keychain under key `host:<id>` and resolved only when relaying to the
//! bridge. It never appears in responses or logs. See ADR-0008.
//!
//! Generation/TTS job endpoints were removed when the bridge became an MCP
//! server (see `docs/superpowers/specs/2026-07-22-bridge-mcp-server-design.md`):
//! the agent now calls bridge tools directly via MCP. What remains here is
//! host CRUD + test-connection, the voices passthrough, and a same-origin
//! asset proxy so the webview can render bearer-protected bridge asset URLs.

use axum::body::Bytes;
use axum::extract::{Multipart, Path, State};
use axum::http::{header, StatusCode};
use axum::response::{IntoResponse, Response};
use axum::Json;
use serde::Deserialize;
use serde_json::{json, Value};
use std::time::Instant;

use crate::bridge::{BridgeClient, HealthProbe};
use crate::db::{RemoteHostRow, VoiceRow};
use crate::error::ApiError;
use crate::state::SharedState;

/// Keychain key prefix for remote host tokens (avoids collision with endpoint
/// ids in the flat `agentgpt` service namespace).
pub fn secret_key(host_id: &str) -> String {
    format!("host:{host_id}")
}

// ---- request shapes ----

#[derive(Debug, Deserialize)]
pub struct CreateRemoteHost {
    pub name: String,
    pub base_url: String,
    #[serde(default)]
    pub token: Option<String>,
    #[serde(default)]
    pub tls_verify: Option<bool>,
}

/// Serde helper for tri-state fields: absent = keep (None), null = clear
/// (Some(None)), value = set (Some(Some(v))).
fn deserialize_some<'de, D, T>(de: D) -> Result<Option<T>, D::Error>
where
    D: serde::Deserializer<'de>,
    T: serde::Deserialize<'de>,
{
    T::deserialize(de).map(Some)
}

#[derive(Debug, Deserialize)]
pub struct PatchRemoteHost {
    #[serde(default)]
    pub name: Option<String>,
    #[serde(default)]
    pub base_url: Option<String>,
    /// Tri-state: absent = keep, null = clear, string = set.
    #[serde(default, deserialize_with = "deserialize_some")]
    pub token: Option<Option<String>>,
    #[serde(default)]
    pub tls_verify: Option<bool>,
}

// ---- helpers ----

async fn load_host(state: &SharedState, id: &str) -> Result<RemoteHostRow, ApiError> {
    state
        .db
        .get_remote_host(id)
        .await?
        .ok_or_else(|| ApiError::not_found(format!("remote host {id} not found")))
}

/// Resolve the raw bearer token for a host (never logged).
fn resolve_token(state: &SharedState, host: &RemoteHostRow) -> Option<String> {
    if host.has_token {
        state.secrets.get(&secret_key(&host.id))
    } else {
        None
    }
}

/// Build a `BridgeClient` for a host, resolving its token from the keychain.
pub fn client_for_host(
    state: &SharedState,
    host: &RemoteHostRow,
) -> Result<BridgeClient, ApiError> {
    let token = resolve_token(state, host);
    BridgeClient::new(&host.base_url, token, host.tls_verify)
}

/// Rebuild `app-data/mcp_servers.json` after a remote-hosts mutation so the
/// agent-runtime's MCP client config stays in sync. Best-effort: a write
/// failure is logged but never fails the API request.
async fn sync_mcp_servers_config(state: &SharedState) {
    if let Err(e) = crate::mcp_servers::regenerate(state).await {
        tracing::warn!("failed to regenerate mcp_servers.json: {e}");
    }
}

fn row_json(row: &RemoteHostRow) -> Value {
    let workloads = row
        .workloads_json
        .as_deref()
        .and_then(|s| serde_json::from_str::<Value>(s).ok())
        .unwrap_or(Value::Null);
    json!({
        "id": row.id,
        "name": row.name,
        "base_url": row.base_url,
        "tls_verify": row.tls_verify,
        "has_token": row.has_token,
        "status": row.status,
        "last_checked_at": row.last_checked_at,
        "workloads": workloads,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    })
}

fn voice_json(row: &VoiceRow) -> Value {
    json!({
        "id": row.id,
        "name": row.name,
        "host_id": row.host_id,
        "source_kind": row.source_kind,
        "source_ref": row.source_ref,
        "duration_ms": row.duration_ms,
        "created_at": row.created_at,
        "last_used_at": row.last_used_at,
    })
}

// ---- CRUD handlers ----

/// `GET /api/remote-hosts`
pub async fn list_hosts(State(state): State<SharedState>) -> Result<Json<Value>, ApiError> {
    let hosts = state.db.list_remote_hosts().await?;
    Ok(Json(
        json!({ "hosts": hosts.iter().map(row_json).collect::<Vec<_>>() }),
    ))
}

/// `POST /api/remote-hosts`
pub async fn create_host(
    State(state): State<SharedState>,
    Json(body): Json<CreateRemoteHost>,
) -> Result<(StatusCode, Json<Value>), ApiError> {
    let name = body.name.trim();
    let base_url = body.base_url.trim();
    if name.is_empty() || base_url.is_empty() {
        return Err(ApiError::bad_request("name and base_url are required"));
    }
    let token = body.token.filter(|t| !t.trim().is_empty());
    let now = chrono::Utc::now().to_rfc3339();
    let row = RemoteHostRow {
        id: uuid::Uuid::now_v7().to_string(),
        name: name.to_string(),
        base_url: base_url.to_string(),
        tls_verify: body.tls_verify.unwrap_or(true),
        has_token: token.is_some(),
        status: None,
        last_checked_at: None,
        workloads_json: None,
        created_at: now.clone(),
        updated_at: now,
    };
    if let Some(tok) = &token {
        state
            .secrets
            .set(&secret_key(&row.id), tok)
            .map_err(|e| ApiError::internal(format!("failed to store host token: {e}")))?;
    }
    if let Err(e) = state.db.insert_remote_host(&row).await {
        if token.is_some() {
            let _ = state.secrets.delete(&secret_key(&row.id));
        }
        return Err(e.into());
    }
    sync_mcp_servers_config(&state).await;
    Ok((StatusCode::CREATED, Json(json!({ "host": row_json(&row) }))))
}

/// `PATCH /api/remote-hosts/{id}`
pub async fn patch_host(
    State(state): State<SharedState>,
    Path(id): Path<String>,
    Json(body): Json<PatchRemoteHost>,
) -> Result<Json<Value>, ApiError> {
    let mut row = load_host(&state, &id).await?;
    if let Some(name) = body.name {
        let name = name.trim();
        if name.is_empty() {
            return Err(ApiError::bad_request("name must not be empty"));
        }
        row.name = name.to_string();
    }
    if let Some(base_url) = body.base_url {
        let base_url = base_url.trim();
        if base_url.is_empty() {
            return Err(ApiError::bad_request("base_url must not be empty"));
        }
        row.base_url = base_url.to_string();
    }
    if let Some(tls_verify) = body.tls_verify {
        row.tls_verify = tls_verify;
    }
    match body.token {
        Some(Some(tok)) if !tok.trim().is_empty() => {
            state
                .secrets
                .set(&secret_key(&id), &tok)
                .map_err(|e| ApiError::internal(format!("failed to store host token: {e}")))?;
            row.has_token = true;
        }
        Some(_) => {
            // null or empty string: clear the token.
            state
                .secrets
                .delete(&secret_key(&id))
                .map_err(|e| ApiError::internal(format!("failed to delete host token: {e}")))?;
            row.has_token = false;
        }
        None => {}
    }
    row.updated_at = chrono::Utc::now().to_rfc3339();
    state.db.update_remote_host(&row).await?;
    sync_mcp_servers_config(&state).await;
    Ok(Json(json!({ "host": row_json(&row) })))
}

/// `DELETE /api/remote-hosts/{id}`
pub async fn delete_host(
    State(state): State<SharedState>,
    Path(id): Path<String>,
) -> Result<StatusCode, ApiError> {
    // Collect asset paths before CASCADE deletes the rows.
    let asset_paths = state
        .db
        .list_asset_paths_by_host(&id)
        .await
        .unwrap_or_default();
    if !state.db.delete_remote_host(&id).await? {
        return Err(ApiError::not_found(format!("remote host {id} not found")));
    }
    let _ = state.secrets.delete(&secret_key(&id));
    // Clean up orphaned asset files on disk (best-effort).
    let dir = crate::assets::assets_dir(&state.repo_root);
    for rel_path in &asset_paths {
        let abs = dir.join(rel_path);
        let _ = std::fs::remove_file(&abs);
    }
    sync_mcp_servers_config(&state).await;
    Ok(StatusCode::NO_CONTENT)
}

/// `POST /api/remote-hosts/{id}/test` — probe the bridge `/health` endpoint
/// and cache the capability snapshot.
pub async fn test_host(
    State(state): State<SharedState>,
    Path(id): Path<String>,
) -> Result<Json<Value>, ApiError> {
    let row = load_host(&state, &id).await?;
    let client = client_for_host(&state, &row)?;
    let started = Instant::now();
    let probe = client.probe_health().await;
    let checked_at = chrono::Utc::now().to_rfc3339();
    match probe {
        HealthProbe::Reachable(health) => {
            let workloads_json = serde_json::to_string(&health.workloads).unwrap_or_default();
            state
                .db
                .update_remote_host_status(&id, "reachable", Some(&workloads_json), &checked_at)
                .await?;
            Ok(Json(json!({
                "ok": true,
                "latency_ms": started.elapsed().as_secs_f64() * 1000.0,
                "version": health.version,
                "workloads": health.workloads,
                "checked_at": checked_at,
            })))
        }
        HealthProbe::Unreachable(message) => {
            state
                .db
                .update_remote_host_status(&id, "unreachable", None, &checked_at)
                .await?;
            Ok(Json(json!({
                "ok": false,
                "latency_ms": started.elapsed().as_secs_f64() * 1000.0,
                "error": { "code": "unreachable", "message": message },
                "checked_at": checked_at,
            })))
        }
    }
}

// ---- voices passthrough ----

/// `GET /api/remote-hosts/{host_id}/voices` — list voices (DB metadata).
pub async fn list_voices(
    State(state): State<SharedState>,
    Path(host_id): Path<String>,
) -> Result<Json<Value>, ApiError> {
    load_host(&state, &host_id).await?;
    let voices = state.db.list_voices(&host_id).await?;
    Ok(Json(
        json!({ "voices": voices.iter().map(voice_json).collect::<Vec<_>>() }),
    ))
}

/// `POST /api/remote-hosts/{host_id}/voices` — multipart upload: clip + name.
/// Streams the clip to the bridge (which extracts the embedding) and records
/// a `voices` row on success.
pub async fn upload_voice(
    State(state): State<SharedState>,
    Path(host_id): Path<String>,
    mut multipart: Multipart,
) -> Result<(StatusCode, Json<Value>), ApiError> {
    let row = load_host(&state, &host_id).await?;
    let mut name: Option<String> = None;
    let mut clip: Option<(Bytes, String, String)> = None; // (bytes, filename, mime)
    while let Some(field) = multipart
        .next_field()
        .await
        .map_err(|e| ApiError::bad_request(format!("invalid multipart: {e}")))?
    {
        let fname = field.name().unwrap_or("").to_string();
        match fname.as_str() {
            "name" => {
                name = Some(
                    field
                        .text()
                        .await
                        .map_err(|e| ApiError::bad_request(format!("invalid name field: {e}")))?,
                );
            }
            "clip" => {
                let filename = field.file_name().unwrap_or("clip.mp3").to_string();
                let mime = field.content_type().unwrap_or("audio/mpeg").to_string();
                let bytes = field
                    .bytes()
                    .await
                    .map_err(|e| ApiError::bad_request(format!("invalid clip field: {e}")))?;
                clip = Some((bytes, filename, mime));
            }
            _ => { /* ignore unknown fields */ }
        }
    }
    let name = name
        .filter(|n| !n.trim().is_empty())
        .ok_or_else(|| ApiError::bad_request("name field is required"))?;
    let (clip_bytes, filename, mime) =
        clip.ok_or_else(|| ApiError::bad_request("clip file field is required"))?;

    let client = client_for_host(&state, &row)?;
    let bridge_resp = client
        .upload_voice(&name, clip_bytes.to_vec(), &filename, &mime)
        .await?;
    let voice_id = bridge_resp
        .get("voice_id")
        .and_then(|v| v.as_str())
        .ok_or_else(|| {
            ApiError::bad_gateway("bad_bridge_response", "bridge did not return voice_id")
        })?
        .to_string();
    let duration_ms = bridge_resp.get("duration_ms").and_then(|v| v.as_i64());

    let now = chrono::Utc::now().to_rfc3339();
    let voice_row = VoiceRow {
        id: voice_id.clone(),
        name: name.clone(),
        host_id: host_id.clone(),
        source_kind: "file".to_string(),
        source_ref: Some(filename),
        duration_ms,
        created_at: now,
        last_used_at: None,
    };
    state.db.insert_voice(&voice_row).await?;
    Ok((
        StatusCode::CREATED,
        Json(json!({ "voice": voice_json(&voice_row) })),
    ))
}

/// `DELETE /api/remote-hosts/{host_id}/voices/{voice_id}` — remove from the
/// bridge (best-effort) and delete the local row.
pub async fn delete_voice(
    State(state): State<SharedState>,
    Path((host_id, voice_id)): Path<(String, String)>,
) -> Result<StatusCode, ApiError> {
    let row = load_host(&state, &host_id).await?;
    // Delete on the bridge first (best-effort — if it's already gone, ignore).
    let client = client_for_host(&state, &row)?;
    let _ = client.delete_voice(&voice_id).await;
    if !state.db.delete_voice(&voice_id).await? {
        return Err(ApiError::not_found(format!("voice {voice_id} not found")));
    }
    Ok(StatusCode::NO_CONTENT)
}

/// `GET /api/remote-hosts/{host_id}/assets/{token}` — proxy a bridge asset
/// through the desktop server.
///
/// MCP generation tools return bridge-direct asset URLs
/// (`https://<host>:8443/assets/<token>`) that the webview cannot load: the
/// bridge requires a bearer token and may present a self-signed certificate.
/// The UI rewrites those URLs to this same-origin route; we fetch the bytes
/// server-side with the host's keychain token and TLS policy, and stream them
/// back with the bridge's Content-Type.
pub async fn proxy_asset(
    State(state): State<SharedState>,
    Path((host_id, token)): Path<(String, String)>,
) -> Result<Response, ApiError> {
    let row = load_host(&state, &host_id).await?;
    let client = client_for_host(&state, &row)?;
    let (bytes, detected_mime) = client
        .fetch_asset_bytes(&token, None)
        .await
        .map_err(|e| e.into_api())?;
    let content_type = detected_mime.unwrap_or_else(|| "application/octet-stream".to_string());
    Ok((
        StatusCode::OK,
        [(header::CONTENT_TYPE, content_type)],
        bytes,
    )
        .into_response())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::secrets::KeyStore;
    use axum::body::Body;
    use axum::extract::ConnectInfo;
    use axum::http::{header, Request};
    use axum::Router;
    use http_body_util::BodyExt;
    use std::net::SocketAddr;
    use tower::ServiceExt;

    struct Rig {
        test_state: crate::state::TestState,
        app: Router,
    }

    fn rig() -> Rig {
        let test_state = crate::state::test_state("tok");
        let app = crate::build_router(test_state.state.clone());
        Rig { test_state, app }
    }

    fn request(method: &str, path: &str, body: Option<Value>) -> Request<Body> {
        let mut builder = Request::builder().method(method).uri(path);
        if body.is_some() {
            builder = builder.header(header::CONTENT_TYPE, "application/json");
        }
        let body = match body {
            Some(v) => Body::from(serde_json::to_vec(&v).unwrap()),
            None => Body::empty(),
        };
        let mut req = builder.body(body).unwrap();
        req.extensions_mut()
            .insert(ConnectInfo(SocketAddr::from(([127, 0, 0, 1], 40_000))));
        req
    }

    async fn json_response(res: axum::response::Response) -> (StatusCode, Value) {
        let status = res.status();
        let bytes = res.into_body().collect().await.unwrap().to_bytes();
        let value = serde_json::from_slice(&bytes).unwrap_or(Value::Null);
        (status, value)
    }

    async fn create_host(rig: &Rig, token: Option<&str>) -> Value {
        let body = match token {
            Some(t) => {
                json!({"name": "DGX Spark", "base_url": "http://127.0.0.1:8443", "token": t})
            }
            None => json!({"name": "DGX Spark", "base_url": "http://127.0.0.1:8443"}),
        };
        let res = rig
            .app
            .clone()
            .oneshot(request("POST", "/api/remote-hosts", Some(body)))
            .await
            .unwrap();
        let (status, value) = json_response(res).await;
        assert_eq!(status, StatusCode::CREATED, "create failed: {value}");
        value["host"].clone()
    }

    #[tokio::test]
    async fn create_with_token_never_returns_token() {
        let rig = rig();
        let host = create_host(&rig, Some("bridge-secret-token")).await;
        let id = host["id"].as_str().unwrap();
        assert_eq!(host["has_token"], json!(true));
        // The raw token must not appear anywhere in the response body.
        assert!(!host.to_string().contains("bridge-secret-token"));
        // But it must be in the (in-memory) keychain under host:<id>.
        assert_eq!(
            rig.test_state.secrets.get(&secret_key(id)).as_deref(),
            Some("bridge-secret-token")
        );
    }

    #[tokio::test]
    async fn list_and_patch_host() {
        let rig = rig();
        let host = create_host(&rig, Some("tok-1")).await;
        let id = host["id"].as_str().unwrap();

        // List returns the host.
        let res = rig
            .app
            .clone()
            .oneshot(request("GET", "/api/remote-hosts", None))
            .await
            .unwrap();
        let (status, value) = json_response(res).await;
        assert_eq!(status, StatusCode::OK);
        assert_eq!(value["hosts"].as_array().unwrap().len(), 1);

        // Patch the name.
        let res = rig
            .app
            .clone()
            .oneshot(request(
                "PATCH",
                &format!("/api/remote-hosts/{id}"),
                Some(json!({"name": "DGX Spark 2"})),
            ))
            .await
            .unwrap();
        let (status, value) = json_response(res).await;
        assert_eq!(status, StatusCode::OK);
        assert_eq!(value["host"]["name"], json!("DGX Spark 2"));
    }

    #[tokio::test]
    async fn delete_removes_keychain_entry() {
        let rig = rig();
        let host = create_host(&rig, Some("tok-to-delete")).await;
        let id = host["id"].as_str().unwrap();

        let res = rig
            .app
            .clone()
            .oneshot(request("DELETE", &format!("/api/remote-hosts/{id}"), None))
            .await
            .unwrap();
        assert_eq!(res.status(), StatusCode::NO_CONTENT);
        assert_eq!(rig.test_state.secrets.get(&secret_key(id)), None);

        // Second delete -> 404.
        let res = rig
            .app
            .clone()
            .oneshot(request("DELETE", &format!("/api/remote-hosts/{id}"), None))
            .await
            .unwrap();
        let (status, value) = json_response(res).await;
        assert_eq!(status, StatusCode::NOT_FOUND);
        assert_eq!(value["error"]["code"], json!("not_found"));
    }

    #[tokio::test]
    async fn test_host_records_unreachable_status() {
        // No real bridge is listening on this port, so /health fails.
        let rig = rig();
        // Create a host pointing at a dead port.
        let body = json!({"name": "Dead", "base_url": "http://127.0.0.1:1"});
        let res = rig
            .app
            .clone()
            .oneshot(request("POST", "/api/remote-hosts", Some(body)))
            .await
            .unwrap();
        let (_, value) = json_response(res).await;
        let id = value["host"]["id"].as_str().unwrap();

        let res = rig
            .app
            .clone()
            .oneshot(request(
                "POST",
                &format!("/api/remote-hosts/{id}/test"),
                None,
            ))
            .await
            .unwrap();
        let (status, value) = json_response(res).await;
        assert_eq!(status, StatusCode::OK);
        assert_eq!(value["ok"], json!(false));

        let row = rig
            .test_state
            .state
            .db
            .get_remote_host(id)
            .await
            .unwrap()
            .unwrap();
        assert_eq!(row.status.as_deref(), Some("unreachable"));
        assert!(row.last_checked_at.is_some());
    }

    #[test]
    fn secret_key_is_prefixed() {
        assert_eq!(secret_key("abc"), "host:abc");
    }

    #[tokio::test]
    async fn proxy_asset_unknown_host_is_404() {
        let rig = rig();
        let res = rig
            .app
            .clone()
            .oneshot(request(
                "GET",
                "/api/remote-hosts/nope/assets/some-token",
                None,
            ))
            .await
            .unwrap();
        let (status, value) = json_response(res).await;
        assert_eq!(status, StatusCode::NOT_FOUND);
        assert_eq!(value["error"]["code"], json!("not_found"));
    }

    #[tokio::test]
    async fn create_host_regenerates_mcp_servers_config() {
        let rig = rig();
        let host = create_host(&rig, Some("bridge-tok")).await;
        let id = host["id"].as_str().unwrap();
        let shortid: String = id.chars().take(8).collect();

        let path = crate::mcp_servers::servers_path(&rig.test_state.state.repo_root);
        let doc: Value = serde_json::from_str(&std::fs::read_to_string(&path).unwrap()).unwrap();
        let entry = &doc["mcpServers"][format!("agentgpt-bridge-{shortid}")];
        assert_eq!(entry["url"], json!("http://127.0.0.1:8443/mcp"));
        assert_eq!(entry["transport"], json!("streamable-http"));
        assert_eq!(
            entry["headers"]["Authorization"],
            json!("Bearer bridge-tok")
        );
    }

    #[tokio::test]
    async fn delete_host_regenerates_mcp_servers_config() {
        let rig = rig();
        let host = create_host(&rig, Some("bridge-tok")).await;
        let id = host["id"].as_str().unwrap();

        let res = rig
            .app
            .clone()
            .oneshot(request("DELETE", &format!("/api/remote-hosts/{id}"), None))
            .await
            .unwrap();
        assert_eq!(res.status(), StatusCode::NO_CONTENT);

        let path = crate::mcp_servers::servers_path(&rig.test_state.state.repo_root);
        let doc: Value = serde_json::from_str(&std::fs::read_to_string(&path).unwrap()).unwrap();
        assert_eq!(doc["mcpServers"], json!({}));
    }
}
