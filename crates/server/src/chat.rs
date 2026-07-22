//! Persistent chat run orchestration: resolve model inheritance, store the
//! user message/run, invoke Strands, and persist the streamed terminal state.

use axum::extract::{Path, State};
use axum::http::StatusCode;
use axum::Json;
use serde::Deserialize;
use serde_json::{json, Value};

use crate::db::{MessageRow, ModelResolutionError, RunRow};
use crate::error::ApiError;
use crate::protocol::{ChatMessage, RunStart};
use crate::relay;
use crate::state::SharedState;

#[derive(Debug, Deserialize)]
pub struct SendMessage {
    content: String,
    #[serde(default, alias = "endpoint_id")]
    provider_id: Option<String>,
    #[serde(default)]
    model_id: Option<String>,
}

fn resolution_error(error: ModelResolutionError) -> ApiError {
    match error {
        ModelResolutionError::ConversationNotFound(_) => ApiError::not_found(error.to_string()),
        ModelResolutionError::Database(inner) => ApiError::from(inner),
        _ => ApiError::bad_request(error.to_string()),
    }
}

fn now() -> String {
    chrono::Utc::now().to_rfc3339()
}

pub async fn send_message(
    State(state): State<SharedState>,
    Path(conversation_id): Path<String>,
    Json(body): Json<SendMessage>,
) -> Result<(StatusCode, Json<Value>), ApiError> {
    let content = body.content.trim();
    if content.is_empty() {
        return Err(ApiError::bad_request("content must not be empty"));
    }
    if body.provider_id.is_some() != body.model_id.is_some() {
        return Err(ApiError::bad_request(
            "provider_id and model_id must be supplied together",
        ));
    }

    let mut conversation = state
        .db
        .get_conversation(&conversation_id)
        .await?
        .ok_or_else(|| ApiError::not_found(format!("conversation {conversation_id} not found")))?;
    if conversation.archived_at.is_some() {
        return Err(ApiError::bad_request(
            "archived conversations are read-only",
        ));
    }

    // Choosing a model in chat is the conversation-level override.
    if let (Some(provider_id), Some(model_id)) = (body.provider_id, body.model_id) {
        conversation.endpoint_id = Some(provider_id);
        conversation.model_id = Some(model_id);
        conversation.updated_at = now();
        state.db.update_conversation(&conversation).await?;
    }
    let resolved = state
        .db
        .resolve_conversation_model(&conversation_id)
        .await
        .map_err(resolution_error)?;
    let history = state.db.list_messages(&conversation_id).await?;
    let project_prompt = match conversation.project_id.as_deref() {
        Some(project_id) => state
            .db
            .get_project(project_id)
            .await?
            .map(|project| project.instructions)
            .filter(|instructions| !instructions.is_empty()),
        None => None,
    };
    let knowledge_context = crate::knowledge::context_for_prompt(&state, content).await?;
    let system_prompt = match (project_prompt, knowledge_context) {
        (Some(project), Some(knowledge)) => Some(format!("{project}\n\n{knowledge}")),
        (Some(project), None) => Some(project),
        (None, Some(knowledge)) => Some(knowledge),
        (None, None) => None,
    };
    let enabled_tools = crate::tools::enabled_tool_ids(&state).await?;

    let created_at = now();
    let user_message = MessageRow {
        id: uuid::Uuid::now_v7().to_string(),
        conversation_id: conversation_id.clone(),
        role: "user".to_string(),
        content: content.to_string(),
        status: "completed".to_string(),
        created_at: created_at.clone(),
    };
    state.db.insert_message(&user_message).await?;

    let run_id = uuid::Uuid::now_v7().to_string();
    let mut run = RunRow {
        id: run_id.clone(),
        conversation_id: conversation_id.clone(),
        user_message_id: Some(user_message.id.clone()),
        assistant_message_id: None,
        status: "running".to_string(),
        endpoint_id: Some(resolved.provider_id.clone()),
        model_id: Some(resolved.model_id.clone()),
        started_at: created_at,
        completed_at: None,
        usage_json: None,
        error_json: None,
    };
    state.db.insert_run(&run).await?;

    let api_key = state.secrets.get(&resolved.provider_id);
    let payload = RunStart {
        run_id: run_id.clone(),
        conversation_id: conversation_id.clone(),
        message_id: user_message.id.clone(),
        prompt: content.to_string(),
        history: history
            .into_iter()
            .filter(|message| matches!(message.role.as_str(), "user" | "assistant"))
            .map(|message| ChatMessage {
                role: message.role,
                content: message.content,
            })
            .collect(),
        system_prompt,
        enabled_tools,
        model: crate::protocol::RunModel {
            base_url: resolved.provider_url,
            model_id: resolved.model_id,
            api_key,
        },
    };

    // Subscribe before start so even an extremely fast local model cannot
    // publish its first delta before persistence begins listening.
    let mut events = state.supervisor.events().subscribe();
    let (request_id, started) = match relay::run_start(&state.supervisor, payload).await {
        Ok(started) => started,
        Err(error) => {
            run.status = "failed".to_string();
            run.completed_at = Some(now());
            run.error_json =
                Some(json!({"code": &error.code, "message": &error.message}).to_string());
            state.db.update_run(&run).await?;
            return Err(error);
        }
    };
    if started.run_id != run_id {
        return Err(ApiError::bad_gateway(
            "bad_sidecar_response",
            "runtime acknowledged a different run id",
        ));
    }

    let persistence_state = state.clone();
    let persistence_request_id = request_id.clone();
    let persistence_run_id = run_id.clone();
    tokio::spawn(async move {
        let mut assistant_text = String::new();
        loop {
            let event = match events.recv().await {
                Ok(event) => event,
                Err(tokio::sync::broadcast::error::RecvError::Lagged(_)) => continue,
                Err(tokio::sync::broadcast::error::RecvError::Closed) => break,
            };
            if event.request_id != persistence_request_id
                || event.payload.get("run_id").and_then(Value::as_str)
                    != Some(persistence_run_id.as_str())
            {
                continue;
            }
            match event.kind.as_str() {
                "run.text_delta" => {
                    if let Some(text) = event.payload.get("text").and_then(Value::as_str) {
                        assistant_text.push_str(text);
                    }
                }
                "run.completed" | "run.failed" => {
                    persist_terminal_run(
                        &persistence_state,
                        &persistence_run_id,
                        &conversation_id,
                        &assistant_text,
                        &event,
                    )
                    .await;
                    break;
                }
                _ => {}
            }
        }
    });

    Ok((
        StatusCode::CREATED,
        Json(json!({
            "message": user_message,
            "run": { "id": run_id, "request_id": request_id },
        })),
    ))
}

async fn persist_terminal_run(
    state: &SharedState,
    run_id: &str,
    conversation_id: &str,
    assistant_text: &str,
    event: &agentgpt_supervisor::protocol::Envelope,
) {
    let Ok(Some(mut run)) = state.db.get_run(run_id).await else {
        return;
    };
    // `cancelled` (user abort) and `sidecar_crashed` (synthetic terminal event
    // from the supervisor after a runtime exit) both mean the run was cut
    // short rather than failed by the model.
    let interrupted = event.kind == "run.failed"
        && matches!(
            event.payload.pointer("/error/code").and_then(Value::as_str),
            Some("cancelled") | Some("sidecar_crashed")
        );
    run.status = if event.kind == "run.completed" {
        "completed"
    } else if interrupted {
        "interrupted"
    } else {
        "failed"
    }
    .to_string();
    run.completed_at = Some(now());
    if event.kind == "run.failed" {
        run.error_json = event.payload.get("error").map(Value::to_string);
    } else {
        run.usage_json = event.payload.get("usage").map(Value::to_string);
    }
    if !assistant_text.is_empty() {
        let assistant = MessageRow {
            id: uuid::Uuid::now_v7().to_string(),
            conversation_id: conversation_id.to_string(),
            role: "assistant".to_string(),
            content: assistant_text.to_string(),
            status: run.status.clone(),
            created_at: now(),
        };
        if state.db.insert_message(&assistant).await.is_ok() {
            run.assistant_message_id = Some(assistant.id);
        }
    }
    if state.db.update_run(&run).await.is_ok() {
        // M3: notify other WS clients (small payload, no message content).
        crate::events::data_changed(
            state,
            json!({
                "entity": "message",
                "conversation_id": conversation_id,
                "run_id": run_id,
                "status": run.status,
            }),
        );
    }
}

pub async fn cancel_run(
    State(state): State<SharedState>,
    Path(run_id): Path<String>,
) -> Result<StatusCode, ApiError> {
    let mut run = state
        .db
        .get_run(&run_id)
        .await?
        .ok_or_else(|| ApiError::not_found(format!("run {run_id} not found")))?;
    if run.status != "running" {
        return Ok(StatusCode::NO_CONTENT);
    }
    relay::run_cancel(&state.supervisor, run_id).await?;
    run.status = "interrupted".to_string();
    run.completed_at = Some(now());
    state.db.update_run(&run).await?;
    Ok(StatusCode::NO_CONTENT)
}

#[cfg(test)]
mod tests {
    use super::*;
    use axum::body::Body;
    use axum::extract::ConnectInfo;
    use axum::http::{header, Method, Request};
    use http_body_util::BodyExt;
    use std::net::SocketAddr;
    use tower::ServiceExt;

    fn request(method: Method, path: &str, body: Option<Value>) -> Request<Body> {
        let mut builder = Request::builder().method(method).uri(path);
        if body.is_some() {
            builder = builder.header(header::CONTENT_TYPE, "application/json");
        }
        let mut request = builder
            .body(body.map_or_else(Body::empty, |value| {
                Body::from(serde_json::to_vec(&value).unwrap())
            }))
            .unwrap();
        request
            .extensions_mut()
            .insert(ConnectInfo(SocketAddr::from(([127, 0, 0, 1], 40_001))));
        request
    }

    async fn response_json(response: axum::response::Response) -> (StatusCode, Value) {
        let status = response.status();
        let body = response.into_body().collect().await.unwrap().to_bytes();
        (status, serde_json::from_slice(&body).unwrap_or(Value::Null))
    }

    /// Create an endpoint + conversation against the fake sidecar; returns
    /// (provider_id, conversation_id).
    async fn setup_conversation(app: &axum::Router, db: &crate::db::Db) -> (String, String) {
        let response = app
            .clone()
            .oneshot(request(
                Method::POST,
                "/api/endpoints",
                Some(json!({"name":"Local","base_url":"http://127.0.0.1:1234"})),
            ))
            .await
            .unwrap();
        let (_, provider) = response_json(response).await;
        let provider_id = provider["endpoint"]["id"].as_str().unwrap().to_string();

        // Conversation model selection is validated against the models table.
        db.upsert_model(&provider_id, "fake-model-1", "discovered", None)
            .await
            .unwrap();

        let response = app
            .clone()
            .oneshot(request(
                Method::POST,
                "/api/conversations",
                Some(json!({
                    "title": "New chat",
                    "endpoint_id": provider_id,
                    "model_id": "fake-model-1"
                })),
            ))
            .await
            .unwrap();
        let (_, conversation) = response_json(response).await;
        let conversation_id = conversation["conversation"]["id"]
            .as_str()
            .unwrap_or_else(|| panic!("conversation create failed: {conversation}"))
            .to_string();
        (provider_id, conversation_id)
    }

    async fn wait_for_terminal_status(db: &crate::db::Db, run_id: &str) -> crate::db::RunRow {
        for _ in 0..100 {
            let run = db.get_run(run_id).await.unwrap().unwrap();
            if run.status != "running" {
                return run;
            }
            tokio::time::sleep(std::time::Duration::from_millis(50)).await;
        }
        panic!("run {run_id} stayed 'running' — persistence task leaked?");
    }

    #[tokio::test]
    async fn send_streams_and_persists_assistant_reply() {
        let rig = crate::state::test_state_with_fake_sidecar("token");
        let app = crate::build_router(rig.state.clone());

        let (_, conversation_id) = setup_conversation(&app, &rig.state.db).await;

        let response = app
            .clone()
            .oneshot(request(
                Method::POST,
                &format!("/api/conversations/{conversation_id}/messages"),
                Some(json!({"content":"hello"})),
            ))
            .await
            .unwrap();
        let (status, value) = response_json(response).await;
        assert_eq!(status, StatusCode::CREATED, "{value}");
        let run_id = value["run"]["id"].as_str().unwrap();
        assert!(value["run"]["request_id"].is_string());

        for _ in 0..50 {
            if rig
                .state
                .db
                .list_messages(&conversation_id)
                .await
                .unwrap()
                .len()
                == 2
            {
                break;
            }
            tokio::time::sleep(std::time::Duration::from_millis(10)).await;
        }
        let messages = rig.state.db.list_messages(&conversation_id).await.unwrap();
        assert_eq!(messages.len(), 2);
        assert_eq!(messages[1].role, "assistant");
        assert_eq!(messages[1].content, "fake reply");
        assert_eq!(
            rig.state.db.get_run(run_id).await.unwrap().unwrap().status,
            "completed"
        );
        let run = rig.state.db.get_run(run_id).await.unwrap().unwrap();
        assert_eq!(
            serde_json::from_str::<Value>(run.usage_json.as_deref().unwrap()).unwrap()
                ["total_tokens"],
            15
        );
    }

    #[tokio::test]
    async fn sidecar_crash_mid_run_marks_run_interrupted() {
        // M1: the fake sidecar dies right after the first delta. The
        // supervisor must broadcast a synthetic run.failed so the persistence
        // task terminates and the run does not stay "running" forever.
        let rig = crate::state::test_state_with_fake_sidecar("token");
        let app = crate::build_router(rig.state.clone());
        let (_, conversation_id) = setup_conversation(&app, &rig.state.db).await;
        let mut host_events = rig.state.host_events.subscribe();

        let response = app
            .clone()
            .oneshot(request(
                Method::POST,
                &format!("/api/conversations/{conversation_id}/messages"),
                Some(json!({"content":"trigger-crash"})),
            ))
            .await
            .unwrap();
        let (status, value) = response_json(response).await;
        assert_eq!(status, StatusCode::CREATED, "{value}");
        let run_id = value["run"]["id"].as_str().unwrap().to_string();

        let run = wait_for_terminal_status(&rig.state.db, &run_id).await;
        assert_eq!(run.status, "interrupted");
        assert!(run.completed_at.is_some());
        let error_json = run.error_json.unwrap_or_default();
        assert!(
            error_json.contains("sidecar_crashed"),
            "error_json: {error_json}"
        );

        // The partial delta is persisted as an interrupted assistant message.
        let messages = rig.state.db.list_messages(&conversation_id).await.unwrap();
        assert_eq!(messages.len(), 2);
        assert_eq!(messages[1].status, "interrupted");
        assert_eq!(messages[1].content, "partial");

        // M3: data.changed for the terminal state reached the host channel.
        let event = tokio::time::timeout(std::time::Duration::from_secs(1), async {
            loop {
                let env = host_events.recv().await.unwrap();
                if env.kind == "data.changed" && env.payload["run_id"] == json!(run_id) {
                    break env;
                }
            }
        })
        .await
        .expect("data.changed for run");
        assert_eq!(event.payload["entity"], json!("message"));
        assert_eq!(event.payload["status"], json!("interrupted"));

        // The supervisor observed the exit.
        for _ in 0..50 {
            if rig.state.supervisor.state() == agentgpt_supervisor::SidecarState::NotSpawned {
                break;
            }
            tokio::time::sleep(std::time::Duration::from_millis(20)).await;
        }
        assert_eq!(
            rig.state.supervisor.state(),
            agentgpt_supervisor::SidecarState::NotSpawned
        );
    }
}
