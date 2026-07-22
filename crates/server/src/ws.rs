//! `/ws` WebSocket endpoint: relays client request envelopes to the sidecar
//! and broadcasts sidecar events to every connected client.

use axum::extract::ws::{Message, WebSocket, WebSocketUpgrade};
use axum::extract::State;
use axum::response::Response;
use futures_util::{SinkExt, StreamExt};
use tokio::sync::broadcast;
use tracing::{debug, warn};

use crate::protocol::{Envelope, PROTOCOL_VERSION};
use crate::state::SharedState;

pub async fn ws_handler(ws: WebSocketUpgrade, State(state): State<SharedState>) -> Response {
    ws.on_upgrade(move |socket| handle_socket(socket, state))
}

async fn handle_socket(socket: WebSocket, state: SharedState) {
    let (mut tx, mut rx) = socket.split();
    // Two event sources: sidecar-originated envelopes (run deltas etc.) and
    // host-originated ones (`data.changed` for multi-client sync, M3).
    let mut sidecar_events = state.supervisor.events().subscribe();
    let mut host_events = state.host_events.subscribe();
    debug!("ws client connected");
    loop {
        tokio::select! {
            msg = rx.next() => {
                match msg {
                    Some(Ok(Message::Text(text))) => {
                        let response = handle_client_message(&state, &text).await;
                        match serde_json::to_string(&response) {
                            Ok(json) => {
                                if tx.send(Message::Text(json.into())).await.is_err() {
                                    break;
                                }
                            }
                            Err(e) => warn!("failed to encode ws response: {e}"),
                        }
                    }
                    Some(Ok(Message::Close(_))) | None => break,
                    Some(Ok(_)) => {} // pings/pongs/binary: ignore
                    Some(Err(e)) => {
                        debug!("ws receive error: {e}");
                        break;
                    }
                }
            }
            event = sidecar_events.recv() => {
                if !forward_event(&mut tx, event, "sidecar").await {
                    break;
                }
            }
            event = host_events.recv() => {
                if !forward_event(&mut tx, event, "host").await {
                    break;
                }
            }
        }
    }
    debug!("ws client disconnected");
}

/// Forward one broadcast envelope to the WS client. Returns false when the
/// socket or channel is done and the handler loop should break.
async fn forward_event(
    tx: &mut futures_util::stream::SplitSink<WebSocket, Message>,
    event: Result<Envelope, broadcast::error::RecvError>,
    source: &str,
) -> bool {
    match event {
        Ok(env) => match serde_json::to_string(&env) {
            Ok(json) => tx.send(Message::Text(json.into())).await.is_ok(),
            Err(e) => {
                warn!("failed to encode {source} event: {e}");
                true
            }
        },
        Err(broadcast::error::RecvError::Lagged(skipped)) => {
            warn!(skipped, source, "ws client lagged behind broadcast events");
            true
        }
        Err(broadcast::error::RecvError::Closed) => false,
    }
}

/// Forward one client envelope to the sidecar and produce the reply.
async fn handle_client_message(state: &SharedState, text: &str) -> Envelope {
    let env: Envelope = match serde_json::from_str(text) {
        Ok(env) => env,
        Err(_) => {
            return Envelope::error("unknown", "bad_request", "invalid envelope JSON", false);
        }
    };
    if env.protocol != PROTOCOL_VERSION {
        return Envelope::error(
            env.request_id,
            "protocol_mismatch",
            format!("expected protocol {PROTOCOL_VERSION}"),
            false,
        );
    }
    let request_id = env.request_id.clone();
    match state.supervisor.request(env).await {
        Ok(resp) => resp,
        Err(e) => Envelope::error(request_id, e.code(), e.to_string(), true),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;
    use std::net::SocketAddr;
    use std::time::Duration;

    /// Receive WS text frames until one parses into an envelope whose `kind`
    /// matches `wanted`, re-sending `emit` between attempts (broadcast sends
    /// before the subscription lands are lost).
    async fn recv_kind<E>(
        ws: &mut tokio_tungstenite::WebSocketStream<
            tokio_tungstenite::MaybeTlsStream<tokio::net::TcpStream>,
        >,
        wanted: &str,
        mut emit: E,
    ) -> Envelope
    where
        E: FnMut(),
    {
        let deadline = std::time::Instant::now() + Duration::from_secs(10);
        loop {
            assert!(std::time::Instant::now() < deadline, "no {wanted} frame");
            emit();
            let frame = tokio::time::timeout(Duration::from_millis(500), ws.next()).await;
            let Ok(Some(Ok(tokio_tungstenite::tungstenite::Message::Text(text)))) = frame else {
                continue;
            };
            let Ok(env) = serde_json::from_str::<Envelope>(&text) else {
                continue;
            };
            if env.kind == wanted {
                return env;
            }
        }
    }

    #[tokio::test]
    async fn ws_forwards_host_and_sidecar_events() {
        let rig = crate::state::test_state("tok");
        let state = rig.state.clone();
        let app = crate::build_router(state.clone());
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

        let (mut ws, _) = tokio_tungstenite::connect_async(format!("ws://{addr}/ws"))
            .await
            .expect("ws connect");

        // Host-originated data.changed envelope.
        let host_env = recv_kind(&mut ws, "data.changed", || {
            crate::events::data_changed(
                &state,
                json!({"entity": "conversation", "id": "c-1", "conversation_id": "c-1"}),
            );
        })
        .await;
        assert_eq!(host_env.protocol, PROTOCOL_VERSION);
        assert_eq!(host_env.payload["entity"], json!("conversation"));
        assert_eq!(host_env.payload["conversation_id"], json!("c-1"));

        // Sidecar-originated event envelope (injected via the supervisor's
        // broadcast channel, no process needed).
        let sidecar_env = recv_kind(&mut ws, "run.text_delta", || {
            let _ = state
                .supervisor
                .events()
                .send(Envelope::new("run.text_delta", json!({"text": "x"})));
        })
        .await;
        assert_eq!(sidecar_env.payload["text"], json!("x"));

        ws.close(None).await.ok();
    }

    #[test]
    fn host_event_payloads_stay_small() {
        // Guard against regressions that would leak message content into
        // data.changed payloads (see chat.rs / phase3.rs call sites).
        let payload = json!({"entity": "message", "conversation_id": "c", "run_id": "r", "status": "completed"});
        let mut keys: Vec<&str> = payload
            .as_object()
            .unwrap()
            .keys()
            .map(String::as_str)
            .collect();
        keys.sort_unstable();
        assert_eq!(keys, ["conversation_id", "entity", "run_id", "status"]);
        assert!(!payload.to_string().contains("content"));
    }
}
