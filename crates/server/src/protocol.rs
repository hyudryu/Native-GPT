//! Protocol v1.0 types: envelope (from `agentgpt-supervisor`) plus the
//! hand-written Phase 0 payload structs mirroring
//! `packages/protocol-types/schemas/messages.json` (ADR-0007).

use serde::{Deserialize, Serialize};

pub use agentgpt_supervisor::protocol::{Envelope, ProtocolError, PROTOCOL_VERSION};

/// `runtime.hello` payload.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct RuntimeHello {
    pub client: String,
    pub client_version: String,
}

/// `runtime.hello.ok` payload.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct RuntimeHelloOk {
    pub runtime: String,
    pub runtime_version: String,
    pub protocol: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub capabilities: Option<Vec<String>>,
}

/// `runtime.health` payload (empty object).
#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq)]
pub struct RuntimeHealth {}

/// `runtime.health.ok` status.
#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "lowercase")]
pub enum HealthStatus {
    Ok,
    Degraded,
}

/// `runtime.health.ok` payload.
#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq)]
pub struct RuntimeHealthOk {
    pub status: HealthStatus,
    pub uptime_seconds: f64,
    pub rss_bytes: u64,
}

/// `runtime.shutdown` payload (empty object).
#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq)]
pub struct RuntimeShutdown {}

/// `endpoint.test` payload. `api_key` is the RAW key resolved from the
/// keychain; it must never be logged or returned by the REST API.
#[derive(Debug, Clone, Serialize, PartialEq)]
pub struct EndpointTest {
    pub base_url: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub api_key: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub timeout_seconds: Option<u32>,
}

/// `endpoint.test.ok` payload.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct EndpointTestOk {
    pub ok: bool,
    #[serde(default)]
    pub latency_ms: Option<f64>,
    #[serde(default)]
    pub server: Option<String>,
    #[serde(default)]
    pub error: Option<ProtocolError>,
}

/// `models.list` payload.
#[derive(Debug, Clone, Serialize, PartialEq)]
pub struct ModelsList {
    pub base_url: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub api_key: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub model_list_path: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub timeout_seconds: Option<u32>,
}

/// One entry of the `models.list.ok` models array.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct ModelInfo {
    pub id: String,
    #[serde(default)]
    pub raw: Option<serde_json::Value>,
}

/// `models.list.ok` payload.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct ModelsListOk {
    pub models: Vec<ModelInfo>,
    #[serde(default)]
    pub fetched_at: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct ChatMessage {
    pub role: String,
    pub content: String,
}

#[derive(Debug, Clone, Serialize, PartialEq)]
pub struct RunModel {
    pub base_url: String,
    pub model_id: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub api_key: Option<String>,
}

#[derive(Debug, Clone, Serialize, PartialEq)]
pub struct RunStart {
    pub run_id: String,
    pub conversation_id: String,
    pub message_id: String,
    pub prompt: String,
    pub history: Vec<ChatMessage>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub system_prompt: Option<String>,
    pub model: RunModel,
}

#[derive(Debug, Clone, Deserialize, PartialEq)]
pub struct RunStarted {
    pub run_id: String,
    pub conversation_id: String,
}

#[derive(Debug, Clone, Serialize, PartialEq)]
pub struct RunCancel {
    pub run_id: String,
}

#[derive(Debug, Clone, Deserialize, PartialEq)]
pub struct RunCancelled {
    pub run_id: String,
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn hello_round_trip() {
        let hello = RuntimeHello {
            client: "agentgpt-host".into(),
            client_version: env!("CARGO_PKG_VERSION").into(),
        };
        let value = serde_json::to_value(&hello).expect("serialize");
        assert_eq!(
            value,
            json!({"client": "agentgpt-host", "client_version": env!("CARGO_PKG_VERSION")})
        );
        let back: RuntimeHello = serde_json::from_value(value).expect("deserialize");
        assert_eq!(hello, back);
    }

    #[test]
    fn empty_payloads_serialize_as_empty_objects() {
        assert_eq!(serde_json::to_value(RuntimeHealth {}).unwrap(), json!({}));
        assert_eq!(serde_json::to_value(RuntimeShutdown {}).unwrap(), json!({}));
    }

    #[test]
    fn health_ok_round_trip() {
        let ok = RuntimeHealthOk {
            status: HealthStatus::Degraded,
            uptime_seconds: 12.5,
            rss_bytes: 42_000,
        };
        let value = serde_json::to_value(ok).expect("serialize");
        assert_eq!(
            value,
            json!({"status": "degraded", "uptime_seconds": 12.5, "rss_bytes": 42000})
        );
        let back: RuntimeHealthOk = serde_json::from_value(value).expect("deserialize");
        assert_eq!(ok, back);
    }

    #[test]
    fn endpoint_payloads_round_trip_and_hide_api_key_when_absent() {
        let test = EndpointTest {
            base_url: "http://127.0.0.1:1234".into(),
            api_key: None,
            timeout_seconds: Some(15),
        };
        let value = serde_json::to_value(&test).unwrap();
        assert_eq!(
            value,
            json!({"base_url": "http://127.0.0.1:1234", "timeout_seconds": 15})
        );
        assert!(!value.to_string().contains("api_key"));

        let ok: EndpointTestOk = serde_json::from_value(json!({
            "ok": false,
            "error": {"code": "connection_error", "message": "refused"}
        }))
        .unwrap();
        assert!(!ok.ok);
        assert_eq!(ok.error.unwrap().code, "connection_error");

        let list: ModelsListOk = serde_json::from_value(json!({
            "models": [{"id": "qwen3:8b", "raw": {"owned_by": "local"}}, {"id": "gpt-4"}],
            "fetched_at": "2026-07-20T00:00:00Z"
        }))
        .unwrap();
        assert_eq!(list.models.len(), 2);
        assert_eq!(list.models[0].id, "qwen3:8b");
    }

    #[test]
    fn hello_ok_envelope_round_trip() {
        let payload = RuntimeHelloOk {
            runtime: "agentgpt_runtime".into(),
            runtime_version: "0.1.0".into(),
            protocol: PROTOCOL_VERSION.into(),
            capabilities: Some(vec!["runtime.health".into()]),
        };
        let env = Envelope::new("runtime.hello.ok", serde_json::to_value(payload).unwrap());
        let text = serde_json::to_string(&env).expect("serialize");
        let back: Envelope = serde_json::from_str(&text).expect("deserialize");
        assert_eq!(env, back);
        let parsed: RuntimeHelloOk = serde_json::from_value(back.payload).expect("payload parses");
        assert_eq!(parsed.protocol, "1.0");
    }
}
