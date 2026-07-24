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
    /// Tool Manager: run in factory mode (registers save_tool only).
    #[serde(default)]
    factory_mode: bool,
    /// Tool Manager revision: the existing tool id to load as context.
    #[serde(default)]
    factory_revision: Option<String>,
    /// Thinking mode for this message: "off" | "high" | "max" (default high).
    #[serde(default)]
    thinking_mode: Option<String>,
    /// Depth preset for thinking_mode=max: "quick" | "standard" | "deep"
    /// (default standard). Ignored for other modes.
    #[serde(default)]
    max_depth: Option<String>,
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

/// The Tool Manager create prompt (mirrors the Python FACTORY_SYSTEM_PROMPT).
const FACTORY_CREATE_PROMPT: &str = "\
You are the Tool Manager. Given the user's request, produce ONE new Strands \
tool by calling the save_tool function EXACTLY ONCE.\n\
\n\
Rules for tool_code:\n\
- It is a complete, self-contained Python 3.12+ module.\n\
- Start with `from strands import tool`.\n\
- Define exactly one function decorated with `@tool`. Its docstring becomes \
the Strands tool description shown to agents — write it clearly.\n\
- End with `TOOL = <function_name>`.\n\
- You may import the Python standard library. To share helpers, import from \
`tools/_lib` using the project's importlib pattern (see existing tools).\n\
- Return a plain string (or JSON-serializable value) from the function.\n\
\n\
Think briefly (1-3 sentences) about what the tool should do, then call \
save_tool with every field filled in. Do not write files; save_tool returns \
the proposal for a human to review.";

/// Build the Tool Manager system prompt. Create mode uses FACTORY_CREATE_PROMPT;
/// revision mode embeds the existing tool's manifest + source as context and
/// instructs the model to return the full revised tool_code. Errors loading
/// the existing tool propagate so the client gets a clear failure instead of
/// a misleading empty-context revision prompt.
async fn factory_system_prompt(
    user_request: &str,
    revision_target: Option<&str>,
    state: &SharedState,
) -> Result<String, ApiError> {
    let (mode_line, context) = match revision_target {
        Some(id) => {
            let s = crate::tools::read_tool_source_public(state, id)?;
            let manifest_json = serde_json::to_string_pretty(&s.manifest)
                .map_err(|e| ApiError::internal(format!("failed to serialize manifest: {e}")))?;
            let existing = format!(
                "\n\nCURRENT MANIFEST:\n{manifest_json}\n\nCURRENT tool.py:\n{}\n",
                s.tool_code,
            );
            (
                "You are the Tool Manager in REVISION mode. Apply the user's \
                 change to the existing tool below and call save_tool EXACTLY \
                 ONCE with the FULL revised tool_code (not a diff). Keep the \
                 id unchanged.",
                existing,
            )
        }
        None => (FACTORY_CREATE_PROMPT, String::new()),
    };
    Ok(format!(
        "{mode_line}{context}\n\nUSER REQUEST: {user_request}"
    ))
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
    let thinking_mode = body.thinking_mode.unwrap_or_else(|| "high".to_string());
    if !matches!(thinking_mode.as_str(), "off" | "high" | "max") {
        return Err(ApiError::bad_request(
            "thinking_mode must be one of: off, high, max",
        ));
    }
    let max_depth = body.max_depth.unwrap_or_else(|| "standard".to_string());
    if !matches!(max_depth.as_str(), "quick" | "standard" | "deep") {
        return Err(ApiError::bad_request(
            "max_depth must be one of: quick, standard, deep",
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
    let knowledge_context =
        crate::knowledge::context_for_prompt(&state, content, conversation.project_id.as_deref())
            .await?;
    let system_prompt = match (project_prompt, knowledge_context) {
        (Some(project), Some(knowledge)) => Some(format!("{project}\n\n{knowledge}")),
        (Some(project), None) => Some(project),
        (None, Some(knowledge)) => Some(knowledge),
        (None, None) => None,
    };
    let enabled_tools = crate::tools::enabled_tool_ids(&state).await?;

    // Tool Manager: override the system prompt and disable normal tools so the
    // sidecar only exposes save_tool. A revision embeds the current tool.
    let factory_mode = body.factory_mode;
    let system_prompt = if factory_mode {
        Some(factory_system_prompt(content, body.factory_revision.as_deref(), &state).await?)
    } else {
        system_prompt
    };
    let enabled_tools = if factory_mode {
        Vec::new()
    } else {
        enabled_tools
    };

    let created_at = now();
    let user_message = MessageRow {
        id: uuid::Uuid::now_v7().to_string(),
        conversation_id: conversation_id.clone(),
        role: "user".to_string(),
        content: content.to_string(),
        status: "completed".to_string(),
        created_at: created_at.clone(),
        tool_events_json: None,
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
        tls_verify: Some(resolved.tls_verify),
        factory_mode,
        thinking_mode: Some(thinking_mode),
        max_depth: Some(max_depth),
        model: crate::protocol::RunModel {
            base_url: resolved.provider_url,
            model_id: resolved.model_id,
            api_key,
            thinking_off_params: resolved
                .thinking_off_params_json
                .and_then(|json| serde_json::from_str(&json).ok()),
            thinking_high_params: resolved
                .thinking_high_params_json
                .and_then(|json| serde_json::from_str(&json).ok()),
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
        // Accumulated tool-call trace for this run. Persisted on the assistant
        // message when the run terminates, so reloading the conversation shows
        // which tools the agent used (and their inputs/outputs).
        let mut tool_events: Vec<Value> = Vec::new();
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
                "run.tool_call" => {
                    tool_events.push(json!({
                        "kind": "call",
                        "sequence": event.sequence,
                        "call_id": event.payload.get("call_id"),
                        "tool": event.payload.get("tool"),
                        "input": event.payload.get("input"),
                    }));
                }
                "run.tool_result" => {
                    tool_events.push(json!({
                        "kind": "result",
                        "sequence": event.sequence,
                        "call_id": event.payload.get("call_id"),
                        "tool": event.payload.get("tool"),
                        "ok": event.payload.get("ok"),
                        "summary": event.payload.get("summary"),
                        "data": event.payload.get("data"),
                        "error": event.payload.get("error"),
                        "retryable": event.payload.get("retryable"),
                    }));
                }
                "run.completed" | "run.failed" => {
                    persist_terminal_run(
                        &persistence_state,
                        &persistence_run_id,
                        &conversation_id,
                        &assistant_text,
                        &tool_events,
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
    tool_events: &[Value],
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
    // Persist the assistant message when there's text OR a tool trace. We
    // keep the trace even when the run failed mid-tool (e.g. shell_execute
    // errored before the model replied) — it's how the user sees what broke.
    let tool_events_json = if tool_events.is_empty() {
        None
    } else {
        Some(serde_json::to_string(tool_events).unwrap_or_else(|_| "[]".to_string()))
    };
    if !assistant_text.is_empty() || tool_events_json.is_some() {
        let assistant = MessageRow {
            id: uuid::Uuid::now_v7().to_string(),
            conversation_id: conversation_id.to_string(),
            role: "assistant".to_string(),
            content: assistant_text.to_string(),
            status: run.status.clone(),
            created_at: now(),
            tool_events_json: tool_events_json.clone(),
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

/// `POST /api/runs/{id}/synthesize-now`: for a thinking_mode=max run, ask the
/// sidecar to stop investigating and synthesize its partial results. Unlike
/// cancel, the run keeps going through SYNTHESIZE -> COMPLETE.
pub async fn synthesize_now_run(
    State(state): State<SharedState>,
    Path(run_id): Path<String>,
) -> Result<StatusCode, ApiError> {
    let run = state
        .db
        .get_run(&run_id)
        .await?
        .ok_or_else(|| ApiError::not_found(format!("run {run_id} not found")))?;
    if run.status != "running" {
        return Ok(StatusCode::NO_CONTENT);
    }
    relay::run_synthesize_now(&state.supervisor, run_id).await?;
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

        // Phase 1.5: the canned tool_call/tool_result pair the fake_sidecar
        // emits between run.started and run.text_delta is persisted on the
        // assistant message as a JSON tool-events trace.
        let tool_events_json = messages[1]
            .tool_events_json
            .as_ref()
            .expect("assistant message from a run with tool events must persist tool_events_json");
        let events: Vec<Value> = serde_json::from_str(tool_events_json).unwrap();
        assert_eq!(events.len(), 2, "one call + one result: {events:?}");
        assert_eq!(events[0]["kind"], "call");
        assert_eq!(events[0]["call_id"], "fake-call-1");
        assert_eq!(events[0]["tool"], "current_time");
        assert_eq!(events[0]["input"]["timezone"], "UTC");
        assert_eq!(events[1]["kind"], "result");
        assert_eq!(events[1]["call_id"], "fake-call-1");
        assert_eq!(events[1]["ok"], true);
        assert!(
            events[1]["summary"]
                .as_str()
                .is_some_and(|s| s.contains("2026")),
            "result summary should carry the canned timestamp: {events:?}"
        );

        // The user message must NOT carry a tool-events trace.
        assert!(messages[0].tool_events_json.is_none());
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
