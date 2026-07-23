//! Native GPT Browser (ADR-0009): the server-side feature package.
//!
//! Modules follow spec §17. This root module wires the axum routes: the
//! authenticated UI API (`/api/browser/*`), the per-viewer stream
//! (`/api/browser/stream`), the loopback-only internal API used by the
//! Python Browser tool (`/internal/browser/*`), the Page Agent Hub bridge
//! (`/internal/browser/hub`), and the model proxy
//! (`/internal/page-agent/v1/chat/completions`).

pub mod cdp;
pub mod chromium;
pub mod component;
pub mod downloads;
pub mod input;
pub mod manager;
pub mod model_proxy;
pub mod page_agent_hub;
pub mod permissions;
pub mod profile;
pub mod protocol;
pub mod screencast;

use std::net::SocketAddr;
use std::sync::Arc;

use axum::body::Bytes;
use axum::extract::{ConnectInfo, Path, Query, State};
use axum::http::{header, HeaderMap, StatusCode};
use axum::response::Response;
use axum::routing::{delete, get, patch, post};
use axum::{Json, Router};
use serde::Deserialize;
use serde_json::{json, Value};
use tracing::{debug, warn};

use crate::db::{BrowserPreferencesRow, BrowserProfileRow};
use crate::error::ApiError;
use crate::events;
use crate::state::SharedState;

use manager::{ApprovalResolution, BrowserManager};
use protocol::{BrowserPanelMode, ClientCommand, PermissionScope, StreamEvent};

/// All browser routes, merged into the main router in `build_router`.
pub fn api_routes() -> Router<SharedState> {
    Router::new()
        // Optional component (spec §12)
        .route("/api/browser/component", get(component_state))
        .route("/api/browser/component/install", post(component_install))
        .route("/api/browser/component", delete(component_uninstall))
        // Profiles (spec §6)
        .route(
            "/api/browser/profiles",
            get(list_profiles).post(create_profile),
        )
        .route(
            "/api/browser/profiles/{id}",
            patch(patch_profile).delete(delete_profile),
        )
        // State + lifecycle (spec §9.2)
        .route("/api/browser/state", get(browser_state))
        .route("/api/browser/start", post(browser_start))
        .route("/api/browser/stop", post(browser_stop))
        .route("/api/browser/navigate", post(browser_navigate))
        .route("/api/browser/tabs", post(create_tab))
        .route("/api/browser/tabs/{id}", delete(close_tab))
        .route("/api/browser/panel", post(update_panel))
        .route("/api/browser/task/{id}/stop", post(stop_task))
        .route("/api/browser/task/{id}/take-over", post(take_over_task))
        .route(
            "/api/browser/approvals/{id}/resolve",
            post(resolve_approval),
        )
        // Viewer stream (spec §9.3)
        .route("/api/browser/stream", get(stream_ws))
        // Loopback-only internal API for the Python Browser tool (spec §9.1)
        .route("/internal/browser/command", post(internal_command))
        .route("/internal/browser/status", get(internal_status))
        .route("/internal/browser/stop", post(internal_stop))
        // Page Agent Hub bridge (spec §5.1) and model proxy (spec §5.4)
        .route("/internal/browser/hub", get(hub_ws))
        .route(
            "/internal/page-agent/v1/chat/completions",
            post(model_proxy::chat_completions),
        )
}

// ---- helpers ----

/// Internal routes are loopback-only and must reject browser-origin requests
/// (spec §9.1): any web page could otherwise CSRF the local host.
fn require_internal(peer: &SocketAddr, headers: &HeaderMap) -> Result<(), ApiError> {
    if !peer.ip().is_loopback() {
        return Err(ApiError::new(
            StatusCode::FORBIDDEN,
            "forbidden",
            "internal browser routes are loopback-only",
        ));
    }
    if headers.contains_key(header::ORIGIN) {
        return Err(ApiError::new(
            StatusCode::FORBIDDEN,
            "forbidden",
            "browser-origin requests are rejected on internal routes",
        ));
    }
    Ok(())
}

/// Current preferences for a profile, or the seeded defaults.
async fn prefs_for(state: &SharedState, profile_id: &str) -> BrowserPreferencesRow {
    state
        .db
        .get_browser_preferences(profile_id)
        .await
        .ok()
        .flatten()
        .unwrap_or(BrowserPreferencesRow {
            profile_id: profile_id.to_string(),
            panel_mode: "hidden".to_string(),
            panel_width: 640,
            previous_panel_width: None,
            auto_open_on_tool_call: true,
            keep_running_when_hidden: true,
            remote_streaming_enabled: false,
            model_mode: "follow_conversation".to_string(),
            model_endpoint_id: None,
            model_id: None,
        })
}

fn prefs_json(prefs: &BrowserPreferencesRow) -> Value {
    json!({
        "profileId": prefs.profile_id,
        "panelMode": prefs.panel_mode,
        "panelWidth": prefs.panel_width,
        "previousPanelWidth": prefs.previous_panel_width,
        "autoOpenOnToolCall": prefs.auto_open_on_tool_call,
        "keepRunningWhenHidden": prefs.keep_running_when_hidden,
        "remoteStreamingEnabled": prefs.remote_streaming_enabled,
        "modelMode": prefs.model_mode,
        "modelEndpointId": prefs.model_endpoint_id,
        "modelId": prefs.model_id,
    })
}

fn profile_json(state: &BrowserManager, row: &BrowserProfileRow) -> Value {
    let profile_path = if row.profile_path.is_empty() {
        profile::profile_dir(state.data_root(), &row.id)
            .to_string_lossy()
            .into_owned()
    } else {
        row.profile_path.clone()
    };
    json!({
        "id": row.id,
        "name": row.name,
        "engine": row.engine,
        "executablePath": row.executable_path,
        "profilePath": profile_path,
        "createdAt": row.created_at,
        "updatedAt": row.updated_at,
        "lastUsedAt": row.last_used_at,
    })
}

fn tool_ok(summary: impl Into<String>, data: Value) -> Json<Value> {
    Json(json!({
        "ok": true,
        "summary": summary.into(),
        "data": data,
        "error": null,
    }))
}

fn tool_err(error: &manager::BrowserError) -> Json<Value> {
    Json(json!({
        "ok": false,
        "summary": error.to_string(),
        "data": null,
        "error": { "code": error.code(), "message": error.to_string() },
    }))
}

// ---- component handlers ----

async fn component_state(State(state): State<SharedState>) -> Result<Json<Value>, ApiError> {
    let snapshot = state.browser.component.snapshot().await;
    let manifest = &component::COMPONENT_MANIFEST;
    Ok(Json(json!({
        "status": snapshot.status,
        "progress": snapshot.progress,
        "error": snapshot.error,
        "installed": snapshot.installed(),
        "installedVersion": snapshot.installed_version,
        "availableVersion": manifest.version,
        "pageAgentExtensionId": manifest.page_agent_extension_id,
    })))
}

async fn component_install(
    State(state): State<SharedState>,
) -> Result<(StatusCode, Json<Value>), ApiError> {
    state
        .browser
        .component
        .install()
        .await
        .map_err(|e| match e {
            component::ComponentError::AlreadyInstalling => {
                ApiError::conflict("an install is already in progress")
            }
            other => ApiError::internal(other.to_string()),
        })?;
    let snapshot = state.browser.component.snapshot().await;
    Ok((
        StatusCode::ACCEPTED,
        Json(json!({
            "status": snapshot.status,
            "progress": snapshot.progress,
        })),
    ))
}

async fn component_uninstall(State(state): State<SharedState>) -> Result<Json<Value>, ApiError> {
    state
        .browser
        .component
        .uninstall()
        .await
        .map_err(|e| ApiError::internal(e.to_string()))?;
    events::data_changed(&state, json!({"entity": "browser_component"}));
    Ok(Json(json!({ "status": "not_installed" })))
}

// ---- profile handlers ----

async fn list_profiles(State(state): State<SharedState>) -> Result<Json<Value>, ApiError> {
    let profiles = state.db.list_browser_profiles().await?;
    let items: Vec<Value> = profiles
        .iter()
        .map(|p| profile_json(&state.browser, p))
        .collect();
    Ok(Json(json!({ "profiles": items })))
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
struct CreateProfile {
    name: String,
    #[serde(default)]
    id: Option<String>,
    #[serde(default)]
    executable_path: Option<String>,
}

async fn create_profile(
    State(state): State<SharedState>,
    Json(body): Json<CreateProfile>,
) -> Result<(StatusCode, Json<Value>), ApiError> {
    let name = body.name.trim();
    if name.is_empty() {
        return Err(ApiError::bad_request("profile name must not be empty"));
    }
    let id = body
        .id
        .filter(|id| !id.trim().is_empty())
        .unwrap_or_else(|| uuid::Uuid::now_v7().to_string());
    // Client-supplied ids become directory names under profiles/ — reject
    // anything that could escape the root (path traversal).
    if !profile::is_valid_profile_id(&id) {
        return Err(ApiError::bad_request("invalid profile id"));
    }
    if state.db.get_browser_profile(&id).await?.is_some() {
        return Err(ApiError::conflict(format!("profile {id} already exists")));
    }
    let now = chrono::Utc::now().to_rfc3339();
    let row = BrowserProfileRow {
        id: id.clone(),
        name: name.to_string(),
        engine: "bundled_chromium".to_string(),
        executable_path: body.executable_path,
        profile_path: String::new(), // resolved lazily (env-dependent)
        created_at: now.clone(),
        updated_at: now,
        last_used_at: None,
    };
    state.db.insert_browser_profile(&row).await?;
    events::data_changed(&state, json!({"entity": "browser_profile", "id": id}));
    Ok((
        StatusCode::CREATED,
        Json(profile_json(&state.browser, &row)),
    ))
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
struct PatchProfile {
    #[serde(default)]
    name: Option<String>,
    #[serde(default)]
    executable_path: Option<Option<String>>,
}

async fn patch_profile(
    State(state): State<SharedState>,
    Path(id): Path<String>,
    Json(body): Json<PatchProfile>,
) -> Result<Json<Value>, ApiError> {
    let mut row = state
        .db
        .get_browser_profile(&id)
        .await?
        .ok_or_else(|| ApiError::not_found(format!("profile {id} not found")))?;
    if let Some(name) = body.name {
        let name = name.trim();
        if name.is_empty() {
            return Err(ApiError::bad_request("profile name must not be empty"));
        }
        row.name = name.to_string();
    }
    if let Some(executable_path) = body.executable_path {
        row.executable_path = executable_path.filter(|p| !p.trim().is_empty());
        row.engine = if row.executable_path.is_some() {
            "system".to_string()
        } else {
            "bundled_chromium".to_string()
        };
    }
    row.updated_at = chrono::Utc::now().to_rfc3339();
    state.db.update_browser_profile(&row).await?;
    events::data_changed(&state, json!({"entity": "browser_profile", "id": id}));
    Ok(Json(profile_json(&state.browser, &row)))
}

async fn delete_profile(
    State(state): State<SharedState>,
    Path(id): Path<String>,
) -> Result<StatusCode, ApiError> {
    if !profile::is_valid_profile_id(&id) {
        return Err(ApiError::bad_request("invalid profile id"));
    }
    if id == "default" {
        return Err(ApiError::bad_request(
            "the default profile cannot be deleted",
        ));
    }
    if state.browser.active_profile_id().await == id {
        return Err(ApiError::conflict(
            "stop the browser before deleting its active profile",
        ));
    }
    if !state.db.delete_browser_profile(&id).await? {
        return Err(ApiError::not_found(format!("profile {id} not found")));
    }
    // Profile files are removed only when the directory exists and is not
    // locked; a live profile is never touched (spec §6.3).
    let dir = profile::profile_dir(state.browser.data_root(), &id);
    if dir.exists() {
        std::fs::remove_dir_all(&dir).map_err(|e| {
            ApiError::conflict(format!(
                "profile row deleted but files could not be removed: {e}"
            ))
        })?;
    }
    events::data_changed(&state, json!({"entity": "browser_profile", "id": id}));
    Ok(StatusCode::NO_CONTENT)
}

// ---- state + lifecycle handlers ----

async fn browser_state(State(state): State<SharedState>) -> Result<Json<Value>, ApiError> {
    let prefs = prefs_for(&state, "default").await;
    let mode = BrowserPanelMode::parse(&prefs.panel_mode).unwrap_or_default();
    let snapshot = state
        .browser
        .state_snapshot(
            mode,
            u32::try_from(prefs.panel_width.max(0)).unwrap_or(640),
            prefs
                .previous_panel_width
                .and_then(|w| u32::try_from(w.max(0)).ok()),
        )
        .await;
    Ok(Json(
        serde_json::to_value(snapshot).map_err(|e| ApiError::internal(e.to_string()))?,
    ))
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
struct StartRequest {
    #[serde(default)]
    profile_id: Option<String>,
}

async fn browser_start(
    State(state): State<SharedState>,
    body: Option<Json<StartRequest>>,
) -> Result<Json<Value>, ApiError> {
    let profile_id = body
        .and_then(|Json(b)| b.profile_id)
        .unwrap_or_else(|| "default".to_string());
    if !profile::is_valid_profile_id(&profile_id) {
        return Err(ApiError::bad_request("invalid profile id"));
    }
    state
        .browser
        .start(&profile_id)
        .await
        .map_err(manager::BrowserError::into_api_error)?;
    browser_state(State(state)).await
}

async fn browser_stop(State(state): State<SharedState>) -> Result<Json<Value>, ApiError> {
    state
        .browser
        .stop()
        .await
        .map_err(manager::BrowserError::into_api_error)?;
    state.browser.emit_state_now().await;
    Ok(Json(json!({ "processStatus": "stopped" })))
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
struct NavigateRequest {
    url: String,
    #[serde(default)]
    tab_id: Option<String>,
}

async fn browser_navigate(
    State(state): State<SharedState>,
    Json(body): Json<NavigateRequest>,
) -> Result<Json<Value>, ApiError> {
    if let Some(tab_id) = &body.tab_id {
        state
            .browser
            .activate_tab(tab_id)
            .await
            .map_err(manager::BrowserError::into_api_error)?;
    }
    // UI-driven navigation is manual (spec §11.4: localhost/private allowed).
    state
        .browser
        .navigate(&body.url, false, None)
        .await
        .map_err(manager::BrowserError::into_api_error)?;
    Ok(Json(json!({ "navigated": body.url })))
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
struct CreateTabRequest {
    #[serde(default)]
    url: Option<String>,
}

async fn create_tab(
    State(state): State<SharedState>,
    Json(body): Json<CreateTabRequest>,
) -> Result<(StatusCode, Json<Value>), ApiError> {
    let tab = state
        .browser
        .new_tab(body.url.as_deref())
        .await
        .map_err(manager::BrowserError::into_api_error)?;
    Ok((
        StatusCode::CREATED,
        Json(serde_json::to_value(tab).map_err(|e| ApiError::internal(e.to_string()))?),
    ))
}

async fn close_tab(
    State(state): State<SharedState>,
    Path(id): Path<String>,
) -> Result<StatusCode, ApiError> {
    state
        .browser
        .close_tab(&id)
        .await
        .map_err(manager::BrowserError::into_api_error)?;
    Ok(StatusCode::NO_CONTENT)
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
struct PanelRequest {
    #[serde(default)]
    mode: Option<String>,
    #[serde(default)]
    width: Option<u32>,
    /// Content-region width from the UI, used for clamping (spec §2.3).
    #[serde(default)]
    container_width: Option<u32>,
}

async fn update_panel(
    State(state): State<SharedState>,
    Json(body): Json<PanelRequest>,
) -> Result<Json<Value>, ApiError> {
    let mut prefs = prefs_for(&state, "default").await;
    if let Some(mode) = &body.mode {
        if BrowserPanelMode::parse(mode).is_none() {
            return Err(ApiError::bad_request(format!("unknown panel mode {mode}")));
        }
        prefs.panel_mode = mode.clone();
    }
    if let Some(width) = body.width {
        let clamped = manager::clamp_panel_width(width, body.container_width.unwrap_or(u32::MAX));
        prefs.previous_panel_width = Some(prefs.panel_width);
        prefs.panel_width = i64::from(clamped);
    }
    state.db.upsert_browser_preferences(&prefs).await?;
    events::data_changed(
        &state,
        json!({"entity": "browser_preferences", "profile_id": "default"}),
    );
    state.browser.emit_state_now().await;
    Ok(Json(prefs_json(&prefs)))
}

async fn stop_task(
    State(state): State<SharedState>,
    Path(id): Path<String>,
) -> Result<Json<Value>, ApiError> {
    state
        .browser
        .stop_task(&id)
        .await
        .map_err(manager::BrowserError::into_api_error)?;
    Ok(Json(json!({ "taskId": id, "status": "stopping" })))
}

async fn take_over_task(
    State(state): State<SharedState>,
    Path(id): Path<String>,
) -> Result<Json<Value>, ApiError> {
    state
        .browser
        .take_over(&id)
        .await
        .map_err(manager::BrowserError::into_api_error)?;
    Ok(Json(
        json!({ "taskId": id, "status": "cancelled", "manualControlEnabled": true }),
    ))
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
struct ResolveApprovalRequest {
    allow: bool,
    #[serde(default)]
    scope: Option<String>,
}

async fn resolve_approval(
    State(state): State<SharedState>,
    Path(id): Path<String>,
    Json(body): Json<ResolveApprovalRequest>,
) -> Result<Json<Value>, ApiError> {
    let scope = body
        .scope
        .as_deref()
        .and_then(|s| match s {
            "once" => Some(PermissionScope::Once),
            "task" => Some(PermissionScope::Task),
            "conversation" => Some(PermissionScope::Conversation),
            "origin" => Some(PermissionScope::Origin),
            "profile" => Some(PermissionScope::Profile),
            _ => None,
        })
        .unwrap_or(PermissionScope::Once);
    if !state.browser.resolve_approval(
        &id,
        ApprovalResolution {
            allow: body.allow,
            scope,
        },
    ) {
        return Err(ApiError::not_found(format!("approval {id} not found")));
    }
    state.browser.emit_state_now().await;
    Ok(Json(
        json!({ "id": id, "resolved": true, "allowed": body.allow }),
    ))
}

// ---- viewer stream (spec §9.3) ----

async fn stream_ws(
    ws: axum::extract::ws::WebSocketUpgrade,
    State(state): State<SharedState>,
    ConnectInfo(peer): ConnectInfo<SocketAddr>,
) -> Result<Response, ApiError> {
    let remote = !peer.ip().is_loopback();
    if remote {
        // Remote viewing is disabled by default (spec §2.7/§11.5).
        let prefs = prefs_for(&state, "default").await;
        if !prefs.remote_streaming_enabled {
            return Err(ApiError::new(
                StatusCode::FORBIDDEN,
                "forbidden",
                "remote browser streaming is disabled; enable it in Settings",
            ));
        }
    }
    Ok(ws.on_upgrade(move |socket| handle_stream(socket, state, remote)))
}

async fn handle_stream(socket: axum::extract::ws::WebSocket, state: SharedState, remote: bool) {
    use axum::extract::ws::Message;
    use futures_util::{SinkExt, StreamExt};

    let browser = state.browser.clone_handle();
    browser.viewer_connected(remote).await;
    let (mut tx, mut rx) = socket.split();
    let mut events = browser.subscribe_events();

    // Initial state snapshot so a fresh viewer renders immediately.
    let prefs = prefs_for(&state, "default").await;
    let mode = BrowserPanelMode::parse(&prefs.panel_mode).unwrap_or_default();
    let snapshot = browser
        .state_snapshot(
            mode,
            u32::try_from(prefs.panel_width.max(0)).unwrap_or(640),
            prefs
                .previous_panel_width
                .and_then(|w| u32::try_from(w.max(0)).ok()),
        )
        .await;
    if let Ok(text) = serde_json::to_string(&StreamEvent::State(Box::new(snapshot))) {
        if tx.send(Message::Text(text.into())).await.is_err() {
            browser.viewer_disconnected(remote).await;
            return;
        }
    }

    // Frame forwarding tolerates pump restarts (tab switches, resizes).
    let (frame_tx, mut frame_rx) = tokio::sync::mpsc::channel::<Arc<protocol::Frame>>(2);
    let frame_browser = browser.clone_handle();
    let frame_forwarder = tokio::spawn(async move {
        loop {
            match frame_browser.subscribe_frames().await {
                Some(mut frames) => loop {
                    match frames.recv().await {
                        Ok(frame) => {
                            if frame_tx.send(frame).await.is_err() {
                                return;
                            }
                        }
                        Err(tokio::sync::broadcast::error::RecvError::Lagged(_)) => continue,
                        Err(tokio::sync::broadcast::error::RecvError::Closed) => break,
                    }
                },
                None => {
                    // No pump (browser stopped / starting); retry shortly.
                    tokio::time::sleep(std::time::Duration::from_millis(250)).await;
                }
            }
        }
    });

    loop {
        tokio::select! {
            message = rx.next() => {
                match message {
                    Some(Ok(Message::Text(text))) => {
                        match serde_json::from_str::<ClientCommand>(&text) {
                            Ok(command) => {
                                if let Err(e) = browser.dispatch_input(&command).await {
                                    debug!(error = %e, "browser input dispatch failed");
                                }
                            }
                            Err(e) => debug!(error = %e, "ignoring malformed browser client command"),
                        }
                    }
                    Some(Ok(Message::Close(_))) | None => break,
                    Some(Ok(_)) => {} // pings/pongs/binary: not used from viewers
                    Some(Err(e)) => {
                        debug!(error = %e, "browser stream receive error");
                        break;
                    }
                }
            }
            event = events.recv() => {
                match event {
                    Ok(event) => {
                        let Ok(text) = serde_json::to_string(&event) else { continue };
                        if tx.send(Message::Text(text.into())).await.is_err() {
                            break;
                        }
                    }
                    Err(tokio::sync::broadcast::error::RecvError::Lagged(skipped)) => {
                        warn!(skipped, "browser stream viewer lagged events");
                    }
                    Err(tokio::sync::broadcast::error::RecvError::Closed) => break,
                }
            }
            frame = frame_rx.recv() => {
                match frame {
                    Some(frame) => {
                        let bytes: Bytes = frame.encode().into();
                        if tx.send(Message::Binary(bytes)).await.is_err() {
                            break;
                        }
                    }
                    None => break,
                }
            }
        }
    }
    frame_forwarder.abort();
    browser.viewer_disconnected(remote).await;
}

// ---- internal API (spec §9.1) ----

#[derive(Debug, Default, Deserialize)]
#[serde(rename_all = "camelCase", default)]
struct InternalCommand {
    action: String,
    url: Option<String>,
    task: Option<String>,
    tab_id: Option<String>,
    file_paths: Vec<String>,
    #[serde(default = "default_wait")]
    wait: bool,
    conversation_id: Option<String>,
    run_id: Option<String>,
    tool_call_id: Option<String>,
}

fn default_wait() -> bool {
    true
}

async fn internal_command(
    State(state): State<SharedState>,
    ConnectInfo(peer): ConnectInfo<SocketAddr>,
    headers: HeaderMap,
    Json(body): Json<InternalCommand>,
) -> Result<Json<Value>, ApiError> {
    require_internal(&peer, &headers)?;
    // Domain errors are reported through the standard tool-result envelope
    // (spec §7.3), not HTTP status codes.
    match dispatch_internal_command(&state, body).await {
        Ok(json) => Ok(json),
        Err(e) => Ok(tool_err(&e)),
    }
}

async fn dispatch_internal_command(
    state: &SharedState,
    body: InternalCommand,
) -> Result<Json<Value>, manager::BrowserError> {
    let browser = &state.browser;
    match body.action.as_str() {
        "open" => {
            browser.start("default").await?;
            if let Some(url) = &body.url {
                browser
                    .navigate(url, true, body.conversation_id.as_deref())
                    .await?;
            }
            Ok(tool_ok(
                "Browser opened.",
                json!({ "profileId": "default" }),
            ))
        }
        "navigate" => {
            let url = body
                .url
                .as_deref()
                .ok_or_else(|| manager::BrowserError::BadRequest("url is required".into()))?;
            browser.start("default").await?;
            browser
                .navigate(url, true, body.conversation_id.as_deref())
                .await?;
            Ok(tool_ok(
                format!("Navigated to {url}."),
                json!({ "url": url }),
            ))
        }
        "execute_task" => {
            let task = body
                .task
                .as_deref()
                .ok_or_else(|| manager::BrowserError::BadRequest("task is required".into()))?;
            let outcome = browser
                .execute_task(
                    task,
                    body.url.as_deref(),
                    body.conversation_id.as_deref(),
                    body.run_id.as_deref(),
                    body.tool_call_id.as_deref(),
                    body.wait,
                )
                .await?;
            if outcome.success {
                Ok(tool_ok(
                    "Browser task completed.",
                    json!({
                        "task_id": outcome.task_id,
                        "final_url": outcome.final_url,
                        "result": outcome.result_text,
                        "artifacts": [],
                    }),
                ))
            } else {
                Ok(Json(json!({
                    "ok": false,
                    "summary": outcome.error_message.clone().unwrap_or_default(),
                    "data": { "task_id": outcome.task_id },
                    "error": {
                        "code": outcome.error_code.unwrap_or_else(|| "task_failed".to_string()),
                        "message": outcome.error_message.unwrap_or_else(|| "task failed".to_string()),
                    },
                })))
            }
        }
        "status" => internal_status_json(state).await.map(Json),
        "stop_task" => {
            let task = browser.active_task().await;
            let Some(task) = task else {
                return Ok(tool_ok("No active task.", json!({ "stopped": false })));
            };
            browser.stop_task(&task.id).await?;
            Ok(tool_ok(
                "Task stopped.",
                json!({ "stopped": true, "taskId": task.id }),
            ))
        }
        "screenshot" => {
            let path = browser.screenshot().await?;
            Ok(tool_ok(
                "Screenshot captured.",
                json!({ "path": path.to_string_lossy() }),
            ))
        }
        "upload_file" => {
            // Validate first, then approval-gate the exact files (spec §15.1).
            let validated = browser.validate_upload_files(&body.file_paths)?;
            let origin = None;
            let (approval_id, rx) = browser.request_approval(
                protocol::PermissionCapability::UploadFile,
                origin,
                format!(
                    "Upload {} file(s): {}",
                    validated.len(),
                    body.file_paths.join(", ")
                ),
                None,
            );
            browser.emit_state_now().await;
            let resolution = rx
                .await
                .map_err(|_| manager::BrowserError::ApprovalDenied)?;
            if !resolution.allow {
                return Ok(tool_err(&manager::BrowserError::ApprovalDenied));
            }
            debug!(approval_id, "file upload approved");
            browser.upload_files(&body.file_paths).await?;
            Ok(tool_ok(
                "Files attached to the page's file input.",
                json!({ "files": body.file_paths }),
            ))
        }
        "close_tab" => {
            let tab_id = body
                .tab_id
                .as_deref()
                .ok_or_else(|| manager::BrowserError::BadRequest("tab_id is required".into()))?;
            browser.close_tab(tab_id).await?;
            Ok(tool_ok("Tab closed.", json!({ "tabId": tab_id })))
        }
        "close_browser" => {
            browser.stop().await?;
            Ok(tool_ok("Browser stopped.", json!({ "stopped": true })))
        }
        other => Ok(tool_err(&manager::BrowserError::BadRequest(format!(
            "unknown action {other}"
        )))),
    }
}

async fn internal_status_json(state: &SharedState) -> Result<Value, manager::BrowserError> {
    let prefs = prefs_for(state, "default").await;
    let mode = BrowserPanelMode::parse(&prefs.panel_mode).unwrap_or_default();
    let snapshot = state
        .browser
        .state_snapshot(
            mode,
            u32::try_from(prefs.panel_width.max(0)).unwrap_or(640),
            None,
        )
        .await;
    let active_tab = snapshot
        .tabs
        .iter()
        .find(|t| Some(&t.id) == snapshot.active_tab_id.as_ref());
    Ok(json!({
        "ok": true,
        "summary": "Browser status.",
        "data": {
            "running": snapshot.process_status == protocol::ProcessStatus::Running,
            "panel_visible": snapshot.panel_mode != BrowserPanelMode::Hidden,
            "profile_id": snapshot.profile_id,
            "active_tab": active_tab.map(|t| json!({
                "id": t.id,
                "title": t.title,
                "url": t.url,
            })),
            "task": snapshot.task,
        },
        "error": null,
    }))
}

async fn internal_status(
    State(state): State<SharedState>,
    ConnectInfo(peer): ConnectInfo<SocketAddr>,
    headers: HeaderMap,
) -> Result<Json<Value>, ApiError> {
    require_internal(&peer, &headers)?;
    internal_status_json(&state)
        .await
        .map(Json)
        .map_err(|e| e.into_api_error())
}

async fn internal_stop(
    State(state): State<SharedState>,
    ConnectInfo(peer): ConnectInfo<SocketAddr>,
    headers: HeaderMap,
) -> Result<Json<Value>, ApiError> {
    require_internal(&peer, &headers)?;
    if let Some(task) = state.browser.active_task().await {
        if task.status.is_active() {
            state
                .browser
                .stop_task(&task.id)
                .await
                .map_err(manager::BrowserError::into_api_error)?;
            return Ok(tool_ok("Task stopped.", json!({ "stopped": true })));
        }
    }
    Ok(tool_ok("No active task.", json!({ "stopped": false })))
}

// ---- Page Agent Hub bridge (spec §5.1) ----

#[derive(Debug, Deserialize)]
struct HubQuery {
    token: Option<String>,
}

async fn hub_ws(
    ws: axum::extract::ws::WebSocketUpgrade,
    State(state): State<SharedState>,
    ConnectInfo(peer): ConnectInfo<SocketAddr>,
    Query(query): Query<HubQuery>,
) -> Result<Response, ApiError> {
    if !peer.ip().is_loopback() {
        return Err(ApiError::new(
            StatusCode::FORBIDDEN,
            "forbidden",
            "the page agent hub endpoint is loopback-only",
        ));
    }
    let expected = state.browser.hub_token().await;
    let provided = query.token.unwrap_or_default();
    if expected.is_empty() || provided != expected {
        return Err(ApiError::new(
            StatusCode::UNAUTHORIZED,
            "unauthorized",
            "invalid hub token",
        ));
    }
    Ok(ws.on_upgrade(move |socket| handle_hub(socket, state)))
}

async fn handle_hub(socket: axum::extract::ws::WebSocket, state: SharedState) {
    use axum::extract::ws::Message;
    use futures_util::{SinkExt, StreamExt};

    let browser = state.browser.clone_handle();
    let (mut tx, mut rx) = socket.split();
    let (outbound_tx, mut outbound_rx) = tokio::sync::mpsc::unbounded_channel::<String>();
    browser.register_hub(outbound_tx).await;
    loop {
        tokio::select! {
            message = rx.next() => {
                match message {
                    Some(Ok(Message::Text(text))) => {
                        browser.dispatch_hub_inbound(&text).await;
                    }
                    Some(Ok(Message::Close(_))) | None => break,
                    Some(Ok(_)) => {}
                    Some(Err(e)) => {
                        debug!(error = %e, "hub receive error");
                        break;
                    }
                }
            }
            Some(text) = outbound_rx.recv() => {
                if tx.send(Message::Text(text.into())).await.is_err() {
                    break;
                }
            }
        }
    }
    browser.unregister_hub().await;
}

#[cfg(test)]
mod tests {
    use super::*;
    use axum::body::Body;
    use axum::extract::ConnectInfo;
    use axum::http::Request;
    use tower::ServiceExt;

    const LOOPBACK: [u8; 4] = [127, 0, 0, 1];
    const LAN: [u8; 4] = [192, 168, 1, 50];

    fn request(
        method: &str,
        path: &str,
        peer: [u8; 4],
        token: Option<&str>,
        body: Option<Value>,
    ) -> Request<Body> {
        let mut builder = Request::builder().method(method).uri(path);
        if let Some(token) = token {
            builder = builder.header(header::AUTHORIZATION, format!("Bearer {token}"));
        }
        if body.is_some() {
            builder = builder.header(header::CONTENT_TYPE, "application/json");
        }
        let body = body
            .map(|b| Body::from(serde_json::to_vec(&b).unwrap()))
            .unwrap_or_else(Body::empty);
        let mut req = builder.body(body).unwrap();
        req.extensions_mut()
            .insert(ConnectInfo(SocketAddr::from((peer, 40_000))));
        req
    }

    #[tokio::test]
    async fn state_returns_not_installed_default() {
        let rig = crate::state::test_state("tok");
        let app = crate::build_router(rig.state.clone());
        let res = app
            .oneshot(request("GET", "/api/browser/state", LOOPBACK, None, None))
            .await
            .unwrap();
        assert_eq!(res.status(), StatusCode::OK);
        let bytes = http_body_util::BodyExt::collect(res.into_body())
            .await
            .unwrap()
            .to_bytes();
        let json: Value = serde_json::from_slice(&bytes).unwrap();
        assert_eq!(json["installed"], false);
        assert_eq!(json["installStatus"], "not_installed");
        assert_eq!(json["processStatus"], "stopped");
        assert_eq!(json["profileId"], "default");
        assert_eq!(json["panelMode"], "hidden");
        assert_eq!(json["manualControlEnabled"], true);
        assert_eq!(json["tabs"], json!([]));
        assert_eq!(json["task"], Value::Null);
    }

    #[tokio::test]
    async fn internal_command_rejects_non_loopback_even_with_ui_token() {
        let rig = crate::state::test_state("tok");
        let app = crate::build_router(rig.state.clone());
        let res = app
            .oneshot(request(
                "POST",
                "/internal/browser/command",
                LAN,
                Some("tok"), // passes the UI bearer gate, must still be denied
                Some(json!({"action": "status"})),
            ))
            .await;
        let res = res.unwrap();
        assert_eq!(res.status(), StatusCode::FORBIDDEN);
    }

    #[tokio::test]
    async fn internal_command_rejects_browser_origins() {
        let rig = crate::state::test_state("tok");
        let app = crate::build_router(rig.state.clone());
        let mut req = request(
            "POST",
            "/internal/browser/command",
            LOOPBACK,
            None,
            Some(json!({"action": "status"})),
        );
        req.headers_mut().insert(
            header::ORIGIN,
            axum::http::HeaderValue::from_static("https://evil.example"),
        );
        let res = app.oneshot(req).await.unwrap();
        assert_eq!(res.status(), StatusCode::FORBIDDEN);
    }

    #[tokio::test]
    async fn internal_command_status_ok_on_loopback() {
        let rig = crate::state::test_state("tok");
        let app = crate::build_router(rig.state.clone());
        let res = app
            .oneshot(request(
                "POST",
                "/internal/browser/command",
                LOOPBACK,
                None,
                Some(json!({"action": "status"})),
            ))
            .await
            .unwrap();
        assert_eq!(res.status(), StatusCode::OK);
        let bytes = http_body_util::BodyExt::collect(res.into_body())
            .await
            .unwrap()
            .to_bytes();
        let json: Value = serde_json::from_slice(&bytes).unwrap();
        assert_eq!(json["ok"], true);
        assert_eq!(json["data"]["running"], false);
        assert_eq!(json["data"]["profile_id"], "default");
    }

    #[tokio::test]
    async fn internal_status_get_requires_loopback() {
        let rig = crate::state::test_state("tok");
        let app = crate::build_router(rig.state.clone());
        let res = app
            .oneshot(request(
                "GET",
                "/internal/browser/status",
                LAN,
                Some("tok"),
                None,
            ))
            .await
            .unwrap();
        assert_eq!(res.status(), StatusCode::FORBIDDEN);
    }

    #[tokio::test]
    async fn hub_ws_rejects_missing_token() {
        let rig = crate::state::test_state("tok");
        let app = crate::build_router(rig.state.clone());
        let listener = tokio::net::TcpListener::bind((std::net::Ipv4Addr::LOCALHOST, 0))
            .await
            .unwrap();
        let addr = listener.local_addr().unwrap();
        tokio::spawn(async move {
            axum::serve(
                listener,
                app.into_make_service_with_connect_info::<SocketAddr>(),
            )
            .await
            .ok();
        });
        // No hub token is minted until the browser starts, so any token must
        // fail the handshake with 401.
        let result = tokio_tungstenite::connect_async(format!(
            "ws://{addr}/internal/browser/hub?token=wrong"
        ))
        .await;
        match result {
            Err(tokio_tungstenite::tungstenite::Error::Http(response)) => {
                assert_eq!(response.status(), StatusCode::UNAUTHORIZED);
            }
            other => panic!("expected HTTP 401 rejection, got {:?}", other.is_ok()),
        }
    }

    #[tokio::test]
    async fn panel_update_persists_and_clamps() {
        let rig = crate::state::test_state("tok");
        let app = crate::build_router(rig.state.clone());
        let res = app
            .oneshot(request(
                "POST",
                "/api/browser/panel",
                LOOPBACK,
                None,
                Some(json!({"mode": "split", "width": 5000, "containerWidth": 1200})),
            ))
            .await
            .unwrap();
        assert_eq!(res.status(), StatusCode::OK);
        let bytes = http_body_util::BodyExt::collect(res.into_body())
            .await
            .unwrap()
            .to_bytes();
        let json: Value = serde_json::from_slice(&bytes).unwrap();
        // 1200 - 420 (min content) = 780.
        assert_eq!(json["panelWidth"], 780);
        assert_eq!(json["previousPanelWidth"], 640);
        assert_eq!(json["panelMode"], "split");
        let prefs = rig
            .state
            .db
            .get_browser_preferences("default")
            .await
            .unwrap()
            .unwrap();
        assert_eq!(prefs.panel_width, 780);
    }

    #[tokio::test]
    async fn approval_resolution_round_trip() {
        let rig = crate::state::test_state("tok");
        let state = rig.state.clone();
        let (approval_id, rx) = state.browser.request_approval(
            protocol::PermissionCapability::NavigatePublicWeb,
            Some("https://example.com".into()),
            "test approval".into(),
            None,
        );
        let app = crate::build_router(state.clone());
        let res = app
            .oneshot(request(
                "POST",
                &format!("/api/browser/approvals/{approval_id}/resolve"),
                LOOPBACK,
                None,
                Some(json!({"allow": true, "scope": "conversation"})),
            ))
            .await
            .unwrap();
        assert_eq!(res.status(), StatusCode::OK);
        let resolution = rx.await.unwrap();
        assert!(resolution.allow);
        assert_eq!(resolution.scope, PermissionScope::Conversation);
        // Resolving twice is a 404.
        let app = crate::build_router(state.clone());
        let res = app
            .oneshot(request(
                "POST",
                &format!("/api/browser/approvals/{approval_id}/resolve"),
                LOOPBACK,
                None,
                Some(json!({"allow": true})),
            ))
            .await
            .unwrap();
        assert_eq!(res.status(), StatusCode::NOT_FOUND);
    }

    #[tokio::test]
    async fn model_proxy_requires_task_token_and_loopback() {
        let rig = crate::state::test_state("tok");
        let app = crate::build_router(rig.state.clone());
        // No token → 401.
        let res = app
            .clone()
            .oneshot(request(
                "POST",
                "/internal/page-agent/v1/chat/completions",
                LOOPBACK,
                None,
                Some(json!({"model": "m", "messages": []})),
            ))
            .await
            .unwrap();
        assert_eq!(res.status(), StatusCode::UNAUTHORIZED);
        // Non-loopback → 403 even with the UI token.
        let res = app
            .oneshot(request(
                "POST",
                "/internal/page-agent/v1/chat/completions",
                LAN,
                Some("tok"),
                Some(json!({"model": "m", "messages": []})),
            ))
            .await
            .unwrap();
        assert_eq!(res.status(), StatusCode::FORBIDDEN);
    }

    #[tokio::test]
    async fn model_proxy_forwards_with_real_key() {
        // Tiny upstream provider capturing the Authorization header.
        let listener = tokio::net::TcpListener::bind((std::net::Ipv4Addr::LOCALHOST, 0))
            .await
            .unwrap();
        let upstream_addr = listener.local_addr().unwrap();
        let (seen_tx, mut seen_rx) = tokio::sync::mpsc::channel::<String>(1);
        let upstream = Router::new().route(
            "/chat/completions",
            post(move |headers: HeaderMap| {
                let seen_tx = seen_tx.clone();
                async move {
                    let auth = headers
                        .get(header::AUTHORIZATION)
                        .and_then(|v| v.to_str().ok())
                        .unwrap_or("")
                        .to_string();
                    let _ = seen_tx.send(auth).await;
                    Json(json!({"id": "cmpl-1", "choices": []}))
                }
            }),
        );
        tokio::spawn(async move {
            axum::serve(listener, upstream).await.ok();
        });

        let rig = crate::state::test_state("tok");
        let state = rig.state.clone();
        // Endpoint + model + conversation wired for follow_conversation.
        let now = chrono::Utc::now().to_rfc3339();
        state
            .db
            .insert_endpoint(&crate::db::EndpointRow {
                id: "ep-1".into(),
                name: "Test".into(),
                base_url: format!("http://{upstream_addr}"),
                timeout_seconds: 15,
                tls_verify: true,
                has_api_key: true,
                default_model_id: Some("m-1".into()),
                last_test_status: None,
                last_tested_at: None,
                created_at: now.clone(),
                updated_at: now.clone(),
            })
            .await
            .unwrap();
        state
            .db
            .upsert_model("ep-1", "m-1", "manual", None)
            .await
            .unwrap();
        state
            .db
            .insert_conversation(&crate::db::ConversationRow {
                id: "c-1".into(),
                project_id: None,
                title: "t".into(),
                endpoint_id: Some("ep-1".into()),
                model_id: Some("m-1".into()),
                archived_at: None,
                created_at: now.clone(),
                updated_at: now,
                message_count: None,
            })
            .await
            .unwrap();
        state.secrets.set("ep-1", "real-provider-key").unwrap();
        let token = state.browser.mint_task_token(manager::TaskTokenGrant {
            task_id: "task-1".into(),
            conversation_id: Some("c-1".into()),
            endpoint_id: None,
            model_id: None,
        });

        let app = crate::build_router(state.clone());
        let mut req = request(
            "POST",
            "/internal/page-agent/v1/chat/completions",
            LOOPBACK,
            None,
            Some(json!({"model": "m-1", "messages": []})),
        );
        req.headers_mut().insert(
            header::AUTHORIZATION,
            axum::http::HeaderValue::from_str(&format!("Bearer {token}")).unwrap(),
        );
        let res = app.oneshot(req).await.unwrap();
        assert_eq!(res.status(), StatusCode::OK);
        let seen = tokio::time::timeout(std::time::Duration::from_secs(5), seen_rx.recv())
            .await
            .unwrap()
            .unwrap();
        assert_eq!(seen, "Bearer real-provider-key");
        // Token use-after-revoke fails.
        state.browser.revoke_task_token(&token);
        assert!(state.browser.validate_task_token(&token).is_none());
    }
}
