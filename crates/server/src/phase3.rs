//! Phase 3 local persistence APIs: projects, conversations, messages, search,
//! and the enabled provider/model catalog used by chat model pickers.

use axum::extract::{Path, Query, State};
use axum::http::StatusCode;
use axum::Json;
use serde::Deserialize;
use serde_json::{json, Value};

use crate::db::{ConversationRow, ProjectRow};
use crate::error::ApiError;
use crate::state::SharedState;

fn deserialize_some<'de, D, T>(deserializer: D) -> Result<Option<T>, D::Error>
where
    D: serde::Deserializer<'de>,
    T: serde::Deserialize<'de>,
{
    T::deserialize(deserializer).map(Some)
}

#[derive(Debug, Deserialize)]
pub struct CreateProject {
    name: String,
    #[serde(default)]
    instructions: String,
    #[serde(default)]
    endpoint_id: Option<String>,
    #[serde(default)]
    model_id: Option<String>,
}

#[derive(Debug, Deserialize)]
pub struct PatchProject {
    #[serde(default)]
    name: Option<String>,
    #[serde(default)]
    instructions: Option<String>,
    #[serde(default, deserialize_with = "deserialize_some")]
    endpoint_id: Option<Option<String>>,
    #[serde(default, deserialize_with = "deserialize_some")]
    model_id: Option<Option<String>>,
}

#[derive(Debug, Deserialize)]
pub struct ConversationQuery {
    #[serde(default)]
    project_id: Option<String>,
    #[serde(default)]
    archived: bool,
}

#[derive(Debug, Deserialize)]
pub struct CreateConversation {
    #[serde(default)]
    project_id: Option<String>,
    title: String,
    #[serde(default)]
    endpoint_id: Option<String>,
    #[serde(default)]
    model_id: Option<String>,
}

#[derive(Debug, Deserialize)]
pub struct PatchConversation {
    #[serde(default, deserialize_with = "deserialize_some")]
    project_id: Option<Option<String>>,
    #[serde(default)]
    title: Option<String>,
    #[serde(default, deserialize_with = "deserialize_some")]
    endpoint_id: Option<Option<String>>,
    #[serde(default, deserialize_with = "deserialize_some")]
    model_id: Option<Option<String>>,
    #[serde(default)]
    archived: Option<bool>,
}

#[derive(Debug, Deserialize)]
pub struct SearchQuery {
    q: String,
}

#[derive(Debug, Deserialize)]
pub struct ModelsQuery {
    #[serde(default)]
    enabled: Option<bool>,
}

async fn load_project(state: &SharedState, id: &str) -> Result<ProjectRow, ApiError> {
    state
        .db
        .get_project(id)
        .await?
        .ok_or_else(|| ApiError::not_found(format!("project {id} not found")))
}

async fn load_conversation(state: &SharedState, id: &str) -> Result<ConversationRow, ApiError> {
    state
        .db
        .get_conversation(id)
        .await?
        .ok_or_else(|| ApiError::not_found(format!("conversation {id} not found")))
}

async fn validate_project_id(
    state: &SharedState,
    project_id: Option<&str>,
) -> Result<(), ApiError> {
    if let Some(id) = project_id {
        load_project(state, id).await?;
    }
    Ok(())
}

async fn validate_model_selection(
    state: &SharedState,
    endpoint_id: Option<&str>,
    model_id: Option<&str>,
) -> Result<(), ApiError> {
    if model_id.is_some() && endpoint_id.is_none() {
        return Err(ApiError::bad_request(
            "model_id requires an endpoint_id/provider",
        ));
    }
    let Some(endpoint_id) = endpoint_id else {
        return Ok(());
    };
    state
        .db
        .get_endpoint(endpoint_id)
        .await?
        .ok_or_else(|| ApiError::bad_request(format!("provider {endpoint_id} not found")))?;
    if let Some(model_id) = model_id {
        let model = state
            .db
            .get_model(endpoint_id, model_id)
            .await?
            .ok_or_else(|| {
                ApiError::bad_request(format!(
                    "model {model_id} not found for provider {endpoint_id}"
                ))
            })?;
        if model.hidden {
            return Err(ApiError::bad_request(format!(
                "model {model_id} is disabled for provider {endpoint_id}"
            )));
        }
    }
    Ok(())
}

pub async fn list_projects(State(state): State<SharedState>) -> Result<Json<Value>, ApiError> {
    Ok(Json(json!({ "projects": state.db.list_projects().await? })))
}

pub async fn create_project(
    State(state): State<SharedState>,
    Json(body): Json<CreateProject>,
) -> Result<(StatusCode, Json<Value>), ApiError> {
    let name = body.name.trim();
    if name.is_empty() {
        return Err(ApiError::bad_request("name is required"));
    }
    validate_model_selection(
        &state,
        body.endpoint_id.as_deref(),
        body.model_id.as_deref(),
    )
    .await?;
    let now = chrono::Utc::now().to_rfc3339();
    let project = ProjectRow {
        id: uuid::Uuid::now_v7().to_string(),
        name: name.to_string(),
        instructions: body.instructions.trim().to_string(),
        endpoint_id: body.endpoint_id,
        model_id: body.model_id,
        created_at: now.clone(),
        updated_at: now,
    };
    state.db.insert_project(&project).await?;
    crate::events::data_changed(&state, json!({ "entity": "project", "id": project.id }));
    Ok((StatusCode::CREATED, Json(json!({ "project": project }))))
}

pub async fn get_project(
    State(state): State<SharedState>,
    Path(id): Path<String>,
) -> Result<Json<Value>, ApiError> {
    Ok(Json(json!({ "project": load_project(&state, &id).await? })))
}

pub async fn patch_project(
    State(state): State<SharedState>,
    Path(id): Path<String>,
    Json(body): Json<PatchProject>,
) -> Result<Json<Value>, ApiError> {
    let mut project = load_project(&state, &id).await?;
    if let Some(name) = body.name {
        let name = name.trim();
        if name.is_empty() {
            return Err(ApiError::bad_request("name must not be empty"));
        }
        project.name = name.to_string();
    }
    if let Some(instructions) = body.instructions {
        project.instructions = instructions.trim().to_string();
    }
    if let Some(endpoint_id) = body.endpoint_id {
        if endpoint_id != project.endpoint_id {
            project.model_id = None;
        }
        project.endpoint_id = endpoint_id;
    }
    if let Some(model_id) = body.model_id {
        project.model_id = model_id;
    }
    validate_model_selection(
        &state,
        project.endpoint_id.as_deref(),
        project.model_id.as_deref(),
    )
    .await?;
    project.updated_at = chrono::Utc::now().to_rfc3339();
    state.db.update_project(&project).await?;
    crate::events::data_changed(&state, json!({ "entity": "project", "id": project.id }));
    Ok(Json(json!({ "project": project })))
}

pub async fn delete_project(
    State(state): State<SharedState>,
    Path(id): Path<String>,
) -> Result<StatusCode, ApiError> {
    if !state.db.delete_project(&id).await? {
        return Err(ApiError::not_found(format!("project {id} not found")));
    }
    crate::events::data_changed(&state, json!({ "entity": "project", "id": id }));
    Ok(StatusCode::NO_CONTENT)
}

pub async fn list_conversations(
    State(state): State<SharedState>,
    Query(query): Query<ConversationQuery>,
) -> Result<Json<Value>, ApiError> {
    validate_project_id(&state, query.project_id.as_deref()).await?;
    let conversations = state
        .db
        .list_conversations(query.project_id.as_deref(), query.archived)
        .await?;
    Ok(Json(json!({ "conversations": conversations })))
}

pub async fn create_conversation(
    State(state): State<SharedState>,
    Json(body): Json<CreateConversation>,
) -> Result<(StatusCode, Json<Value>), ApiError> {
    let title = body.title.trim();
    if title.is_empty() {
        return Err(ApiError::bad_request("title is required"));
    }
    validate_project_id(&state, body.project_id.as_deref()).await?;
    validate_model_selection(
        &state,
        body.endpoint_id.as_deref(),
        body.model_id.as_deref(),
    )
    .await?;
    let now = chrono::Utc::now().to_rfc3339();
    let conversation = ConversationRow {
        id: uuid::Uuid::now_v7().to_string(),
        project_id: body.project_id,
        title: title.to_string(),
        endpoint_id: body.endpoint_id,
        model_id: body.model_id,
        archived_at: None,
        created_at: now.clone(),
        updated_at: now,
    };
    state.db.insert_conversation(&conversation).await?;
    crate::events::data_changed(
        &state,
        json!({ "entity": "conversation", "id": conversation.id, "conversation_id": conversation.id }),
    );
    Ok((
        StatusCode::CREATED,
        Json(json!({ "conversation": conversation })),
    ))
}

pub async fn get_conversation(
    State(state): State<SharedState>,
    Path(id): Path<String>,
) -> Result<Json<Value>, ApiError> {
    Ok(Json(json!({
        "conversation": load_conversation(&state, &id).await?
    })))
}

pub async fn patch_conversation(
    State(state): State<SharedState>,
    Path(id): Path<String>,
    Json(body): Json<PatchConversation>,
) -> Result<Json<Value>, ApiError> {
    let mut conversation = load_conversation(&state, &id).await?;
    if let Some(project_id) = body.project_id {
        validate_project_id(&state, project_id.as_deref()).await?;
        conversation.project_id = project_id;
    }
    if let Some(title) = body.title {
        let title = title.trim();
        if title.is_empty() {
            return Err(ApiError::bad_request("title must not be empty"));
        }
        conversation.title = title.to_string();
    }
    if let Some(endpoint_id) = body.endpoint_id {
        if endpoint_id != conversation.endpoint_id {
            conversation.model_id = None;
        }
        conversation.endpoint_id = endpoint_id;
    }
    if let Some(model_id) = body.model_id {
        conversation.model_id = model_id;
    }
    if let Some(archived) = body.archived {
        conversation.archived_at = archived.then(|| chrono::Utc::now().to_rfc3339());
    }
    validate_model_selection(
        &state,
        conversation.endpoint_id.as_deref(),
        conversation.model_id.as_deref(),
    )
    .await?;
    conversation.updated_at = chrono::Utc::now().to_rfc3339();
    state.db.update_conversation(&conversation).await?;
    crate::events::data_changed(
        &state,
        json!({ "entity": "conversation", "id": conversation.id, "conversation_id": conversation.id }),
    );
    Ok(Json(json!({ "conversation": conversation })))
}

pub async fn delete_conversation(
    State(state): State<SharedState>,
    Path(id): Path<String>,
) -> Result<StatusCode, ApiError> {
    if !state.db.delete_conversation(&id).await? {
        return Err(ApiError::not_found(format!("conversation {id} not found")));
    }
    crate::events::data_changed(
        &state,
        json!({ "entity": "conversation", "id": id, "conversation_id": id }),
    );
    Ok(StatusCode::NO_CONTENT)
}

pub async fn list_messages(
    State(state): State<SharedState>,
    Path(id): Path<String>,
) -> Result<Json<Value>, ApiError> {
    load_conversation(&state, &id).await?;
    Ok(Json(json!({
        "messages": state.db.list_messages(&id).await?
    })))
}

pub async fn search(
    State(state): State<SharedState>,
    Query(query): Query<SearchQuery>,
) -> Result<Json<Value>, ApiError> {
    if query.q.trim().is_empty() {
        return Err(ApiError::bad_request("q must not be empty"));
    }
    Ok(Json(json!({
        "conversations": state.db.search_conversations(&query.q).await?
    })))
}

pub async fn list_models(
    State(state): State<SharedState>,
    Query(query): Query<ModelsQuery>,
) -> Result<Json<Value>, ApiError> {
    Ok(Json(json!({
        "models": state.db.list_provider_models(query.enabled).await?
    })))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::db::MessageRow;
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
        let body = body
            .map(|value| Body::from(serde_json::to_vec(&value).unwrap()))
            .unwrap_or_else(Body::empty);
        let mut request = builder.body(body).unwrap();
        request
            .extensions_mut()
            .insert(ConnectInfo(SocketAddr::from(([127, 0, 0, 1], 40_001))));
        request
    }

    async fn send(
        app: &Router,
        method: &str,
        path: &str,
        body: Option<Value>,
    ) -> (StatusCode, Value) {
        let response = app
            .clone()
            .oneshot(request(method, path, body))
            .await
            .unwrap();
        let status = response.status();
        let bytes = response.into_body().collect().await.unwrap().to_bytes();
        let value = serde_json::from_slice(&bytes).unwrap_or(Value::Null);
        (status, value)
    }

    async fn create_project(app: &Router) -> Value {
        let (status, value) = send(
            app,
            "POST",
            "/api/projects",
            Some(json!({"name": "Native Mind", "instructions": "Be concise"})),
        )
        .await;
        assert_eq!(status, StatusCode::CREATED, "{value}");
        value["project"].clone()
    }

    #[tokio::test]
    async fn project_conversation_message_search_archive_and_delete_routes() {
        let rig = rig();
        let project = create_project(&rig.app).await;
        let project_id = project["id"].as_str().unwrap();

        let (status, value) = send(
            &rig.app,
            "POST",
            "/api/conversations",
            Some(json!({"project_id": project_id, "title": "SQLite design"})),
        )
        .await;
        assert_eq!(status, StatusCode::CREATED, "{value}");
        let conversation_id = value["conversation"]["id"].as_str().unwrap().to_string();

        rig.test_state
            .state
            .db
            .insert_message(&MessageRow {
                id: "message-1".to_string(),
                conversation_id: conversation_id.clone(),
                role: "user".to_string(),
                content: "Build durable local search".to_string(),
                status: "completed".to_string(),
                created_at: chrono::Utc::now().to_rfc3339(),
            })
            .await
            .unwrap();
        let (status, value) = send(
            &rig.app,
            "GET",
            &format!("/api/conversations/{conversation_id}/messages"),
            None,
        )
        .await;
        assert_eq!(status, StatusCode::OK, "{value}");
        assert_eq!(value["messages"][0]["status"], "completed");

        let (status, value) = send(
            &rig.app,
            "GET",
            &format!("/api/conversations?project_id={project_id}&archived=false"),
            None,
        )
        .await;
        assert_eq!(status, StatusCode::OK);
        assert_eq!(value["conversations"].as_array().unwrap().len(), 1);

        let (status, value) = send(&rig.app, "GET", "/api/search?q=durable", None).await;
        assert_eq!(status, StatusCode::OK);
        assert_eq!(value["conversations"][0]["id"], conversation_id);

        let (status, value) = send(
            &rig.app,
            "PATCH",
            &format!("/api/conversations/{conversation_id}"),
            Some(json!({"title": "Renamed", "archived": true})),
        )
        .await;
        assert_eq!(status, StatusCode::OK, "{value}");
        assert!(value["conversation"]["archived_at"].is_string());

        let (_, active) = send(&rig.app, "GET", "/api/conversations", None).await;
        assert!(active["conversations"].as_array().unwrap().is_empty());
        let (_, archived) = send(&rig.app, "GET", "/api/conversations?archived=true", None).await;
        assert_eq!(archived["conversations"].as_array().unwrap().len(), 1);

        let (status, _) = send(
            &rig.app,
            "DELETE",
            &format!("/api/projects/{project_id}"),
            None,
        )
        .await;
        assert_eq!(status, StatusCode::NO_CONTENT);
        let (_, conversation) = send(
            &rig.app,
            "GET",
            &format!("/api/conversations/{conversation_id}"),
            None,
        )
        .await;
        assert!(conversation["conversation"]["project_id"].is_null());

        let (status, _) = send(
            &rig.app,
            "DELETE",
            &format!("/api/conversations/{conversation_id}"),
            None,
        )
        .await;
        assert_eq!(status, StatusCode::NO_CONTENT);
        let (status, _) = send(
            &rig.app,
            "GET",
            &format!("/api/conversations/{conversation_id}"),
            None,
        )
        .await;
        assert_eq!(status, StatusCode::NOT_FOUND);
    }

    #[tokio::test]
    async fn provider_model_catalog_filters_and_mutations_validate_selection() {
        let rig = rig();
        let now = chrono::Utc::now().to_rfc3339();
        rig.test_state
            .state
            .db
            .insert_endpoint(&crate::db::EndpointRow {
                id: "ep-1".to_string(),
                name: "Local provider".to_string(),
                base_url: "http://127.0.0.1:11434/v1".to_string(),
                timeout_seconds: 15,
                tls_verify: true,
                has_api_key: false,
                default_model_id: Some("enabled-model".to_string()),
                last_test_status: None,
                last_tested_at: None,
                created_at: now.clone(),
                updated_at: now,
            })
            .await
            .unwrap();
        for model in ["enabled-model", "disabled-model"] {
            rig.test_state
                .state
                .db
                .upsert_model("ep-1", model, "discovered", None)
                .await
                .unwrap();
        }
        rig.test_state
            .state
            .db
            .set_model_hidden("ep-1", "disabled-model", true)
            .await
            .unwrap();

        let (status, value) = send(&rig.app, "GET", "/api/models?enabled=true", None).await;
        assert_eq!(status, StatusCode::OK);
        assert_eq!(value["models"].as_array().unwrap().len(), 1);
        assert_eq!(value["models"][0]["provider_id"], "ep-1");
        assert_eq!(value["models"][0]["model_id"], "enabled-model");
        assert_eq!(value["models"][0]["enabled"], true);

        let (status, value) = send(
            &rig.app,
            "POST",
            "/api/projects",
            Some(json!({
                "name": "Bad",
                "endpoint_id": "ep-1",
                "model_id": "disabled-model"
            })),
        )
        .await;
        assert_eq!(status, StatusCode::BAD_REQUEST);
        assert_eq!(value["error"]["code"], "bad_request");

        let (status, value) = send(
            &rig.app,
            "POST",
            "/api/projects",
            Some(json!({
                "name": "Good",
                "endpoint_id": "ep-1",
                "model_id": "enabled-model"
            })),
        )
        .await;
        assert_eq!(status, StatusCode::CREATED, "{value}");
        let project_id = value["project"]["id"].as_str().unwrap();

        let (status, value) = send(
            &rig.app,
            "PATCH",
            &format!("/api/projects/{project_id}"),
            Some(json!({"endpoint_id": null})),
        )
        .await;
        assert_eq!(status, StatusCode::OK, "{value}");
        assert!(value["project"]["endpoint_id"].is_null());
        assert!(value["project"]["model_id"].is_null());
    }

    #[tokio::test]
    async fn conversation_create_emits_data_changed() {
        let rig = rig();
        let mut events = rig.test_state.state.host_events.subscribe();
        let (status, value) = send(
            &rig.app,
            "POST",
            "/api/conversations",
            Some(json!({"title": "Synced chat"})),
        )
        .await;
        assert_eq!(status, StatusCode::CREATED, "{value}");
        let conversation_id = value["conversation"]["id"].as_str().unwrap();

        let env = tokio::time::timeout(std::time::Duration::from_secs(1), events.recv())
            .await
            .expect("data.changed emitted")
            .expect("channel open");
        assert_eq!(env.kind, "data.changed");
        assert_eq!(env.protocol, "1.0");
        assert_eq!(env.payload["entity"], json!("conversation"));
        assert_eq!(env.payload["id"], json!(conversation_id));
        assert_eq!(env.payload["conversation_id"], json!(conversation_id));
    }

    #[tokio::test]
    async fn project_patch_and_delete_emit_data_changed() {
        let rig = rig();
        let project = create_project(&rig.app).await;
        let project_id = project["id"].as_str().unwrap().to_string();
        let mut events = rig.test_state.state.host_events.subscribe();

        let (status, _) = send(
            &rig.app,
            "PATCH",
            &format!("/api/projects/{project_id}"),
            Some(json!({"name": "Renamed"})),
        )
        .await;
        assert_eq!(status, StatusCode::OK);
        let env = tokio::time::timeout(std::time::Duration::from_secs(1), events.recv())
            .await
            .unwrap()
            .unwrap();
        assert_eq!(env.kind, "data.changed");
        assert_eq!(env.payload["entity"], json!("project"));
        assert_eq!(env.payload["id"], json!(project_id));

        let (status, _) = send(
            &rig.app,
            "DELETE",
            &format!("/api/projects/{project_id}"),
            None,
        )
        .await;
        assert_eq!(status, StatusCode::NO_CONTENT);
        let env = tokio::time::timeout(std::time::Duration::from_secs(1), events.recv())
            .await
            .unwrap()
            .unwrap();
        assert_eq!(env.payload["entity"], json!("project"));
    }

    #[tokio::test]
    async fn invalid_references_and_message_shapes_return_client_errors() {
        let rig = rig();
        let (status, _) = send(
            &rig.app,
            "POST",
            "/api/conversations",
            Some(json!({"project_id": "missing", "title": "Chat"})),
        )
        .await;
        assert_eq!(status, StatusCode::NOT_FOUND);

        let (status, _) = send(
            &rig.app,
            "POST",
            "/api/conversations",
            Some(json!({"title": "Chat", "model_id": "orphan"})),
        )
        .await;
        assert_eq!(status, StatusCode::BAD_REQUEST);

        let (_, conversation) = send(
            &rig.app,
            "POST",
            "/api/conversations",
            Some(json!({"title": "Chat"})),
        )
        .await;
        let id = conversation["conversation"]["id"].as_str().unwrap();
        let (status, _) = send(
            &rig.app,
            "POST",
            &format!("/api/conversations/{id}/messages"),
            Some(json!({"content": ""})),
        )
        .await;
        assert_eq!(status, StatusCode::BAD_REQUEST);
        let (status, _) = send(&rig.app, "GET", "/api/search?q=", None).await;
        assert_eq!(status, StatusCode::BAD_REQUEST);
    }
}
