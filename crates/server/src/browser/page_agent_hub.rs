//! Page Agent Hub bridge, server side (spec §5.1): the extension's Hub tab
//! connects to `/internal/browser/hub?token=…` (loopback only, token-gated).
//! The host sends `execute`/`stop`, receives `ready`/`result`/`error` and
//! optional `activity` progress messages.

use serde::{Deserialize, Serialize};
use serde_json::Value;

/// Messages the host sends to the Hub.
#[derive(Debug, Clone, Serialize, PartialEq)]
#[serde(tag = "type", rename_all = "camelCase")]
pub enum HubOutbound {
    #[serde(rename = "execute")]
    Execute { task: String, config: Value },
    #[serde(rename = "stop")]
    Stop,
}

/// Messages the Hub sends to the host.
#[derive(Debug, Clone, Deserialize, PartialEq)]
#[serde(tag = "type", rename_all = "camelCase")]
pub enum HubInbound {
    #[serde(rename = "ready")]
    Ready,
    #[serde(rename = "result")]
    Result {
        success: bool,
        #[serde(default)]
        data: Value,
    },
    #[serde(rename = "error")]
    Error { message: String },
    /// Optional progress from the pinned extension patch (spec §5.5). The
    /// feature remains fully functional without it.
    #[serde(rename = "activity")]
    Activity {
        #[serde(default)]
        task_id: Option<String>,
        #[serde(default)]
        message: String,
        #[serde(default)]
        url: Option<String>,
    },
}

impl HubInbound {
    pub fn parse(text: &str) -> Result<Self, serde_json::Error> {
        serde_json::from_str(text)
    }
}

/// Build the Hub tab URL opened inside the dedicated Chromium (spec §5.1).
pub fn hub_tab_url(extension_id: &str, port: u16, token: &str) -> String {
    format!(
        "chrome-extension://{extension_id}/hub.html?ws=127.0.0.1:{port}/internal/browser/hub?token={token}"
    )
}

/// The extension origin expected on Hub WebSocket handshakes.
pub fn expected_extension_origin(extension_id: &str) -> String {
    format!("chrome-extension://{extension_id}")
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn parses_ready_result_error_and_activity() {
        assert_eq!(
            HubInbound::parse(r#"{"type":"ready"}"#).unwrap(),
            HubInbound::Ready
        );

        let result =
            HubInbound::parse(r#"{"type":"result","success":true,"data":"done"}"#).unwrap();
        assert_eq!(
            result,
            HubInbound::Result {
                success: true,
                data: json!("done")
            }
        );

        let error = HubInbound::parse(r#"{"type":"error","message":"boom"}"#).unwrap();
        assert_eq!(
            error,
            HubInbound::Error {
                message: "boom".into()
            }
        );

        let activity = HubInbound::parse(
            r#"{"type":"activity","task_id":"t-1","message":"Clicking Continue","url":"https://example.com"}"#,
        )
        .unwrap();
        match activity {
            HubInbound::Activity {
                task_id,
                message,
                url,
            } => {
                assert_eq!(task_id.as_deref(), Some("t-1"));
                assert_eq!(message, "Clicking Continue");
                assert_eq!(url.as_deref(), Some("https://example.com"));
            }
            other => panic!("expected activity, got {other:?}"),
        }

        assert!(HubInbound::parse(r#"{"type":"mystery"}"#).is_err());
        assert!(HubInbound::parse("not json").is_err());
    }

    #[test]
    fn outbound_messages_match_hub_protocol() {
        let execute = serde_json::to_value(HubOutbound::Execute {
            task: "fill the form".into(),
            config: json!({"model": "m", "baseURL": "http://127.0.0.1:1/internal/page-agent/v1"}),
        })
        .unwrap();
        assert_eq!(execute["type"], "execute");
        assert_eq!(execute["task"], "fill the form");
        assert_eq!(
            serde_json::to_value(HubOutbound::Stop).unwrap(),
            json!({"type": "stop"})
        );
    }

    #[test]
    fn hub_url_carries_ws_endpoint_and_token() {
        let url = hub_tab_url("extid123", 9123, "secret-token");
        assert_eq!(
            url,
            "chrome-extension://extid123/hub.html?ws=127.0.0.1:9123/internal/browser/hub?token=secret-token"
        );
        assert_eq!(
            expected_extension_origin("extid123"),
            "chrome-extension://extid123"
        );
    }
}
