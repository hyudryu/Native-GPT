//! Loopback-only OpenAI-compatible proxy for the Page Agent extension
//! (spec §5.4). The extension receives a short-lived task token instead of
//! the real provider key; this proxy validates the token, resolves the
//! selected Native GPT endpoint+model, reads the real key from the OS
//! keychain, and forwards streaming (SSE passthrough) and non-streaming
//! chat-completions traffic. Prompt bodies are never logged.

use std::net::SocketAddr;

use axum::body::{Body, Bytes};
use axum::extract::{ConnectInfo, State};
use axum::http::{header, HeaderMap, HeaderValue, StatusCode};
use axum::response::{IntoResponse, Response};
use tracing::{debug, warn};

use crate::db::ResolvedModel;
use crate::error::ApiError;
use crate::state::SharedState;

use super::manager::TaskTokenGrant;

/// `POST /internal/page-agent/v1/chat/completions`
pub async fn chat_completions(
    State(state): State<SharedState>,
    ConnectInfo(peer): ConnectInfo<SocketAddr>,
    headers: HeaderMap,
    body: Bytes,
) -> Result<Response, ApiError> {
    if !peer.ip().is_loopback() {
        return Err(ApiError::new(
            StatusCode::FORBIDDEN,
            "forbidden",
            "the page-agent model proxy is loopback-only",
        ));
    }
    // When an Origin header is present (browser/extension fetch), it must be
    // our pinned extension. Requests without Origin (sidecar, curl) rely on
    // loopback + token only.
    if let Some(origin) = headers.get(header::ORIGIN).and_then(|v| v.to_str().ok()) {
        let expected = state.browser.expected_extension_origin();
        if origin != expected {
            return Err(ApiError::new(
                StatusCode::FORBIDDEN,
                "forbidden",
                "unexpected origin for the page-agent model proxy",
            ));
        }
    }
    let token = bearer_token(&headers).ok_or_else(|| {
        ApiError::new(
            StatusCode::UNAUTHORIZED,
            "unauthorized",
            "missing bearer task token",
        )
    })?;
    let grant = state.browser.validate_task_token(token).ok_or_else(|| {
        ApiError::new(
            StatusCode::UNAUTHORIZED,
            "unauthorized",
            "invalid or expired browser task token",
        )
    })?;

    let resolved = resolve_model(&state, &grant).await?;
    let api_key = state.secrets.get(&resolved.provider_id);

    let url = format!(
        "{}/chat/completions",
        resolved.provider_url.trim_end_matches('/')
    );
    let streaming = serde_json::from_slice::<serde_json::Value>(&body)
        .ok()
        .and_then(|v| v.get("stream").and_then(|s| s.as_bool()))
        .unwrap_or(false);
    debug!(
        task_id = %grant.task_id,
        provider = %resolved.provider_name,
        model = %resolved.model_id,
        streaming,
        "forwarding page-agent completion (prompt body not logged)"
    );

    let client = reqwest::Client::builder()
        .danger_accept_invalid_certs(!resolved.tls_verify)
        .build()
        .map_err(|e| ApiError::internal(e.to_string()))?;
    let mut request = client
        .post(&url)
        .header(header::CONTENT_TYPE, "application/json")
        .body(body);
    if let Some(key) = api_key {
        request = request.bearer_auth(key);
    }
    let upstream = request.send().await.map_err(|e| {
        warn!(error = %e, "page-agent upstream request failed");
        ApiError::bad_gateway("upstream_error", format!("provider request failed: {e}"))
    })?;

    let status =
        StatusCode::from_u16(upstream.status().as_u16()).unwrap_or(StatusCode::BAD_GATEWAY);
    let content_type = upstream
        .headers()
        .get(header::CONTENT_TYPE)
        .and_then(|v| v.to_str().ok())
        .unwrap_or("application/json")
        .to_string();

    if streaming || content_type.contains("text/event-stream") {
        // SSE passthrough: stream upstream bytes straight to the extension.
        let stream = upstream.bytes_stream();
        let mut response = Response::new(Body::from_stream(stream));
        *response.status_mut() = status;
        response.headers_mut().insert(
            header::CONTENT_TYPE,
            HeaderValue::from_str(&content_type)
                .unwrap_or(HeaderValue::from_static("text/event-stream")),
        );
        Ok(response)
    } else {
        let bytes = upstream.bytes().await.map_err(|e| {
            ApiError::bad_gateway("upstream_error", format!("provider response failed: {e}"))
        })?;
        let mut response = Response::new(Body::from(bytes));
        *response.status_mut() = status;
        response.headers_mut().insert(
            header::CONTENT_TYPE,
            HeaderValue::from_str(&content_type)
                .unwrap_or(HeaderValue::from_static("application/json")),
        );
        Ok(response)
    }
}

fn bearer_token(headers: &HeaderMap) -> Option<&str> {
    headers
        .get(header::AUTHORIZATION)
        .and_then(|v| v.to_str().ok())
        .and_then(|h| h.strip_prefix("Bearer "))
        .filter(|t| !t.is_empty())
}

/// Resolve the endpoint+model the task token was minted for: fixed selection
/// from `browser_preferences`, or the initiating conversation's model
/// (follow_conversation default, spec §5.3).
async fn resolve_model(
    state: &SharedState,
    grant: &TaskTokenGrant,
) -> Result<ResolvedModel, ApiError> {
    if let (Some(endpoint_id), Some(model_id)) = (&grant.endpoint_id, &grant.model_id) {
        let endpoint = state
            .db
            .get_endpoint(endpoint_id)
            .await?
            .ok_or_else(|| ApiError::bad_request("fixed browser model endpoint not found"))?;
        return Ok(ResolvedModel {
            provider_id: endpoint.id,
            provider_name: endpoint.name,
            provider_url: endpoint.base_url,
            model_id: model_id.clone(),
            tls_verify: endpoint.tls_verify,
        });
    }
    let conversation_id = grant.conversation_id.as_deref().ok_or_else(|| {
        ApiError::bad_request("task token has neither a fixed model nor a conversation to follow")
    })?;
    state
        .db
        .resolve_conversation_model(conversation_id)
        .await
        .map_err(|e| ApiError::bad_request(format!("browser model resolution failed: {e}")))
}

/// Convenience mapper for tests and non-axum callers.
pub fn unauthorized_response() -> Response {
    ApiError::new(StatusCode::UNAUTHORIZED, "unauthorized", "unauthorized").into_response()
}

#[cfg(test)]
mod tests {
    use super::*;
    use axum::http::HeaderValue;

    #[test]
    fn extracts_bearer_tokens() {
        let mut headers = HeaderMap::new();
        assert_eq!(bearer_token(&headers), None);
        headers.insert(
            header::AUTHORIZATION,
            HeaderValue::from_static("Bearer abc"),
        );
        assert_eq!(bearer_token(&headers), Some("abc"));
        headers.insert(header::AUTHORIZATION, HeaderValue::from_static("Basic xyz"));
        assert_eq!(bearer_token(&headers), None);
        headers.insert(header::AUTHORIZATION, HeaderValue::from_static("Bearer "));
        assert_eq!(bearer_token(&headers), None);
    }
}
