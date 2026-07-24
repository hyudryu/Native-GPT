//! Shared application state handed to handlers and middleware.

use std::net::Ipv4Addr;
use std::path::PathBuf;
use std::sync::Arc;
use std::time::Instant;

use agentgpt_supervisor::protocol::Envelope;
use agentgpt_supervisor::Supervisor;
use agentgpt_telemetry::Telemetry;
use tokio::sync::broadcast;

use crate::db::Db;
use crate::secrets::KeyStore;

/// Capacity of the host-originated broadcast channel (M3 multi-client sync).
pub const HOST_EVENTS_CAPACITY: usize = 256;

pub struct AppState {
    /// Bearer token for non-localhost auth. Never logged.
    pub token: String,
    /// Bound TCP port.
    pub port: u16,
    /// Process start (for uptime reporting).
    pub started: Instant,
    /// Tailscale interface addresses we bound (ADR-0003).
    pub tailscale_ips: Vec<Ipv4Addr>,
    /// `apps/ui/dist` directory (may not exist yet).
    pub ui_dist: PathBuf,
    /// Repository root containing the built-in `/tools` folders.
    pub repo_root: PathBuf,
    pub supervisor: Supervisor,
    pub telemetry: Telemetry,
    pub db: Db,
    /// Keychain for endpoint API keys (service "agentgpt", key = endpoint id).
    pub secrets: Arc<dyn KeyStore>,
    /// Host-originated broadcast envelopes (e.g. `data.changed`), forwarded
    /// to every WS client alongside supervisor events.
    pub host_events: broadcast::Sender<Envelope>,
    /// Native GPT Browser orchestration (ADR-0009). Cheap to construct;
    /// Chromium starts lazily on first use.
    pub browser: crate::browser::manager::BrowserManager,
}

pub type SharedState = Arc<AppState>;

/// State for unit tests: tempdir DB, in-memory keychain, scripted sidecar.
#[cfg(test)]
pub(crate) struct TestState {
    pub state: SharedState,
    pub secrets: Arc<crate::secrets::MemoryKeyStore>,
    dir: PathBuf,
}

#[cfg(test)]
impl Drop for TestState {
    fn drop(&mut self) {
        // `state` (holding the DB connection) is a field; it is dropped after
        // this body runs, so remove files best-effort only. Tests that need
        // clean removal drop their routers/clones first.
        let _ = std::fs::remove_dir_all(&self.dir);
    }
}

#[cfg(test)]
fn make_test_state(token: &str, sidecar: agentgpt_supervisor::SupervisorConfig) -> TestState {
    let dir = std::env::temp_dir().join(format!("agentgpt-server-test-{}", uuid::Uuid::now_v7()));
    let db = Db::open(&dir.join("agentgpt.sqlite3")).expect("open test db");
    let secrets = Arc::new(crate::secrets::MemoryKeyStore::new());
    let state = Arc::new(AppState {
        token: token.to_string(),
        port: 0,
        started: Instant::now(),
        tailscale_ips: Vec::new(),
        ui_dist: std::env::temp_dir().join("agentgpt-test-ui-dist"),
        repo_root: dir.clone(),
        supervisor: Supervisor::new(sidecar),
        telemetry: Telemetry::new(),
        db: db.clone(),
        secrets: secrets.clone(),
        host_events: broadcast::channel(HOST_EVENTS_CAPACITY).0,
        browser: crate::browser::manager::BrowserManager::new(db, dir.clone(), 0),
    });
    TestState {
        state,
        secrets,
        dir,
    }
}

/// Test state whose sidecar never spawns successfully (auth tests etc.).
#[cfg(test)]
pub(crate) fn test_state(token: &str) -> TestState {
    make_test_state(
        token,
        agentgpt_supervisor::SupervisorConfig {
            program: "false".to_string(),
            args: Vec::new(),
            cwd: std::env::temp_dir(),
            idle_timeout: std::time::Duration::from_secs(600),
            request_timeout: std::time::Duration::from_secs(1),
        },
    )
}

/// Test state wired to the `fake_sidecar` binary from `agentgpt-supervisor`.
///
/// The binary must be built (it is, for `cargo test --workspace`; for
/// `cargo test -p agentgpt-server` run `cargo build -p agentgpt-supervisor`
/// first).
#[cfg(test)]
pub(crate) fn test_state_with_fake_sidecar(token: &str) -> TestState {
    let mut program = std::env::current_exe().expect("current exe");
    program.pop(); // deps/
    program.pop(); // target/<profile>/
    program.push(format!("fake_sidecar{}", std::env::consts::EXE_SUFFIX));
    assert!(
        program.is_file(),
        "fake_sidecar binary not found at {program:?}; build the workspace first"
    );
    make_test_state(
        token,
        agentgpt_supervisor::SupervisorConfig {
            program: program.to_string_lossy().into_owned(),
            args: Vec::new(),
            cwd: std::env::temp_dir(),
            idle_timeout: std::time::Duration::from_secs(600),
            request_timeout: std::time::Duration::from_secs(10),
        },
    )
}
