//! End-to-end supervisor tests against the `fake_sidecar` test binary.

use std::path::PathBuf;
use std::time::Duration;

use agentgpt_supervisor::protocol::Envelope;
use agentgpt_supervisor::{SidecarState, Supervisor, SupervisorConfig};

fn test_config() -> SupervisorConfig {
    SupervisorConfig {
        program: env!("CARGO_BIN_EXE_fake_sidecar").to_string(),
        args: Vec::new(),
        cwd: std::env::temp_dir(),
        idle_timeout: Duration::from_secs(600),
        request_timeout: Duration::from_secs(10),
    }
}

#[tokio::test]
async fn lazy_spawn_request_response_round_trip() {
    let supervisor = Supervisor::new(test_config());
    assert_eq!(supervisor.state(), SidecarState::NotSpawned);

    let req = Envelope::new("runtime.health", serde_json::json!({}));
    let request_id = req.request_id.clone();
    let resp = supervisor.request(req).await.expect("round trip");

    assert_eq!(resp.request_id, request_id);
    assert_eq!(resp.kind, "runtime.health.ok");
    assert_eq!(supervisor.state(), SidecarState::Running);
    assert!(supervisor.pid().is_some());
    assert!(supervisor.rss_bytes().unwrap_or(0) > 0);

    supervisor.shutdown().await;
    assert_eq!(supervisor.state(), SidecarState::NotSpawned);
    assert_eq!(supervisor.pid(), None);
}

#[tokio::test]
async fn respawns_after_child_exit() {
    let supervisor = Supervisor::new(test_config());
    let first = supervisor
        .request(Envelope::new("runtime.health", serde_json::json!({})))
        .await
        .expect("first round trip");
    assert_eq!(first.kind, "runtime.health.ok");

    // Graceful shutdown makes the fake sidecar exit; next request respawns.
    supervisor.shutdown().await;
    assert_eq!(supervisor.state(), SidecarState::NotSpawned);

    let second = supervisor
        .request(Envelope::new("runtime.health", serde_json::json!({})))
        .await
        .expect("second round trip after respawn");
    assert_eq!(second.kind, "runtime.health.ok");
    assert_eq!(supervisor.state(), SidecarState::Running);

    supervisor.shutdown().await;
}

#[tokio::test]
async fn spawn_failure_is_reported_not_panicked() {
    let config = SupervisorConfig {
        program: "definitely-not-a-real-program-agentgpt".to_string(),
        args: Vec::new(),
        cwd: PathBuf::from("."),
        idle_timeout: Duration::from_secs(600),
        request_timeout: Duration::from_secs(1),
    };
    let supervisor = Supervisor::new(config);
    let err = supervisor
        .request(Envelope::new("runtime.health", serde_json::json!({})))
        .await
        .expect_err("spawn must fail");
    assert_eq!(err.code(), "sidecar_spawn_failed");
}
