//! Typed relays from REST handlers to the Python sidecar via the supervisor.
//!
//! Builds `endpoint.test` / `models.list` request envelopes, correlates the
//! response, and maps supervisor failures and protocol `error` envelopes to
//! REST errors. The raw API key flows into the NDJSON payload only — it is
//! never logged here.

use agentgpt_supervisor::{Supervisor, SupervisorError};

use crate::error::ApiError;
use crate::protocol::{
    EndpointTest, EndpointTestOk, Envelope, ModelInfo, ModelsList, ModelsListOk, ProtocolError,
    RunCancel, RunCancelled, RunStart, RunStarted, RunSynthesizeNow, RunSynthesizeNowOk,
};

/// Map a supervisor transport error to a 503 REST error.
fn supervisor_error(e: &SupervisorError) -> ApiError {
    ApiError::sidecar_unavailable(e.code(), e.to_string())
}

/// Map a protocol `error` envelope from the sidecar to a REST error.
fn protocol_error(env: &Envelope) -> ApiError {
    let err: ProtocolError = serde_json::from_value(env.payload.clone()).unwrap_or(ProtocolError {
        code: "sidecar_error".to_string(),
        message: "sidecar returned an error".to_string(),
        retryable: None,
    });
    match err.code.as_str() {
        "sidecar_unavailable" | "sidecar_crashed" | "sidecar_spawn_failed" | "request_timeout" => {
            ApiError::sidecar_unavailable(err.code, err.message)
        }
        _ => ApiError::bad_gateway(err.code, err.message),
    }
}

/// Send a request envelope and decode the typed `.ok` payload.
async fn relay<T: serde::de::DeserializeOwned>(
    supervisor: &Supervisor,
    env: Envelope,
) -> Result<T, ApiError> {
    let resp = supervisor
        .request(env)
        .await
        .map_err(|e| supervisor_error(&e))?;
    if resp.kind == "error" {
        return Err(protocol_error(&resp));
    }
    serde_json::from_value(resp.payload).map_err(|e| {
        ApiError::bad_gateway(
            "bad_sidecar_response",
            format!("sidecar response did not match the expected payload: {e}"),
        )
    })
}

/// `endpoint.test` round-trip.
pub async fn endpoint_test(
    supervisor: &Supervisor,
    base_url: &str,
    api_key: Option<String>,
    timeout_seconds: u32,
    tls_verify: bool,
) -> Result<EndpointTestOk, ApiError> {
    let payload = EndpointTest {
        base_url: base_url.to_string(),
        api_key,
        timeout_seconds: Some(timeout_seconds),
        tls_verify: Some(tls_verify),
    };
    let env = Envelope::new(
        "endpoint.test",
        serde_json::to_value(payload).expect("EndpointTest serializes"),
    );
    relay(supervisor, env).await
}

/// `models.list` round-trip.
pub async fn models_list(
    supervisor: &Supervisor,
    base_url: &str,
    api_key: Option<String>,
    model_list_path: Option<String>,
    timeout_seconds: u32,
    tls_verify: bool,
) -> Result<ModelsListOk, ApiError> {
    let payload = ModelsList {
        base_url: base_url.to_string(),
        api_key,
        model_list_path,
        timeout_seconds: Some(timeout_seconds),
        tls_verify: Some(tls_verify),
    };
    let env = Envelope::new(
        "models.list",
        serde_json::to_value(payload).expect("ModelsList serializes"),
    );
    relay(supervisor, env).await
}

/// Convenience: convert fetched [`ModelInfo`]s into `(remote_id, raw_json)`
/// pairs for `Db::replace_discovered_models`.
pub fn models_for_upsert(models: &[ModelInfo]) -> Vec<(String, Option<String>)> {
    models
        .iter()
        .map(|m| {
            (
                m.id.clone(),
                m.raw
                    .as_ref()
                    .map(|raw| serde_json::to_string(raw).unwrap_or_default()),
            )
        })
        .collect()
}

/// Start a streaming run. The acknowledgement is correlated; subsequent
/// deltas and the terminal event are broadcast by the supervisor.
pub async fn run_start(
    supervisor: &Supervisor,
    payload: RunStart,
) -> Result<(String, RunStarted), ApiError> {
    let env = Envelope::new(
        "run.start",
        serde_json::to_value(payload).expect("RunStart serializes"),
    );
    let request_id = env.request_id.clone();
    let started = relay(supervisor, env).await?;
    Ok((request_id, started))
}

pub async fn run_cancel(supervisor: &Supervisor, run_id: String) -> Result<RunCancelled, ApiError> {
    let env = Envelope::new(
        "run.cancel",
        serde_json::to_value(RunCancel { run_id }).expect("RunCancel serializes"),
    );
    relay(supervisor, env).await
}

/// `run.synthesize_now`: ask a max-mode run to stop investigating and
/// synthesize its partial results. Mirrors the run.cancel relay.
pub async fn run_synthesize_now(
    supervisor: &Supervisor,
    run_id: String,
) -> Result<RunSynthesizeNowOk, ApiError> {
    let env = Envelope::new(
        "run.synthesize_now",
        serde_json::to_value(RunSynthesizeNow { run_id }).expect("RunSynthesizeNow serializes"),
    );
    relay(supervisor, env).await
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn protocol_error_envelope_maps_to_bad_gateway() {
        let env = Envelope::error("req", "connection_error", "refused", true);
        let err = protocol_error(&env);
        assert_eq!(err.status, axum::http::StatusCode::BAD_GATEWAY);
        assert_eq!(err.code, "connection_error");
    }

    #[test]
    fn crashed_sidecar_maps_to_service_unavailable() {
        let env = Envelope::error("req", "sidecar_crashed", "exited", true);
        let err = protocol_error(&env);
        assert_eq!(err.status, axum::http::StatusCode::SERVICE_UNAVAILABLE);
    }
}
