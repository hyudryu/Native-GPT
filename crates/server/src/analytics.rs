//! Local model usage analytics derived from persisted chat runs.

use std::collections::BTreeMap;

use axum::extract::State;
use axum::Json;
use serde::Serialize;
use serde_json::{json, Value};

use crate::error::ApiError;
use crate::state::SharedState;

#[derive(Debug, Default)]
struct Aggregate {
    runs: u64,
    successful_runs: u64,
    input_tokens: u64,
    output_tokens: u64,
    total_tokens: u64,
    latency_ms: f64,
    run_duration_ms: f64,
    duration_samples: u64,
}

#[derive(Debug, Serialize)]
pub struct ModelAnalytics {
    provider_name: String,
    model_id: String,
    runs: u64,
    successful_runs: u64,
    input_tokens: u64,
    output_tokens: u64,
    total_tokens: u64,
    average_tokens_per_second: f64,
    average_run_duration_ms: f64,
}

fn usage_number(usage: &Value, snake: &str, camel: &str) -> f64 {
    usage
        .get(snake)
        .or_else(|| usage.get(camel))
        .and_then(Value::as_f64)
        .unwrap_or(0.0)
}

pub async fn models(State(state): State<SharedState>) -> Result<Json<Value>, ApiError> {
    let runs = state.db.analytics_runs().await?;
    let mut grouped: BTreeMap<(String, String), Aggregate> = BTreeMap::new();
    for run in runs {
        let aggregate = grouped
            .entry((run.provider_name, run.model_id))
            .or_default();
        aggregate.runs += 1;
        if run.status == "completed" {
            aggregate.successful_runs += 1;
        }
        if let Some(raw) = run.usage_json.as_deref() {
            if let Ok(usage) = serde_json::from_str::<Value>(raw) {
                aggregate.input_tokens +=
                    usage_number(&usage, "input_tokens", "inputTokens") as u64;
                aggregate.output_tokens +=
                    usage_number(&usage, "output_tokens", "outputTokens") as u64;
                aggregate.total_tokens +=
                    usage_number(&usage, "total_tokens", "totalTokens") as u64;
                aggregate.latency_ms += usage_number(&usage, "latency_ms", "latencyMs");
            }
        }
        if let Some(completed_at) = run.completed_at.as_deref() {
            if let (Ok(start), Ok(end)) = (
                chrono::DateTime::parse_from_rfc3339(&run.started_at),
                chrono::DateTime::parse_from_rfc3339(completed_at),
            ) {
                aggregate.run_duration_ms += (end - start).num_milliseconds().max(0) as f64;
                aggregate.duration_samples += 1;
            }
        }
    }

    let mut totals = Aggregate::default();
    let models = grouped
        .into_iter()
        .map(|((provider_name, model_id), aggregate)| {
            totals.runs += aggregate.runs;
            totals.successful_runs += aggregate.successful_runs;
            totals.input_tokens += aggregate.input_tokens;
            totals.output_tokens += aggregate.output_tokens;
            totals.total_tokens += aggregate.total_tokens;
            totals.latency_ms += aggregate.latency_ms;
            totals.run_duration_ms += aggregate.run_duration_ms;
            totals.duration_samples += aggregate.duration_samples;
            ModelAnalytics {
                provider_name,
                model_id,
                runs: aggregate.runs,
                successful_runs: aggregate.successful_runs,
                input_tokens: aggregate.input_tokens,
                output_tokens: aggregate.output_tokens,
                total_tokens: aggregate.total_tokens,
                average_tokens_per_second: if aggregate.latency_ms > 0.0 {
                    aggregate.output_tokens as f64 / (aggregate.latency_ms / 1000.0)
                } else {
                    0.0
                },
                average_run_duration_ms: if aggregate.duration_samples > 0 {
                    aggregate.run_duration_ms / aggregate.duration_samples as f64
                } else {
                    0.0
                },
            }
        })
        .collect::<Vec<_>>();

    let total_tps = if totals.latency_ms > 0.0 {
        totals.output_tokens as f64 / (totals.latency_ms / 1000.0)
    } else {
        0.0
    };
    Ok(Json(json!({
        "totals": {
            "runs": totals.runs,
            "successful_runs": totals.successful_runs,
            "input_tokens": totals.input_tokens,
            "output_tokens": totals.output_tokens,
            "total_tokens": totals.total_tokens,
            "average_tokens_per_second": total_tps,
        },
        "models": models,
    })))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn usage_accepts_strands_and_normalized_names() {
        let strands = json!({"inputTokens": 12});
        let normalized = json!({"input_tokens": 14});
        assert_eq!(usage_number(&strands, "input_tokens", "inputTokens"), 12.0);
        assert_eq!(
            usage_number(&normalized, "input_tokens", "inputTokens"),
            14.0
        );
    }
}
