//! REST handlers for endpoint/model persistence (`/api/endpoints...`).
//!
//! API keys: accepted on create/patch, stored in the keychain via
//! [`crate::secrets::KeyStore`], resolved to the raw key only when relaying
//! to the sidecar. They never appear in responses or logs.

use axum::extract::{Path, Query, State};
use axum::http::StatusCode;
use axum::Json;
use serde::Deserialize;
use serde_json::{json, Value};
use std::time::Instant;

use crate::db::{EndpointRow, ModelRow};
use crate::error::ApiError;
use crate::relay;
use crate::state::SharedState;

// ---- request/response shapes ----

#[derive(Debug, Deserialize)]
pub struct CreateEndpoint {
    name: String,
    base_url: String,
    #[serde(default)]
    api_key: Option<String>,
    #[serde(default)]
    timeout_seconds: Option<i64>,
    #[serde(default)]
    tls_verify: Option<bool>,
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
pub struct PatchEndpoint {
    #[serde(default)]
    name: Option<String>,
    #[serde(default)]
    base_url: Option<String>,
    /// Tri-state: absent = keep, null = clear, string = set.
    #[serde(default, deserialize_with = "deserialize_some")]
    api_key: Option<Option<String>>,
    #[serde(default)]
    timeout_seconds: Option<i64>,
    #[serde(default)]
    tls_verify: Option<bool>,
    /// Tri-state: absent = keep, null = clear, string = set.
    #[serde(default, deserialize_with = "deserialize_some")]
    default_model_id: Option<Option<String>>,
}

#[derive(Debug, Deserialize)]
pub struct AddModel {
    model_id: String,
}

#[derive(Debug, Deserialize)]
pub struct PatchModel {
    hidden: bool,
}

#[derive(Debug, Deserialize)]
pub struct ModelsQuery {
    #[serde(default)]
    refresh: Option<bool>,
}

fn model_json(row: &ModelRow) -> Value {
    let capabilities = row
        .capabilities_json
        .as_deref()
        .and_then(|s| serde_json::from_str::<Value>(s).ok())
        .unwrap_or(Value::Null);
    json!({
        "id": row.remote_model_id,
        "hidden": row.hidden,
        "source": row.source,
        "capabilities": capabilities,
    })
}

fn validate_timeout(timeout_seconds: i64) -> Result<i64, ApiError> {
    if (1..=120).contains(&timeout_seconds) {
        Ok(timeout_seconds)
    } else {
        Err(ApiError::bad_request(
            "timeout_seconds must be between 1 and 120",
        ))
    }
}

async fn load_endpoint(state: &SharedState, id: &str) -> Result<EndpointRow, ApiError> {
    state
        .db
        .get_endpoint(id)
        .await?
        .ok_or_else(|| ApiError::not_found(format!("endpoint {id} not found")))
}

/// `GET /api/endpoints`
pub async fn list_endpoints(State(state): State<SharedState>) -> Result<Json<Value>, ApiError> {
    let endpoints = state.db.list_endpoints().await?;
    Ok(Json(json!({ "endpoints": endpoints })))
}

/// `POST /api/endpoints`
pub async fn create_endpoint(
    State(state): State<SharedState>,
    Json(body): Json<CreateEndpoint>,
) -> Result<(StatusCode, Json<Value>), ApiError> {
    let name = body.name.trim();
    let base_url = body.base_url.trim();
    if name.is_empty() || base_url.is_empty() {
        return Err(ApiError::bad_request("name and base_url are required"));
    }
    let api_key = body.api_key.filter(|k| !k.is_empty());
    let now = chrono::Utc::now().to_rfc3339();
    let row = EndpointRow {
        id: uuid::Uuid::now_v7().to_string(),
        name: name.to_string(),
        base_url: base_url.to_string(),
        timeout_seconds: validate_timeout(body.timeout_seconds.unwrap_or(15))?,
        tls_verify: body.tls_verify.unwrap_or(true),
        has_api_key: api_key.is_some(),
        default_model_id: None,
        last_test_status: None,
        last_tested_at: None,
        created_at: now.clone(),
        updated_at: now,
    };
    if let Some(key) = &api_key {
        state
            .secrets
            .set(&row.id, key)
            .map_err(|e| ApiError::internal(format!("failed to store api key: {e}")))?;
    }
    if let Err(e) = state.db.insert_endpoint(&row).await {
        if api_key.is_some() {
            let _ = state.secrets.delete(&row.id);
        }
        return Err(e.into());
    }
    Ok((StatusCode::CREATED, Json(json!({ "endpoint": row }))))
}

/// `PATCH /api/endpoints/{id}`
pub async fn patch_endpoint(
    State(state): State<SharedState>,
    Path(id): Path<String>,
    Json(body): Json<PatchEndpoint>,
) -> Result<Json<Value>, ApiError> {
    let mut row = load_endpoint(&state, &id).await?;
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
    if let Some(timeout) = body.timeout_seconds {
        row.timeout_seconds = validate_timeout(timeout)?;
    }
    if let Some(tls_verify) = body.tls_verify {
        row.tls_verify = tls_verify;
    }
    if let Some(default_model_id) = body.default_model_id {
        row.default_model_id = default_model_id;
    }
    match body.api_key {
        Some(Some(key)) if !key.is_empty() => {
            state
                .secrets
                .set(&id, &key)
                .map_err(|e| ApiError::internal(format!("failed to store api key: {e}")))?;
            row.has_api_key = true;
        }
        Some(_) => {
            // null or empty string: clear the key.
            state
                .secrets
                .delete(&id)
                .map_err(|e| ApiError::internal(format!("failed to delete api key: {e}")))?;
            row.has_api_key = false;
        }
        None => {}
    }
    row.updated_at = chrono::Utc::now().to_rfc3339();
    state.db.update_endpoint(&row).await?;
    Ok(Json(json!({ "endpoint": row })))
}

/// `DELETE /api/endpoints/{id}`
pub async fn delete_endpoint(
    State(state): State<SharedState>,
    Path(id): Path<String>,
) -> Result<StatusCode, ApiError> {
    if !state.db.delete_endpoint(&id).await? {
        return Err(ApiError::not_found(format!("endpoint {id} not found")));
    }
    let _ = state.secrets.delete(&id);
    Ok(StatusCode::NO_CONTENT)
}

/// Resolve the raw API key for relaying to the sidecar (never logged).
fn resolve_api_key(state: &SharedState, endpoint: &EndpointRow) -> Option<String> {
    if endpoint.has_api_key {
        state.secrets.get(&endpoint.id)
    } else {
        None
    }
}

/// `POST /api/endpoints/{id}/test`
pub async fn test_endpoint(
    State(state): State<SharedState>,
    Path(id): Path<String>,
) -> Result<Json<Value>, ApiError> {
    let endpoint = load_endpoint(&state, &id).await?;
    let api_key = resolve_api_key(&state, &endpoint);
    let started = Instant::now();
    let result = relay::models_list(
        &state.supervisor,
        &endpoint.base_url,
        api_key,
        None,
        endpoint.timeout_seconds as u32,
        endpoint.tls_verify,
    )
    .await;
    let tested_at = chrono::Utc::now().to_rfc3339();
    match result {
        Ok(list) => {
            state
                .db
                .replace_discovered_models(&id, &relay::models_for_upsert(&list.models))
                .await?;
            state.db.update_test_status(&id, "ok", &tested_at).await?;
            let rows = state.db.list_models(&id).await?;
            Ok(Json(json!({
                "ok": true,
                "latency_ms": started.elapsed().as_secs_f64() * 1000.0,
                "models": rows.iter().map(model_json).collect::<Vec<_>>(),
                "fetched_at": list.fetched_at.unwrap_or(tested_at),
            })))
        }
        Err(error) => {
            state
                .db
                .update_test_status(&id, "failed", &tested_at)
                .await?;
            Ok(Json(json!({
                "ok": false,
                "latency_ms": started.elapsed().as_secs_f64() * 1000.0,
                "error": { "code": error.code, "message": error.message },
            })))
        }
    }
}

/// `GET /api/endpoints/{id}/models` (`?refresh=true` forces re-discovery)
pub async fn list_models(
    State(state): State<SharedState>,
    Path(id): Path<String>,
    Query(query): Query<ModelsQuery>,
) -> Result<Json<Value>, ApiError> {
    let endpoint = load_endpoint(&state, &id).await?;
    let cached = state.db.list_models(&id).await?;
    let refresh = query.refresh.unwrap_or(false);

    if !refresh && !cached.is_empty() {
        let fetched_at = cached.iter().filter_map(|m| m.last_seen_at.clone()).max();
        return Ok(Json(json!({
            "models": cached.iter().map(model_json).collect::<Vec<_>>(),
            "fetched_at": fetched_at,
        })));
    }

    let api_key = resolve_api_key(&state, &endpoint);
    let result = relay::models_list(
        &state.supervisor,
        &endpoint.base_url,
        api_key,
        None,
        15,
        endpoint.tls_verify,
    )
    .await;
    match result {
        Ok(list) => {
            state
                .db
                .replace_discovered_models(&id, &relay::models_for_upsert(&list.models))
                .await?;
            let rows = state.db.list_models(&id).await?;
            Ok(Json(json!({
                "models": rows.iter().map(model_json).collect::<Vec<_>>(),
                "fetched_at": list
                    .fetched_at
                    .unwrap_or_else(|| chrono::Utc::now().to_rfc3339()),
            })))
        }
        Err(e) if !cached.is_empty() => Ok(Json(json!({
            "models": cached.iter().map(model_json).collect::<Vec<_>>(),
            "fetched_at": Value::Null,
            "warning": e.message,
        }))),
        Err(e) => Err(e),
    }
}

/// `POST /api/endpoints/{id}/models` — add a manual entry (idempotent).
pub async fn add_model(
    State(state): State<SharedState>,
    Path(id): Path<String>,
    Json(body): Json<AddModel>,
) -> Result<(StatusCode, Json<Value>), ApiError> {
    load_endpoint(&state, &id).await?;
    let model_id = body.model_id.trim();
    if model_id.is_empty() {
        return Err(ApiError::bad_request("model_id must not be empty"));
    }
    let row = state.db.upsert_model(&id, model_id, "manual", None).await?;
    Ok((
        StatusCode::CREATED,
        Json(json!({ "model": model_json(&row) })),
    ))
}

/// `PATCH /api/endpoints/{id}/models/{model_id}` — set hidden flag.
pub async fn patch_model(
    State(state): State<SharedState>,
    Path((id, model_id)): Path<(String, String)>,
    Json(body): Json<PatchModel>,
) -> Result<Json<Value>, ApiError> {
    load_endpoint(&state, &id).await?;
    let row = state
        .db
        .set_model_hidden(&id, &model_id, body.hidden)
        .await?
        .ok_or_else(|| {
            ApiError::not_found(format!("model {model_id} not found for endpoint {id}"))
        })?;
    Ok(Json(json!({ "model": model_json(&row) })))
}

pub async fn set_all_models_hidden(
    State(state): State<SharedState>,
    Path(id): Path<String>,
    Json(body): Json<PatchModel>,
) -> Result<Json<Value>, ApiError> {
    load_endpoint(&state, &id).await?;
    let rows = state.db.set_all_models_hidden(&id, body.hidden).await?;
    Ok(Json(
        json!({ "models": rows.iter().map(model_json).collect::<Vec<_>>() }),
    ))
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
        let test_state = crate::state::test_state_with_fake_sidecar("tok");
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

    async fn create(rig: &Rig, api_key: Option<&str>) -> Value {
        let body = match api_key {
            Some(key) => {
                json!({"name": "Local", "base_url": "http://127.0.0.1:1234", "api_key": key})
            }
            None => json!({"name": "Local", "base_url": "http://127.0.0.1:1234"}),
        };
        let res = rig
            .app
            .clone()
            .oneshot(request("POST", "/api/endpoints", Some(body)))
            .await
            .unwrap();
        let (status, value) = json_response(res).await;
        assert_eq!(status, StatusCode::CREATED, "create failed: {value}");
        value["endpoint"].clone()
    }

    #[tokio::test]
    async fn create_with_api_key_never_returns_key() {
        let rig = rig();
        let endpoint = create(&rig, Some("sk-super-secret")).await;
        let id = endpoint["id"].as_str().unwrap();
        assert_eq!(endpoint["has_api_key"], json!(true));
        // The raw key must not appear anywhere in the response body.
        assert!(!endpoint.to_string().contains("sk-super-secret"));
        // But it must be in the (in-memory) keychain under the endpoint id.
        assert_eq!(
            rig.test_state.secrets.get(id).as_deref(),
            Some("sk-super-secret")
        );

        // List endpoint returns it too, still without the key.
        let res = rig
            .app
            .clone()
            .oneshot(request("GET", "/api/endpoints", None))
            .await
            .unwrap();
        let (status, value) = json_response(res).await;
        assert_eq!(status, StatusCode::OK);
        assert_eq!(value["endpoints"].as_array().unwrap().len(), 1);
        assert!(!value.to_string().contains("sk-super-secret"));
    }

    #[tokio::test]
    async fn patch_default_model_and_clear_api_key() {
        let rig = rig();
        let endpoint = create(&rig, Some("sk-x")).await;
        let id = endpoint["id"].as_str().unwrap();

        let res = rig
            .app
            .clone()
            .oneshot(request(
                "PATCH",
                &format!("/api/endpoints/{id}"),
                Some(json!({"default_model_id": "qwen3:8b", "api_key": null})),
            ))
            .await
            .unwrap();
        let (status, value) = json_response(res).await;
        assert_eq!(status, StatusCode::OK);
        assert_eq!(value["endpoint"]["default_model_id"], json!("qwen3:8b"));
        assert_eq!(value["endpoint"]["has_api_key"], json!(false));
        assert_eq!(rig.test_state.secrets.get(id), None);
    }

    #[tokio::test]
    async fn endpoint_test_discovers_models_and_updates_status() {
        let rig = rig();
        let endpoint = create(&rig, None).await;
        let id = endpoint["id"].as_str().unwrap();

        let res = rig
            .app
            .clone()
            .oneshot(request("POST", &format!("/api/endpoints/{id}/test"), None))
            .await
            .unwrap();
        let (status, value) = json_response(res).await;
        assert_eq!(status, StatusCode::OK);
        assert_eq!(value["ok"], json!(true));
        assert!(value["latency_ms"].is_number());
        assert_eq!(value["models"].as_array().unwrap().len(), 2);

        let row = rig
            .test_state
            .state
            .db
            .get_endpoint(id)
            .await
            .unwrap()
            .unwrap();
        assert_eq!(row.last_test_status.as_deref(), Some("ok"));
        assert!(row.last_tested_at.is_some());
        assert_eq!(
            rig.test_state.state.db.list_models(id).await.unwrap().len(),
            2
        );
    }

    #[tokio::test]
    async fn models_refresh_manual_add_hide_and_cache_fallback() {
        let rig = rig();
        let endpoint = create(&rig, None).await;
        let id = endpoint["id"].as_str().unwrap();

        // Manual entry first.
        let res = rig
            .app
            .clone()
            .oneshot(request(
                "POST",
                &format!("/api/endpoints/{id}/models"),
                Some(json!({"model_id": "manual-1"})),
            ))
            .await
            .unwrap();
        let (status, value) = json_response(res).await;
        assert_eq!(status, StatusCode::CREATED);
        assert_eq!(value["model"]["source"], json!("manual"));

        // Refresh discovers fake-sidecar models; manual entry is kept.
        let res = rig
            .app
            .clone()
            .oneshot(request(
                "GET",
                &format!("/api/endpoints/{id}/models?refresh=true"),
                None,
            ))
            .await
            .unwrap();
        let (status, value) = json_response(res).await;
        assert_eq!(status, StatusCode::OK);
        let models = value["models"].as_array().unwrap();
        let ids: Vec<&str> = models.iter().map(|m| m["id"].as_str().unwrap()).collect();
        assert!(ids.contains(&"fake-model-1"), "ids: {ids:?}");
        assert!(ids.contains(&"fake-model-2"), "ids: {ids:?}");
        assert!(ids.contains(&"manual-1"), "ids: {ids:?}");
        assert!(value["fetched_at"].is_string());

        // Hide a discovered model; refresh must preserve the hidden flag.
        let res = rig
            .app
            .clone()
            .oneshot(request(
                "PATCH",
                &format!("/api/endpoints/{id}/models/fake-model-1"),
                Some(json!({"hidden": true})),
            ))
            .await
            .unwrap();
        let (status, value) = json_response(res).await;
        assert_eq!(status, StatusCode::OK);
        assert_eq!(value["model"]["hidden"], json!(true));

        let res = rig
            .app
            .clone()
            .oneshot(request(
                "GET",
                &format!("/api/endpoints/{id}/models?refresh=true"),
                None,
            ))
            .await
            .unwrap();
        let (_, value) = json_response(res).await;
        let models = value["models"].as_array().unwrap();
        let hidden = models.iter().find(|m| m["id"] == "fake-model-1").unwrap();
        assert_eq!(hidden["hidden"], json!(true));

        // Non-refresh read is served from cache.
        let res = rig
            .app
            .clone()
            .oneshot(request("GET", &format!("/api/endpoints/{id}/models"), None))
            .await
            .unwrap();
        let (status, value) = json_response(res).await;
        assert_eq!(status, StatusCode::OK);
        assert_eq!(value["models"].as_array().unwrap().len(), 3);
    }

    #[tokio::test]
    async fn delete_cascades_and_removes_keychain_entry() {
        let rig = rig();
        let endpoint = create(&rig, Some("sk-to-delete")).await;
        let id = endpoint["id"].as_str().unwrap();
        rig.app
            .clone()
            .oneshot(request(
                "POST",
                &format!("/api/endpoints/{id}/models"),
                Some(json!({"model_id": "m-1"})),
            ))
            .await
            .unwrap();

        let res = rig
            .app
            .clone()
            .oneshot(request("DELETE", &format!("/api/endpoints/{id}"), None))
            .await
            .unwrap();
        assert_eq!(res.status(), StatusCode::NO_CONTENT);
        assert_eq!(rig.test_state.secrets.get(id), None);
        assert!(
            rig.test_state
                .state
                .db
                .list_models(id)
                .await
                .unwrap()
                .is_empty(),
            "models must cascade"
        );

        // Second delete -> 404 with error body.
        let res = rig
            .app
            .clone()
            .oneshot(request("DELETE", &format!("/api/endpoints/{id}"), None))
            .await
            .unwrap();
        let (status, value) = json_response(res).await;
        assert_eq!(status, StatusCode::NOT_FOUND);
        assert_eq!(value["error"]["code"], json!("not_found"));
    }
}
