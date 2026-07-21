//! Supervisor for the Python agent-runtime sidecar.
//!
//! Spawns the sidecar lazily on first request, speaks NDJSON over
//! stdin/stdout (protocol v1.0, see `packages/protocol-types`), correlates
//! responses to requests by `request_id`, forwards stderr to `tracing`,
//! terminates the sidecar after an idle timeout (ADR-0004) and respawns it
//! after a crash (ADR-0002).

pub mod protocol;

use std::path::PathBuf;
use std::process::Stdio;
use std::sync::atomic::{AtomicU32, Ordering};
use std::sync::{Mutex, MutexGuard};
use std::time::{Duration, Instant};

use dashmap::DashMap;
use protocol::Envelope;
use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};
use tokio::process::{Child, ChildStdin, Command};
use tokio::sync::{broadcast, oneshot};
use tokio::task::JoinHandle;
use tracing::{debug, info, warn};

pub use protocol::PROTOCOL_VERSION;

/// Default idle timeout after which the sidecar is shut down (ADR-0004).
pub const DEFAULT_IDLE_TIMEOUT: Duration = Duration::from_secs(10 * 60);
/// Default deadline for a single request/response round-trip.
pub const DEFAULT_REQUEST_TIMEOUT: Duration = Duration::from_secs(30);
/// Default spawn command when `AGENTGPT_SIDECAR_CMD` is not set.
pub const DEFAULT_SIDECAR_CMD: &str =
    "uv run --directory apps/agent-runtime python -m agentgpt_runtime";

const BROADCAST_CAPACITY: usize = 256;

/// Lifecycle state of the sidecar process.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SidecarState {
    NotSpawned,
    Starting,
    Running,
    Stopping,
}

impl SidecarState {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::NotSpawned => "not_spawned",
            Self::Starting => "starting",
            Self::Running => "running",
            Self::Stopping => "stopping",
        }
    }
}

/// Configuration for spawning and supervising the sidecar.
#[derive(Debug, Clone)]
pub struct SupervisorConfig {
    /// Program to spawn (e.g. `uv`).
    pub program: String,
    /// Arguments for `program`.
    pub args: Vec<String>,
    /// Working directory of the sidecar (repo root).
    pub cwd: PathBuf,
    /// Shut the sidecar down after this much inactivity.
    pub idle_timeout: Duration,
    /// Deadline for a single request/response round-trip.
    pub request_timeout: Duration,
}

impl SupervisorConfig {
    /// Resolve the spawn command: `AGENTGPT_SIDECAR_CMD` if set (split on
    /// whitespace), otherwise the default `uv run ... agentgpt_runtime`.
    pub fn from_env(repo_root: PathBuf, idle_timeout: Duration) -> Self {
        let cmd = std::env::var("AGENTGPT_SIDECAR_CMD")
            .ok()
            .filter(|c| !c.trim().is_empty())
            .unwrap_or_else(|| DEFAULT_SIDECAR_CMD.to_string());
        let mut parts = cmd.split_whitespace();
        let program = parts.next().unwrap_or("uv").to_string();
        let args = parts.map(str::to_string).collect();
        Self {
            program,
            args,
            cwd: repo_root,
            idle_timeout,
            request_timeout: DEFAULT_REQUEST_TIMEOUT,
        }
    }
}

/// Errors surfaced to callers of [`Supervisor::request`].
#[derive(Debug, thiserror::Error)]
pub enum SupervisorError {
    #[error("failed to spawn sidecar: {0}")]
    Spawn(String),
    #[error("sidecar exited while the request was in flight")]
    Crashed,
    #[error("sidecar did not respond in time")]
    Timeout,
    #[error("failed to encode request envelope: {0}")]
    Encode(String),
}

impl SupervisorError {
    /// Protocol `error` payload code for this failure.
    pub fn code(&self) -> &'static str {
        match self {
            Self::Spawn(_) => "sidecar_spawn_failed",
            Self::Crashed => "sidecar_crashed",
            Self::Timeout => "request_timeout",
            Self::Encode(_) => "bad_request",
        }
    }
}

/// Pending request/response correlation map: `request_id` -> reply channel
/// plus request metadata used for stream tracking.
pub type PendingMap = DashMap<String, PendingRequest>;

/// A pending request awaiting its terminal response.
#[derive(Debug)]
pub struct PendingRequest {
    pub tx: oneshot::Sender<Envelope>,
    /// Kind of the request (e.g. `run.start`).
    pub kind: String,
    /// `run_id` from a `run.start` payload, when present.
    pub run_id: Option<String>,
}

/// Active run streams: stream `request_id` -> `run_id` (when known). A stream
/// becomes active when a `run.start` ack is delivered (or a `run.*` event
/// arrives for an untracked request_id) and ends on `run.completed` /
/// `run.failed`. Used to synthesize `run.failed` on sidecar exit.
pub type StreamMap = DashMap<String, Option<String>>;

struct ChildHandle {
    child: Child,
    stdin: ChildStdin,
}

struct Inner {
    config: SupervisorConfig,
    child: tokio::sync::Mutex<Option<ChildHandle>>,
    pending: PendingMap,
    streams: StreamMap,
    events: broadcast::Sender<Envelope>,
    last_activity: tokio::sync::Mutex<Instant>,
    state: Mutex<SidecarState>,
    pid: AtomicU32,
    telemetry: agentgpt_telemetry::Telemetry,
}

impl Inner {
    fn set_state(&self, state: SidecarState) {
        *lock(&self.state) = state;
    }

    fn state(&self) -> SidecarState {
        *lock(&self.state)
    }

    async fn touch(&self) {
        *self.last_activity.lock().await = Instant::now();
    }
}

fn lock<T>(mutex: &Mutex<T>) -> MutexGuard<'_, T> {
    mutex.lock().unwrap_or_else(|e| e.into_inner())
}

/// Supervises one sidecar child process. Cheap to clone (shares state).
#[derive(Clone)]
pub struct Supervisor {
    inner: std::sync::Arc<Inner>,
}

impl Supervisor {
    pub fn new(config: SupervisorConfig) -> Self {
        let (events, _) = broadcast::channel(BROADCAST_CAPACITY);
        Self {
            inner: std::sync::Arc::new(Inner {
                config,
                child: tokio::sync::Mutex::new(None),
                pending: DashMap::new(),
                streams: DashMap::new(),
                events,
                last_activity: tokio::sync::Mutex::new(Instant::now()),
                state: Mutex::new(SidecarState::NotSpawned),
                pid: AtomicU32::new(0),
                telemetry: agentgpt_telemetry::Telemetry::new(),
            }),
        }
    }

    /// Broadcast channel of sidecar events (envelopes that do not answer a
    /// pending request). Subscribe to forward them to WS clients.
    pub fn events(&self) -> broadcast::Sender<Envelope> {
        self.inner.events.clone()
    }

    pub fn state(&self) -> SidecarState {
        self.inner.state()
    }

    /// PID of the running sidecar, if any.
    pub fn pid(&self) -> Option<u32> {
        match self.inner.pid.load(Ordering::Relaxed) {
            0 => None,
            pid => Some(pid),
        }
    }

    /// RSS of the running sidecar in bytes, if it is running.
    pub fn rss_bytes(&self) -> Option<u64> {
        self.pid()
            .and_then(|pid| self.inner.telemetry.process_rss_bytes(pid))
    }

    /// Send a request envelope and await its terminal response.
    ///
    /// Spawns the sidecar lazily on first use. If the sidecar dies while the
    /// request is in flight, the returned envelope is an `error` with code
    /// `sidecar_crashed`.
    pub async fn request(&self, env: Envelope) -> Result<Envelope, SupervisorError> {
        self.inner.touch().await;
        self.ensure_spawned().await?;
        self.round_trip(env, self.inner.config.request_timeout)
            .await
    }

    /// Gracefully stop the sidecar: send `runtime.shutdown`, wait briefly,
    /// then kill. No-op when the sidecar is not running.
    pub async fn shutdown(&self) {
        if self.pid().is_none() {
            return;
        }
        info!("shutting down sidecar");
        self.inner.set_state(SidecarState::Stopping);
        let req = Envelope::new("runtime.shutdown", serde_json::json!({}));
        // Best effort; the process may legitimately exit without replying.
        let _ = self.round_trip(req, Duration::from_secs(5)).await;
        let mut guard = self.inner.child.lock().await;
        if let Some(handle) = guard.take() {
            drop(handle); // kill_on_drop ensures the process is dead
        }
        self.inner.pid.store(0, Ordering::Relaxed);
        self.inner.set_state(SidecarState::NotSpawned);
        self.drain_pending("sidecar_shutdown", "sidecar was shut down");
        fail_active_streams(
            &self.inner.streams,
            &self.inner.events,
            "sidecar_shutdown",
            "sidecar was shut down",
        );
    }

    /// Spawn the idle-timeout watchdog task (ADR-0004). Event-driven: sleeps
    /// until the next idle deadline rather than polling.
    pub fn start_idle_watchdog(&self) -> JoinHandle<()> {
        let inner = self.inner.clone();
        tokio::spawn(async move {
            loop {
                let timeout = inner.config.idle_timeout;
                let elapsed = inner.last_activity.lock().await.elapsed();
                if elapsed < timeout {
                    tokio::time::sleep(timeout - elapsed).await;
                    continue;
                }
                if inner.child.lock().await.is_some() {
                    info!(?timeout, "sidecar idle timeout reached; shutting down");
                    let supervisor = Supervisor {
                        inner: inner.clone(),
                    };
                    supervisor.shutdown().await;
                }
                // Reset so an absent child does not spin the loop.
                inner.touch().await;
            }
        })
    }

    async fn ensure_spawned(&self) -> Result<(), SupervisorError> {
        let mut guard = self.inner.child.lock().await;
        if guard.is_some() {
            return Ok(());
        }
        self.inner.set_state(SidecarState::Starting);
        let config = &self.inner.config;
        info!(program = %config.program, "spawning sidecar");
        let mut cmd = Command::new(&config.program);
        cmd.args(&config.args)
            .current_dir(&config.cwd)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            // The NDJSON protocol channel is UTF-8; Windows consoles default
            // to cp1252, which cannot encode much of typical model output.
            .env("PYTHONIOENCODING", "utf-8")
            .kill_on_drop(true);
        // Do not pop up a console window for the sidecar on Windows.
        #[cfg(windows)]
        {
            const CREATE_NO_WINDOW: u32 = 0x0800_0000;
            cmd.creation_flags(CREATE_NO_WINDOW);
        }
        let mut child = cmd
            .spawn()
            .map_err(|e| SupervisorError::Spawn(format!("{}: {e}", config.program)))?;
        let stdin = child
            .stdin
            .take()
            .ok_or_else(|| SupervisorError::Spawn("stdin not piped".into()))?;
        let stdout = child
            .stdout
            .take()
            .ok_or_else(|| SupervisorError::Spawn("stdout not piped".into()))?;
        let stderr = child
            .stderr
            .take()
            .ok_or_else(|| SupervisorError::Spawn("stderr not piped".into()))?;
        let pid = child.id().unwrap_or(0);
        self.inner.pid.store(pid, Ordering::Relaxed);
        *guard = Some(ChildHandle { child, stdin });
        drop(guard);

        // stdout reader: route responses to pending requests, broadcast events.
        // Any received line counts as activity (M2: streaming runs must not
        // trip the idle watchdog). EOF means the process exited -> clean up
        // (event-driven, no polling).
        let inner = self.inner.clone();
        tokio::spawn(async move {
            let mut lines = BufReader::new(stdout).lines();
            loop {
                match lines.next_line().await {
                    Ok(Some(line)) => {
                        *inner.last_activity.lock().await = Instant::now();
                        route_incoming_line(&line, &inner.pending, &inner.streams, &inner.events);
                    }
                    Ok(None) => break,
                    Err(e) => {
                        debug!("sidecar stdout read error: {e}");
                        break;
                    }
                }
            }
            // Process exited (stdout EOF). Clear the handle if it is still the
            // same child and fail all in-flight requests and active streams.
            let was_current = {
                let mut guard = inner.child.lock().await;
                if guard.as_ref().and_then(|h| h.child.id()) == Some(pid) {
                    drop(guard.take());
                    true
                } else {
                    false
                }
            };
            if was_current {
                inner.pid.store(0, Ordering::Relaxed);
                if inner.state() != SidecarState::Stopping {
                    warn!(pid, "sidecar exited unexpectedly");
                    inner.set_state(SidecarState::NotSpawned);
                }
                let supervisor = Supervisor {
                    inner: inner.clone(),
                };
                supervisor.drain_pending("sidecar_crashed", "sidecar process exited");
                fail_active_streams(
                    &inner.streams,
                    &inner.events,
                    "sidecar_crashed",
                    "agent runtime exited unexpectedly",
                );
            }
        });

        // stderr reader: sidecar logs go to tracing only (ADR-0002).
        tokio::spawn(async move {
            let mut lines = BufReader::new(stderr).lines();
            while let Ok(Some(line)) = lines.next_line().await {
                info!(target: "agentgpt::sidecar", "{line}");
            }
        });

        self.inner.set_state(SidecarState::Running);
        Ok(())
    }

    async fn round_trip(
        &self,
        env: Envelope,
        timeout: Duration,
    ) -> Result<Envelope, SupervisorError> {
        let request_id = env.request_id.clone();
        let line = encode_line(&env).map_err(|e| SupervisorError::Encode(e.to_string()))?;
        let (tx, rx) = oneshot::channel();
        self.inner.pending.insert(
            request_id.clone(),
            PendingRequest {
                tx,
                kind: env.kind.clone(),
                run_id: env
                    .payload
                    .get("run_id")
                    .and_then(serde_json::Value::as_str)
                    .map(str::to_string),
            },
        );
        {
            let mut guard = self.inner.child.lock().await;
            let Some(handle) = guard.as_mut() else {
                self.inner.pending.remove(&request_id);
                return Err(SupervisorError::Crashed);
            };
            if let Err(e) = handle.stdin.write_all(line.as_bytes()).await {
                self.inner.pending.remove(&request_id);
                warn!("failed to write to sidecar stdin: {e}");
                return Err(SupervisorError::Crashed);
            }
            if let Err(e) = handle.stdin.flush().await {
                self.inner.pending.remove(&request_id);
                warn!("failed to flush sidecar stdin: {e}");
                return Err(SupervisorError::Crashed);
            }
        }
        match tokio::time::timeout(timeout, rx).await {
            Ok(Ok(resp)) => Ok(resp),
            Ok(Err(_)) => Err(SupervisorError::Crashed),
            Err(_) => {
                self.inner.pending.remove(&request_id);
                Err(SupervisorError::Timeout)
            }
        }
    }

    fn drain_pending(&self, code: &str, message: &str) {
        fail_all_pending(&self.inner.pending, code, message);
    }
}

/// Encode an envelope as one NDJSON line (trailing newline included).
pub fn encode_line(env: &Envelope) -> Result<String, serde_json::Error> {
    let mut line = serde_json::to_string(env)?;
    line.push('\n');
    Ok(line)
}

/// Route one stdout line from the sidecar: envelopes whose `request_id`
/// matches a pending request complete that request; everything else is
/// broadcast as an event. Also maintains the active-stream registry (M1):
/// a delivered `run.start` ack opens a stream, `run.*` events for untracked
/// request_ids register one, and `run.completed`/`run.failed` closes it.
/// Unparseable lines are logged and ignored.
pub fn route_incoming_line(
    line: &str,
    pending: &PendingMap,
    streams: &StreamMap,
    events: &broadcast::Sender<Envelope>,
) {
    let env: Envelope = match serde_json::from_str(line) {
        Ok(env) => env,
        Err(e) => {
            warn!("ignoring non-JSON sidecar line: {e}");
            return;
        }
    };
    if let Some((_, request)) = pending.remove(&env.request_id) {
        if request.kind == "run.start" && env.kind != "error" {
            // The ack consumed the oneshot; the run's events now flow on the
            // broadcast channel. Track the stream so a crash can fail it.
            let run_id = request.run_id.or_else(|| payload_run_id(&env));
            streams.insert(env.request_id.clone(), run_id);
        }
        let _ = request.tx.send(env);
        return;
    }
    match env.kind.as_str() {
        "run.completed" | "run.failed" => {
            streams.remove(&env.request_id);
        }
        kind if kind.starts_with("run.") => {
            // First event for a request_id with no pending entry (e.g. the
            // ack raced ahead of tracking): register the stream.
            streams
                .entry(env.request_id.clone())
                .or_insert_with(|| payload_run_id(&env));
        }
        _ => {}
    }
    // No subscriber is fine.
    let _ = events.send(env);
}

fn payload_run_id(env: &Envelope) -> Option<String> {
    env.payload
        .get("run_id")
        .and_then(serde_json::Value::as_str)
        .map(str::to_string)
}

/// Fail every pending request with an `error` envelope carrying `code`.
pub fn fail_all_pending(pending: &PendingMap, code: &str, message: &str) {
    let ids: Vec<String> = pending.iter().map(|r| r.key().clone()).collect();
    for id in ids {
        if let Some((_, request)) = pending.remove(&id) {
            let _ = request.tx.send(Envelope::error(id, code, message, true));
        }
    }
}

/// Broadcast a synthetic `run.failed` for every active stream (sidecar exit
/// or shutdown). The payload carries `run_id` when known so host-side run
/// persistence (which matches on `request_id` + `payload.run_id`) can mark
/// the run terminated instead of leaking it in "running" state.
pub fn fail_active_streams(
    streams: &StreamMap,
    events: &broadcast::Sender<Envelope>,
    code: &str,
    message: &str,
) {
    let ids: Vec<String> = streams.iter().map(|r| r.key().clone()).collect();
    for id in ids {
        if let Some((_, run_id)) = streams.remove(&id) {
            let mut payload = serde_json::json!({
                "error": { "code": code, "message": message, "retryable": true }
            });
            if let Some(run_id) = run_id {
                payload["run_id"] = serde_json::Value::String(run_id);
            }
            let mut env = Envelope::new("run.failed", payload);
            env.request_id = id;
            let _ = events.send(env);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn test_channels() -> (
        PendingMap,
        StreamMap,
        broadcast::Sender<Envelope>,
        broadcast::Receiver<Envelope>,
    ) {
        let (tx, rx) = broadcast::channel(8);
        (DashMap::new(), DashMap::new(), tx, rx)
    }

    fn pending_request(tx: oneshot::Sender<Envelope>, kind: &str) -> PendingRequest {
        PendingRequest {
            tx,
            kind: kind.to_string(),
            run_id: None,
        }
    }

    #[test]
    fn encode_line_is_single_ndjson_line() {
        let env = Envelope::new("runtime.health", json!({}));
        let line = encode_line(&env).expect("encode");
        assert!(line.ends_with('\n'));
        assert_eq!(line.matches('\n').count(), 1);
        let back: Envelope = serde_json::from_str(line.trim_end()).expect("decode");
        assert_eq!(back.kind, "runtime.health");
    }

    #[tokio::test]
    async fn correlated_response_completes_pending_request() {
        let (pending, streams, events, _rx) = test_channels();
        let (tx, rx) = oneshot::channel();
        pending.insert("req-1".to_string(), pending_request(tx, "runtime.health"));
        let resp = Envelope::error("req-1", "boom", "it broke", false);
        let line = serde_json::to_string(&resp).unwrap();
        route_incoming_line(&line, &pending, &streams, &events);
        let got = rx.await.expect("response delivered");
        assert_eq!(got.request_id, "req-1");
        assert!(pending.is_empty());
    }

    #[tokio::test]
    async fn uncorrelated_message_is_broadcast_as_event() {
        let (pending, streams, events, mut rx) = test_channels();
        let event = Envelope::new("run.text_delta", json!({"text": "hi"}));
        let line = serde_json::to_string(&event).unwrap();
        route_incoming_line(&line, &pending, &streams, &events);
        let got = rx.try_recv().expect("event broadcast");
        assert_eq!(got.kind, "run.text_delta");
    }

    #[test]
    fn non_json_line_is_ignored() {
        let (pending, streams, events, mut rx) = test_channels();
        route_incoming_line("not json at all", &pending, &streams, &events);
        assert!(rx.try_recv().is_err());
        assert!(pending.is_empty());
    }

    #[tokio::test]
    async fn fail_all_pending_marks_requests_with_error_code() {
        let (pending, _streams, _events, _rx) = test_channels();
        let (tx1, rx1) = oneshot::channel();
        let (tx2, rx2) = oneshot::channel();
        pending.insert("a".to_string(), pending_request(tx1, "runtime.health"));
        pending.insert("b".to_string(), pending_request(tx2, "runtime.health"));
        fail_all_pending(&pending, "sidecar_crashed", "sidecar process exited");
        for (rx, id) in [(rx1, "a"), (rx2, "b")] {
            let env = rx.await.expect("error delivered");
            assert_eq!(env.kind, "error");
            assert_eq!(env.request_id, id);
            assert_eq!(env.error_code(), Some("sidecar_crashed"));
        }
        assert!(pending.is_empty());
    }

    #[tokio::test]
    async fn run_start_ack_opens_stream_and_terminal_event_closes_it() {
        let (pending, streams, events, _rx) = test_channels();
        let (tx, _rx) = oneshot::channel();
        pending.insert(
            "req-run".to_string(),
            PendingRequest {
                tx,
                kind: "run.start".to_string(),
                run_id: Some("run-1".to_string()),
            },
        );
        // Ack delivery registers the stream.
        let mut ack = Envelope::new("run.start.ok", json!({"run_id": "run-1"}));
        ack.request_id = "req-run".to_string();
        let line = serde_json::to_string(&ack).unwrap();
        route_incoming_line(&line, &pending, &streams, &events);
        assert_eq!(
            streams.get("req-run").as_deref(),
            Some(&Some("run-1".to_string()))
        );

        // Streaming events keep it registered...
        let mut delta = Envelope::new("run.text_delta", json!({"run_id": "run-1", "text": "hi"}));
        delta.request_id = "req-run".to_string();
        let line = serde_json::to_string(&delta).unwrap();
        route_incoming_line(&line, &pending, &streams, &events);
        assert!(streams.contains_key("req-run"));

        // ...and the terminal event closes it.
        let mut done = Envelope::new("run.completed", json!({"run_id": "run-1"}));
        done.request_id = "req-run".to_string();
        let line = serde_json::to_string(&done).unwrap();
        route_incoming_line(&line, &pending, &streams, &events);
        assert!(!streams.contains_key("req-run"));
    }

    #[tokio::test]
    async fn fail_active_streams_broadcasts_synthetic_run_failed() {
        let (_pending, streams, events, mut rx) = test_channels();
        streams.insert("req-run".to_string(), Some("run-1".to_string()));
        fail_active_streams(
            &streams,
            &events,
            "sidecar_crashed",
            "agent runtime exited unexpectedly",
        );
        let env = rx.try_recv().expect("synthetic event");
        assert_eq!(env.kind, "run.failed");
        assert_eq!(env.request_id, "req-run");
        assert_eq!(env.payload["run_id"], json!("run-1"));
        assert_eq!(env.payload["error"]["code"], json!("sidecar_crashed"));
        assert_eq!(env.payload["error"]["retryable"], json!(true));
        assert!(streams.is_empty());
    }
}
