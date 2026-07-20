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
    let mut events = state.supervisor.events().subscribe();
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
            event = events.recv() => {
                match event {
                    Ok(env) => match serde_json::to_string(&env) {
                        Ok(json) => {
                            if tx.send(Message::Text(json.into())).await.is_err() {
                                break;
                            }
                        }
                        Err(e) => warn!("failed to encode broadcast event: {e}"),
                    },
                    Err(broadcast::error::RecvError::Lagged(skipped)) => {
                        warn!(skipped, "ws client lagged behind sidecar events");
                    }
                    Err(broadcast::error::RecvError::Closed) => break,
                }
            }
        }
    }
    debug!("ws client disconnected");
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
