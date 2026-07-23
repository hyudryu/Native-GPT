//! Minimal Chrome DevTools Protocol client over `tokio-tungstenite`
//! (spec §10): multiplexed command ids with oneshot responders, flattened
//! per-target sessions, and a broadcast event stream. Disconnects drain all
//! pending commands with [`CdpError::Disconnected`] so the manager can drive
//! the crash flow (spec §14.4).

use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::Arc;
use std::time::Duration;

use dashmap::DashMap;
use futures_util::{SinkExt, StreamExt};
use serde_json::{json, Value};
use tokio::sync::{broadcast, mpsc, oneshot};
use tracing::debug;

use super::protocol::error_codes;

/// Default per-command timeout.
const COMMAND_TIMEOUT: Duration = Duration::from_secs(15);
/// Capacity of the CDP event broadcast; lagging consumers drop events
/// (frames must never build an unbounded backlog, spec §14.3).
const EVENT_CAPACITY: usize = 256;

#[derive(Debug, thiserror::Error)]
pub enum CdpError {
    #[error("CDP protocol error {code}: {message}")]
    Protocol { code: i64, message: String },
    #[error("CDP connection closed")]
    Disconnected,
    #[error("CDP command timed out")]
    Timeout,
    #[error("websocket error: {0}")]
    WebSocket(String),
    #[error("unexpected CDP message shape")]
    BadMessage,
}

impl CdpError {
    /// Stable tool-facing code (spec §7.3).
    pub fn code(&self) -> &'static str {
        match self {
            Self::Disconnected | Self::WebSocket(_) => error_codes::CDP_DISCONNECTED,
            Self::Timeout => error_codes::TASK_TIMEOUT,
            _ => error_codes::CDP_DISCONNECTED,
        }
    }
}

/// A CDP event (method + params), tagged with its session when it came from
/// an attached target in flatten mode.
#[derive(Debug, Clone)]
pub struct CdpEvent {
    pub session_id: Option<String>,
    pub method: String,
    pub params: Value,
}

type PendingMap = DashMap<u64, oneshot::Sender<Result<Value, CdpError>>>;

struct Outgoing {
    id: u64,
    method: String,
    params: Value,
    session_id: Option<String>,
}

struct Inner {
    next_id: AtomicU64,
    pending: PendingMap,
    outgoing: mpsc::UnboundedSender<Outgoing>,
    events: broadcast::Sender<CdpEvent>,
    closed: AtomicBool,
}

/// Handle to a live CDP connection. Cheap to clone; all clones share the
/// connection. The connection closes when the internal task ends (peer
/// disconnect or all handles dropped).
pub struct CdpClient {
    inner: Arc<Inner>,
}

impl CdpClient {
    /// Connect to a browser-level CDP WebSocket (`ws://…/devtools/browser/…`).
    pub async fn connect(ws_url: &str) -> Result<Self, CdpError> {
        let (socket, _response) = tokio_tungstenite::connect_async(ws_url)
            .await
            .map_err(|e| CdpError::WebSocket(e.to_string()))?;
        let (mut write, mut read) = socket.split();
        let (outgoing_tx, mut outgoing_rx) = mpsc::unbounded_channel::<Outgoing>();
        let (events_tx, _) = broadcast::channel(EVENT_CAPACITY);
        let inner = Arc::new(Inner {
            next_id: AtomicU64::new(1),
            pending: DashMap::new(),
            outgoing: outgoing_tx,
            events: events_tx.clone(),
            closed: AtomicBool::new(false),
        });

        let reader_inner = Arc::clone(&inner);
        tokio::spawn(async move {
            loop {
                tokio::select! {
                    message = read.next() => {
                        match message {
                            Some(Ok(tokio_tungstenite::tungstenite::Message::Text(text))) => {
                                dispatch_incoming(&reader_inner, &text);
                            }
                            Some(Ok(tokio_tungstenite::tungstenite::Message::Close(_))) | None => {
                                break;
                            }
                            Some(Ok(_)) => {} // pings/pongs/binary: not used by CDP
                            Some(Err(e)) => {
                                debug!(error = %e, "cdp read error");
                                break;
                            }
                        }
                    }
                    Some(cmd) = outgoing_rx.recv() => {
                        let mut body = json!({
                            "id": cmd.id,
                            "method": cmd.method,
                            "params": cmd.params,
                        });
                        if let Some(session_id) = &cmd.session_id {
                            body["sessionId"] = json!(session_id);
                        }
                        let text = body.to_string();
                        if write
                            .send(tokio_tungstenite::tungstenite::Message::Text(text.into()))
                            .await
                            .is_err()
                        {
                            break;
                        }
                    }
                }
            }
            // Teardown: fail every pending command and close the event
            // stream so subscribers observe the disconnect.
            reader_inner.closed.store(true, Ordering::SeqCst);
            drain_pending(&reader_inner.pending);
            drop(events_tx);
            debug!("cdp connection closed");
        });

        Ok(Self { inner })
    }

    pub fn is_closed(&self) -> bool {
        self.inner.closed.load(Ordering::SeqCst)
    }

    /// Subscribe to the CDP event stream (all targets, flatten mode).
    pub fn subscribe(&self) -> broadcast::Receiver<CdpEvent> {
        self.inner.events.subscribe()
    }

    /// Raw command with an optional target session.
    pub async fn call(
        &self,
        method: &str,
        params: Value,
        session_id: Option<&str>,
    ) -> Result<Value, CdpError> {
        if self.is_closed() {
            return Err(CdpError::Disconnected);
        }
        let id = self.inner.next_id.fetch_add(1, Ordering::Relaxed);
        let (tx, rx) = oneshot::channel();
        self.inner.pending.insert(id, tx);
        let send_result = self.inner.outgoing.send(Outgoing {
            id,
            method: method.to_string(),
            params,
            session_id: session_id.map(str::to_string),
        });
        if send_result.is_err() {
            self.inner.pending.remove(&id);
            return Err(CdpError::Disconnected);
        }
        match tokio::time::timeout(COMMAND_TIMEOUT, rx).await {
            Ok(Ok(result)) => result,
            Ok(Err(_closed)) => Err(CdpError::Disconnected),
            Err(_timeout) => {
                self.inner.pending.remove(&id);
                Err(CdpError::Timeout)
            }
        }
    }

    // ---- Target domain ----

    /// Flatten-mode attach; returns the session id used for per-target calls.
    pub async fn attach_to_target(&self, target_id: &str) -> Result<String, CdpError> {
        let result = self
            .call(
                "Target.attachToTarget",
                json!({ "targetId": target_id, "flatten": true }),
                None,
            )
            .await?;
        result
            .get("sessionId")
            .and_then(Value::as_str)
            .map(str::to_string)
            .ok_or(CdpError::BadMessage)
    }

    pub async fn create_target(&self, url: &str) -> Result<String, CdpError> {
        let result = self
            .call("Target.createTarget", json!({ "url": url }), None)
            .await?;
        result
            .get("targetId")
            .and_then(Value::as_str)
            .map(str::to_string)
            .ok_or(CdpError::BadMessage)
    }

    pub async fn close_target(&self, target_id: &str) -> Result<(), CdpError> {
        self.call("Target.closeTarget", json!({ "targetId": target_id }), None)
            .await?;
        Ok(())
    }

    pub async fn detach(&self, session_id: &str) -> Result<(), CdpError> {
        self.call(
            "Target.detachFromTarget",
            json!({ "sessionId": session_id }),
            None,
        )
        .await?;
        Ok(())
    }

    // ---- Browser domain ----

    /// Graceful browser shutdown (spec §14.2).
    pub async fn browser_close(&self) -> Result<(), CdpError> {
        self.call("Browser.close", json!({}), None).await?;
        Ok(())
    }

    /// Route downloads into the profile's `Downloads/` directory with events
    /// enabled so [`super::downloads`] can track them (spec §6.5).
    pub async fn set_download_behavior(&self, download_path: &str) -> Result<(), CdpError> {
        self.call(
            "Browser.setDownloadBehavior",
            json!({
                "behavior": "allow",
                "downloadPath": download_path,
                "eventsEnabled": true,
            }),
            None,
        )
        .await?;
        Ok(())
    }

    // ---- Page domain (session-scoped) ----

    pub async fn page_enable(&self, session_id: &str) -> Result<(), CdpError> {
        self.call("Page.enable", json!({}), Some(session_id))
            .await?;
        Ok(())
    }

    pub async fn runtime_enable(&self, session_id: &str) -> Result<(), CdpError> {
        self.call("Runtime.enable", json!({}), Some(session_id))
            .await?;
        Ok(())
    }

    pub async fn navigate(&self, session_id: &str, url: &str) -> Result<Value, CdpError> {
        self.call("Page.navigate", json!({ "url": url }), Some(session_id))
            .await
    }

    pub async fn start_screencast(
        &self,
        session_id: &str,
        format: &str,
        quality: u32,
        max_width: u32,
        max_height: u32,
    ) -> Result<(), CdpError> {
        self.call(
            "Page.startScreencast",
            json!({
                "format": format,
                "quality": quality,
                "maxWidth": max_width,
                "maxHeight": max_height,
                "everyNthFrame": 1,
            }),
            Some(session_id),
        )
        .await?;
        Ok(())
    }

    pub async fn stop_screencast(&self, session_id: &str) -> Result<(), CdpError> {
        self.call("Page.stopScreencast", json!({}), Some(session_id))
            .await?;
        Ok(())
    }

    /// Every frame must be acked or Chromium stops sending (spec §10.1).
    pub async fn screencast_frame_ack(
        &self,
        session_id: &str,
        cdp_session_frame_id: u64,
    ) -> Result<(), CdpError> {
        self.call(
            "Page.screencastFrameAck",
            json!({ "sessionId": cdp_session_frame_id }),
            Some(session_id),
        )
        .await?;
        Ok(())
    }

    pub async fn capture_screenshot(
        &self,
        session_id: &str,
        format: &str,
        quality: Option<u32>,
    ) -> Result<String, CdpError> {
        let mut params = json!({ "format": format });
        if let Some(quality) = quality {
            params["quality"] = json!(quality);
        }
        let result = self
            .call("Page.captureScreenshot", params, Some(session_id))
            .await?;
        result
            .get("data")
            .and_then(Value::as_str)
            .map(str::to_string)
            .ok_or(CdpError::BadMessage)
    }

    // ---- Emulation domain ----

    pub async fn set_device_metrics(
        &self,
        session_id: &str,
        width: u32,
        height: u32,
        device_scale_factor: f64,
    ) -> Result<(), CdpError> {
        self.call(
            "Emulation.setDeviceMetricsOverride",
            json!({
                "width": width,
                "height": height,
                "deviceScaleFactor": device_scale_factor,
                "mobile": false,
            }),
            Some(session_id),
        )
        .await?;
        Ok(())
    }

    // ---- Input domain ----

    pub async fn dispatch_mouse_event(
        &self,
        session_id: &str,
        params: Value,
    ) -> Result<(), CdpError> {
        self.call("Input.dispatchMouseEvent", params, Some(session_id))
            .await?;
        Ok(())
    }

    pub async fn dispatch_key_event(
        &self,
        session_id: &str,
        params: Value,
    ) -> Result<(), CdpError> {
        self.call("Input.dispatchKeyEvent", params, Some(session_id))
            .await?;
        Ok(())
    }

    pub async fn insert_text(&self, session_id: &str, text: &str) -> Result<(), CdpError> {
        self.call(
            "Input.insertText",
            json!({ "text": text }),
            Some(session_id),
        )
        .await?;
        Ok(())
    }

    pub async fn dispatch_touch_event(
        &self,
        session_id: &str,
        params: Value,
    ) -> Result<(), CdpError> {
        self.call("Input.dispatchTouchEvent", params, Some(session_id))
            .await?;
        Ok(())
    }

    // ---- DOM / file chooser ----

    /// Set approved files on a file input without a native picker (spec §15).
    pub async fn set_file_input_files(
        &self,
        session_id: &str,
        node_id: u64,
        files: &[String],
    ) -> Result<(), CdpError> {
        self.call(
            "DOM.setFileInputFiles",
            json!({ "files": files, "nodeId": node_id }),
            Some(session_id),
        )
        .await?;
        Ok(())
    }

    /// Intercept file chooser dialogs so the host shows its own approval UI
    /// (spec §15.2).
    pub async fn intercept_file_chooser(&self, session_id: &str) -> Result<(), CdpError> {
        self.call(
            "Page.setInterceptFileChooserDialog",
            json!({ "enabled": true }),
            Some(session_id),
        )
        .await?;
        Ok(())
    }
}

fn dispatch_incoming(inner: &Arc<Inner>, text: &str) {
    let value: Value = match serde_json::from_str(text) {
        Ok(value) => value,
        Err(e) => {
            debug!(error = %e, "ignoring malformed cdp message");
            return;
        }
    };
    if let Some(id) = value.get("id").and_then(Value::as_u64) {
        let result = if let Some(error) = value.get("error") {
            Err(CdpError::Protocol {
                code: error.get("code").and_then(Value::as_i64).unwrap_or(-1),
                message: error
                    .get("message")
                    .and_then(Value::as_str)
                    .unwrap_or("unknown CDP error")
                    .to_string(),
            })
        } else {
            Ok(value.get("result").cloned().unwrap_or(Value::Null))
        };
        if let Some((_, responder)) = inner.pending.remove(&id) {
            let _ = responder.send(result);
        } else {
            debug!(id, "cdp response for unknown command id");
        }
        return;
    }
    if let Some(method) = value.get("method").and_then(Value::as_str) {
        let event = CdpEvent {
            session_id: value
                .get("sessionId")
                .and_then(Value::as_str)
                .map(str::to_string),
            method: method.to_string(),
            params: value.get("params").cloned().unwrap_or(Value::Null),
        };
        // Lagging receivers drop events; never block the reader loop.
        if let Err(e) = inner.events.send(event) {
            debug!(error = %e, "no cdp event subscribers");
        }
    }
}

fn drain_pending(pending: &PendingMap) {
    let ids: Vec<u64> = pending.iter().map(|entry| *entry.key()).collect();
    for id in ids {
        if let Some((_, responder)) = pending.remove(&id) {
            let _ = responder.send(Err(CdpError::Disconnected));
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use tokio::net::TcpListener;
    use tokio_tungstenite::tungstenite::Message;

    /// A fake CDP server speaking just enough protocol for navigate +
    /// screencast flow tests (spec §20 "launch a fake Chromium/CDP server").
    struct FakeCdpServer {
        addr: std::net::SocketAddr,
        received: mpsc::UnboundedReceiver<Value>,
        shutdown: oneshot::Sender<()>,
    }

    impl FakeCdpServer {
        async fn start() -> (Self, mpsc::UnboundedSender<Value>) {
            let listener = TcpListener::bind((std::net::Ipv4Addr::LOCALHOST, 0))
                .await
                .unwrap();
            let addr = listener.local_addr().unwrap();
            let (received_tx, received) = mpsc::unbounded_channel();
            let (events_tx, events_rx) = mpsc::unbounded_channel::<Value>();
            let (shutdown_tx, mut shutdown_rx) = oneshot::channel::<()>();
            tokio::spawn(async move {
                let Ok((stream, _)) = listener.accept().await else {
                    return;
                };
                let socket = tokio_tungstenite::accept_async(stream).await.unwrap();
                let (mut write, mut read) = socket.split();
                let mut events_rx = events_rx;
                loop {
                    tokio::select! {
                        _ = &mut shutdown_rx => break,
                        message = read.next() => {
                            let Some(Ok(Message::Text(text))) = message else { break };
                            let Ok(value) = serde_json::from_str::<Value>(&text) else { continue };
                            let _ = received_tx.send(value.clone());
                            let id = value.get("id").cloned().unwrap_or(Value::Null);
                            let method = value.get("method").and_then(Value::as_str).unwrap_or("");
                            let result = match method {
                                "Target.attachToTarget" => json!({"sessionId": "sess-1"}),
                                "Target.createTarget" => json!({"targetId": "target-1"}),
                                "Page.navigate" => json!({"frameId": "frame-1", "loaderId": "l-1"}),
                                "Page.captureScreenshot" => json!({"data": "aGVsbG8="}),
                                _ => json!({}),
                            };
                            let reply = json!({"id": id, "result": result}).to_string();
                            if write.send(Message::Text(reply.into())).await.is_err() {
                                break;
                            }
                        }
                        Some(event) = events_rx.recv() => {
                            let text = event.to_string();
                            if write.send(Message::Text(text.into())).await.is_err() {
                                break;
                            }
                        }
                    }
                }
            });
            (
                Self {
                    addr,
                    received,
                    shutdown: shutdown_tx,
                },
                events_tx,
            )
        }

        fn ws_url(&self) -> String {
            format!("ws://{}/devtools/browser/fake", self.addr)
        }

        async fn next_command(&mut self) -> Value {
            tokio::time::timeout(Duration::from_secs(5), self.received.recv())
                .await
                .expect("command within timeout")
                .expect("channel open")
        }
    }

    #[tokio::test]
    async fn navigate_and_screencast_flow() {
        let (mut server, events) = FakeCdpServer::start().await;
        let client = CdpClient::connect(&server.ws_url()).await.unwrap();
        let mut event_stream = client.subscribe();

        let session = client.attach_to_target("target-1").await.unwrap();
        assert_eq!(session, "sess-1");

        let result = client
            .navigate(&session, "https://example.com")
            .await
            .unwrap();
        assert_eq!(result["frameId"], "frame-1");

        client
            .start_screencast(&session, "jpeg", 75, 1280, 720)
            .await
            .unwrap();

        // The fake browser emits a frame; the client forwards it as an event
        // and the ack round-trips back to the server.
        events
            .send(json!({
                "method": "Page.screencastFrame",
                "sessionId": "sess-1",
                "params": {"data": "aGVsbG8=", "sessionId": 7, "metadata": {"deviceWidth": 1280, "deviceHeight": 720}}
            }))
            .unwrap();
        let event = tokio::time::timeout(Duration::from_secs(5), event_stream.recv())
            .await
            .unwrap()
            .unwrap();
        assert_eq!(event.method, "Page.screencastFrame");
        assert_eq!(event.session_id.as_deref(), Some("sess-1"));
        client.screencast_frame_ack(&session, 7).await.unwrap();

        // Verify wire shapes the fake server observed.
        let attach = server.next_command().await;
        assert_eq!(attach["method"], "Target.attachToTarget");
        assert_eq!(attach["params"]["flatten"], true);
        let navigate = server.next_command().await;
        assert_eq!(navigate["method"], "Page.navigate");
        assert_eq!(navigate["sessionId"], "sess-1");
        let start = server.next_command().await;
        assert_eq!(start["method"], "Page.startScreencast");
        assert_eq!(start["params"]["quality"], 75);
        let ack = server.next_command().await;
        assert_eq!(ack["method"], "Page.screencastFrameAck");
        assert_eq!(ack["params"]["sessionId"], 7);

        let screenshot = client
            .capture_screenshot(&session, "jpeg", Some(80))
            .await
            .unwrap();
        assert_eq!(screenshot, "aGVsbG8=");

        let _ = server.shutdown.send(());
    }

    #[tokio::test]
    async fn disconnect_fails_pending_commands() {
        let (server, _events) = FakeCdpServer::start().await;
        let client = CdpClient::connect(&server.ws_url()).await.unwrap();
        // Kill the server without answering: pending + future calls fail.
        let shutdown = server.shutdown;
        let _ = shutdown.send(());
        let deadline = std::time::Instant::now() + Duration::from_secs(5);
        loop {
            match client.call("Browser.getVersion", json!({}), None).await {
                Err(CdpError::Disconnected) => break,
                _ if std::time::Instant::now() < deadline => {
                    tokio::time::sleep(Duration::from_millis(20)).await;
                }
                other => panic!("expected Disconnected, got {:?}", other.is_ok()),
            }
        }
        assert!(client.is_closed());
    }
}
