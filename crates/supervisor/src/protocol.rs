//! Protocol v1.0 envelope types shared by the host and the Python sidecar.
//!
//! Mirrors `packages/protocol-types/schemas/envelope.json`. Hand-written per
//! ADR-0007; `agentgpt-server::protocol` re-exports these and adds the typed
//! Phase 0 payload structs.

use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize};

/// Protocol version. Mismatched major versions are rejected at handshake.
pub const PROTOCOL_VERSION: &str = "1.0";

/// Base envelope for every message (host<->sidecar NDJSON, UI<->host WS).
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct Envelope {
    pub protocol: String,
    #[serde(rename = "type")]
    pub kind: String,
    pub request_id: String,
    /// Monotonic per `request_id`; present on streaming events only.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub sequence: Option<u64>,
    pub timestamp: DateTime<Utc>,
    pub payload: serde_json::Value,
}

/// Payload of an `error` message.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct ProtocolError {
    pub code: String,
    pub message: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub retryable: Option<bool>,
}

impl Envelope {
    /// Create a request/notification envelope with a fresh UUIDv7 request id.
    pub fn new(kind: impl Into<String>, payload: serde_json::Value) -> Self {
        Self {
            protocol: PROTOCOL_VERSION.to_string(),
            kind: kind.into(),
            request_id: uuid::Uuid::now_v7().to_string(),
            sequence: None,
            timestamp: Utc::now(),
            payload,
        }
    }

    /// Create an `error` envelope responding to `request_id`.
    pub fn error(
        request_id: impl Into<String>,
        code: impl Into<String>,
        message: impl Into<String>,
        retryable: bool,
    ) -> Self {
        let err = ProtocolError {
            code: code.into(),
            message: message.into(),
            retryable: Some(retryable),
        };
        let mut env = Self::new(
            "error",
            serde_json::to_value(err).unwrap_or_else(|_| serde_json::json!({})),
        );
        env.request_id = request_id.into();
        env
    }

    /// Error code if this is an `error` envelope.
    pub fn error_code(&self) -> Option<&str> {
        if self.kind != "error" {
            return None;
        }
        self.payload.get("code")?.as_str()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn envelope_round_trip_matches_schema_shape() {
        let raw = r#"{"protocol":"1.0","type":"runtime.health","request_id":"0190abcd-1234-7abc-8000-0123456789ab","timestamp":"2026-07-20T05:00:00Z","payload":{}}"#;
        let env: Envelope = serde_json::from_str(raw).expect("parse");
        assert_eq!(env.protocol, PROTOCOL_VERSION);
        assert_eq!(env.kind, "runtime.health");
        assert_eq!(env.sequence, None);
        let out = serde_json::to_string(&env).expect("serialize");
        let back: Envelope = serde_json::from_str(&out).expect("re-parse");
        assert_eq!(env, back);
        // No `sequence` key when absent (schema: additionalProperties=false).
        assert!(!out.contains("sequence"));
    }

    #[test]
    fn error_envelope_carries_request_id_and_code() {
        let env = Envelope::error("req-1", "sidecar_crashed", "process exited", true);
        assert_eq!(env.kind, "error");
        assert_eq!(env.request_id, "req-1");
        assert_eq!(env.error_code(), Some("sidecar_crashed"));
        let parsed: ProtocolError =
            serde_json::from_value(env.payload.clone()).expect("payload parses as ProtocolError");
        assert_eq!(parsed.code, "sidecar_crashed");
        assert_eq!(parsed.retryable, Some(true));
    }
}
