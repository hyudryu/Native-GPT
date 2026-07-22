//! Test double for the Python sidecar: echoes NDJSON requests back as
//! `<type>.ok` responses with the same `request_id` and kind-specific
//! payloads. Used by supervisor/server integration tests; not shipped.

use std::io::{BufRead, Write};

use agentgpt_supervisor::protocol::Envelope;

fn main() {
    let stdin = std::io::stdin();
    let stdout = std::io::stdout();
    for line in stdin.lock().lines() {
        let Ok(line) = line else { break };
        let Ok(env) = serde_json::from_str::<Envelope>(&line) else {
            continue;
        };
        if env.kind == "runtime.shutdown" {
            // Exit without replying, like a cooperative sidecar would.
            std::process::exit(0);
        }
        let payload = match env.kind.as_str() {
            "endpoint.test" => serde_json::json!({
                "ok": true,
                "latency_ms": 2.5,
                "server": "fake-sidecar"
            }),
            "models.list" => serde_json::json!({
                "models": [
                    {"id": "fake-model-1", "raw": {"owned_by": "fake"}},
                    {"id": "fake-model-2"}
                ],
                "fetched_at": chrono::Utc::now().to_rfc3339()
            }),
            "run.start" => serde_json::json!({
                "run_id": env.payload["run_id"],
                "conversation_id": env.payload["conversation_id"]
            }),
            "run.cancel" => serde_json::json!({
                "run_id": env.payload["run_id"]
            }),
            _ => serde_json::json!({"status": "ok", "uptime_seconds": 1.0, "rss_bytes": 1024}),
        };
        let mut resp = Envelope::new(format!("{}.ok", env.kind), payload);
        resp.request_id = env.request_id.clone();
        let mut out = stdout.lock();
        if writeln!(out, "{}", serde_json::to_string(&resp).unwrap()).is_err() {
            break;
        }
        let _ = out.flush();
        if env.kind == "run.start" {
            if env.payload["prompt"] == "trigger-crash" {
                // Crash mid-stream: one delta, then die without a terminal
                // event (exercises M1 synthetic run.failed).
                let mut event = Envelope::new(
                    "run.text_delta",
                    serde_json::json!({"run_id": env.payload["run_id"], "text": "partial"}),
                );
                event.request_id = env.request_id.clone();
                let _ = writeln!(out, "{}", serde_json::to_string(&event).unwrap());
                let _ = out.flush();
                std::process::exit(2);
            }
            for (kind, payload) in [
                (
                    "run.text_delta",
                    serde_json::json!({"run_id": env.payload["run_id"], "text": "fake reply"}),
                ),
                (
                    "run.completed",
                    serde_json::json!({
                        "run_id": env.payload["run_id"],
                        "usage": {
                            "input_tokens": 10,
                            "output_tokens": 5,
                            "total_tokens": 15,
                            "latency_ms": 250.0,
                            "tokens_per_second": 20.0
                        }
                    }),
                ),
            ] {
                let mut event = Envelope::new(kind, payload);
                event.request_id = env.request_id.clone();
                let _ = writeln!(out, "{}", serde_json::to_string(&event).unwrap());
            }
            let _ = out.flush();
        }
        if env.kind == "test.stream" {
            // Stream events for ~1s without any new request (exercises M2:
            // stdout activity must refresh the idle timer).
            for _ in 0..8 {
                std::thread::sleep(std::time::Duration::from_millis(120));
                let mut event = Envelope::new(
                    "run.text_delta",
                    serde_json::json!({"run_id": "stream", "text": "tick"}),
                );
                event.request_id = env.request_id.clone();
                if writeln!(out, "{}", serde_json::to_string(&event).unwrap()).is_err() {
                    break;
                }
                let _ = out.flush();
            }
        }
    }
}
