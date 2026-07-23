//! HTTP client for remote backend hosts (the "bridge").
//!
//! The desktop server acts as an authenticated client of one or more remote
//! bridge services (ADR-0008). Generation/TTS job submission moved to MCP
//! (the agent-runtime connects to the bridge's `/mcp` endpoint directly; see
//! `docs/superpowers/specs/2026-07-22-bridge-mcp-server-design.md`). What
//! remains here is the REST surface the desktop still proxies: health probes
//! ("test connection"), asset byte fetching (same-origin asset proxy), and
//! voice management. Per-host bearer tokens are resolved from the keychain by
//! the caller and passed in — they are never stored or logged here.

use std::time::Duration;

use reqwest::{Client, ClientBuilder, StatusCode};
use serde::{Deserialize, Serialize};
use serde_json::Value;

use crate::error::ApiError;

/// Default request timeout for bridge calls (generation may exceed this; jobs
/// use their own timeout).
const DEFAULT_TIMEOUT: Duration = Duration::from_secs(15);

/// A single configured remote bridge connection.
#[derive(Clone)]
pub struct BridgeClient {
    base_url: String,
    token: Option<String>,
    client: Client,
}

/// Deserialized `/health` response: capability snapshot of the bridge.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct BridgeHealth {
    pub version: String,
    #[serde(default)]
    pub workloads: serde_json::Map<String, Value>,
}

/// Result of a `/health` probe: either a healthy snapshot or a failure reason.
#[derive(Debug, Clone)]
pub enum HealthProbe {
    Reachable(BridgeHealth),
    Unreachable(String),
}

impl BridgeClient {
    /// Build a client for a host. `token` is the raw bearer token from the
    /// keychain; it is sent only as an Authorization header, never logged.
    pub fn new(base_url: &str, token: Option<String>, tls_verify: bool) -> Result<Self, ApiError> {
        let mut builder = ClientBuilder::new().timeout(DEFAULT_TIMEOUT);
        if !tls_verify {
            builder = builder
                .danger_accept_invalid_certs(true)
                .danger_accept_invalid_hostnames(true);
        }
        let client = builder
            .build()
            .map_err(|e| ApiError::internal(format!("failed to build bridge client: {e}")))?;
        Ok(Self {
            base_url: base_url.trim_end_matches('/').to_string(),
            token,
            client,
        })
    }

    fn url(&self, path: &str) -> String {
        format!("{}{}", self.base_url, path)
    }

    async fn get_json<T: serde::de::DeserializeOwned>(
        &self,
        path: &str,
        timeout: Duration,
    ) -> Result<T, BridgeError> {
        let mut req = self.client.get(self.url(path)).timeout(timeout);
        if let Some(token) = &self.token {
            req = req.bearer_auth(token);
        }
        let resp = req.send().await.map_err(BridgeError::transport)?;
        let status = resp.status();
        let body = resp.bytes().await.map_err(BridgeError::transport)?;
        if status.is_success() {
            serde_json::from_slice(&body).map_err(BridgeError::decode)
        } else {
            Err(BridgeError::status(status, &body))
        }
    }

    async fn post_multipart<T: serde::de::DeserializeOwned>(
        &self,
        path: &str,
        form: reqwest::multipart::Form,
        timeout: Duration,
    ) -> Result<T, BridgeError> {
        let mut req = self
            .client
            .post(self.url(path))
            .timeout(timeout)
            .multipart(form);
        if let Some(token) = &self.token {
            req = req.bearer_auth(token);
        }
        let resp = req.send().await.map_err(BridgeError::transport)?;
        let status = resp.status();
        let body = resp.bytes().await.map_err(BridgeError::transport)?;
        if status.is_success() {
            serde_json::from_slice(&body).map_err(BridgeError::decode)
        } else {
            Err(BridgeError::status(status, &body))
        }
    }

    /// Probe `/health`. Returns a `HealthProbe` rather than erroring on
    /// unreachable hosts, so callers can record status without try/catch.
    pub async fn probe_health(&self) -> HealthProbe {
        match self
            .get_json::<BridgeHealth>("/health", DEFAULT_TIMEOUT)
            .await
        {
            Ok(h) => HealthProbe::Reachable(h),
            Err(e) => HealthProbe::Unreachable(e.to_string()),
        }
    }

    /// Fetch raw bytes for a generated asset by its short-lived asset token.
    pub async fn fetch_asset_bytes(
        &self,
        asset_token: &str,
        mime_type: Option<&str>,
    ) -> Result<(Vec<u8>, Option<String>), BridgeError> {
        let mut req = self
            .client
            .get(self.url(&format!("/assets/{asset_token}")))
            .timeout(Duration::from_secs(30));
        if let Some(token) = &self.token {
            req = req.bearer_auth(token);
        }
        let resp = req.send().await.map_err(BridgeError::transport)?;
        let status = resp.status();
        if !status.is_success() {
            let body = resp.bytes().await.unwrap_or_default();
            return Err(BridgeError::status(status, &body));
        }
        // Prefer the server's actual Content-Type header; fall back to the
        // caller-provided hint only if the header is missing.
        let detected_mime = resp
            .headers()
            .get("content-type")
            .map(|v| v.to_str().unwrap_or("").to_string())
            .filter(|s| !s.is_empty())
            .or_else(|| mime_type.map(|m| m.to_string()));
        let body = resp.bytes().await.map_err(BridgeError::transport)?;
        Ok((body.to_vec(), detected_mime))
    }

    /// Upload a voice reference clip (multipart) to the OpenVoice workload.
    pub async fn upload_voice(
        &self,
        name: &str,
        clip_bytes: Vec<u8>,
        filename: &str,
        mime_type: &str,
    ) -> Result<Value, ApiError> {
        let part = reqwest::multipart::Part::bytes(clip_bytes)
            .file_name(filename.to_string())
            .mime_str(mime_type)
            .map_err(|e| ApiError::internal(format!("invalid mime type: {e}")))?;
        let form = reqwest::multipart::Form::new()
            .text("name", name.to_string())
            .part("clip", part);
        self.post_multipart::<Value>("/workloads/openvoice/voices", form, Duration::from_secs(60))
            .await
            .map_err(|e| e.into_api())
    }

    /// Delete a voice on the bridge.
    pub async fn delete_voice(&self, voice_id: &str) -> Result<(), ApiError> {
        let mut req = self
            .client
            .delete(self.url(&format!("/workloads/openvoice/voices/{voice_id}")))
            .timeout(DEFAULT_TIMEOUT);
        if let Some(token) = &self.token {
            req = req.bearer_auth(token);
        }
        let resp = req
            .send()
            .await
            .map_err(|e| BridgeError::transport(e).into_api())?;
        let status = resp.status();
        if !status.is_success() {
            let body = resp.bytes().await.unwrap_or_default();
            return Err(BridgeError::status(status, &body).into_api());
        }
        Ok(())
    }
}

/// Error from a bridge call. Carries enough to map to a REST `ApiError`.
#[derive(Debug)]
pub struct BridgeError {
    pub kind: BridgeErrorKind,
    pub message: String,
    pub status: Option<StatusCode>,
}

#[derive(Debug)]
pub enum BridgeErrorKind {
    /// Could not connect / timed out / DNS / TLS.
    Transport,
    /// Response body could not be parsed as the expected type.
    Decode,
    /// Bridge returned a non-success status.
    Status,
}

impl BridgeError {
    fn transport(e: reqwest::Error) -> Self {
        Self {
            kind: BridgeErrorKind::Transport,
            message: e.to_string(),
            status: None,
        }
    }

    fn decode(e: serde_json::Error) -> Self {
        Self {
            kind: BridgeErrorKind::Decode,
            message: e.to_string(),
            status: None,
        }
    }

    fn status(status: StatusCode, body: &[u8]) -> Self {
        let message = serde_json::from_slice::<Value>(body)
            .ok()
            .and_then(|v| v.get("error").and_then(|e| e.get("message")).cloned())
            .and_then(|m| m.as_str().map(String::from))
            .unwrap_or_else(|| format!("bridge returned {}", status.as_u16()));
        Self {
            kind: BridgeErrorKind::Status,
            message,
            status: Some(status),
        }
    }

    /// Map to a REST-facing `ApiError`.
    pub fn into_api(self) -> ApiError {
        match self.kind {
            BridgeErrorKind::Transport => ApiError::bad_gateway("bridge_unreachable", self.message),
            BridgeErrorKind::Decode => ApiError::bad_gateway("bad_bridge_response", self.message),
            BridgeErrorKind::Status => match self.status {
                Some(s) if s == StatusCode::BAD_REQUEST => ApiError::bad_request(self.message),
                Some(s) if s == StatusCode::NOT_FOUND => ApiError::not_found(self.message),
                Some(s) if s == StatusCode::UNAUTHORIZED || s == StatusCode::FORBIDDEN => {
                    ApiError::new(s, "bridge_auth_error", self.message)
                }
                Some(s) if s == StatusCode::SERVICE_UNAVAILABLE => {
                    ApiError::sidecar_unavailable("workload_unavailable", self.message)
                }
                Some(s) => ApiError::new(
                    s,
                    "bridge_error",
                    format!("bridge returned {}: {}", s.as_u16(), self.message),
                ),
                None => ApiError::bad_gateway("bridge_error", self.message),
            },
        }
    }
}

impl std::fmt::Display for BridgeError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self.status {
            Some(s) => write!(
                f,
                "bridge {}: {} (status {})",
                self.kind_msg(),
                self.message,
                s
            ),
            None => write!(f, "bridge {}: {}", self.kind_msg(), self.message),
        }
    }
}

impl BridgeError {
    fn kind_msg(&self) -> &'static str {
        match self.kind {
            BridgeErrorKind::Transport => "transport error",
            BridgeErrorKind::Decode => "decode error",
            BridgeErrorKind::Status => "error",
        }
    }
}
