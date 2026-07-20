//! HTTP handlers: `/api/health`, `/api/pair`.

use axum::extract::State;
use axum::http::StatusCode;
use axum::Json;
use serde_json::{json, Value};

use crate::state::SharedState;

/// `GET /api/health` — unauthenticated liveness/telemetry probe.
pub async fn health(State(state): State<SharedState>) -> Json<Value> {
    let tailscale_urls: Vec<String> = state
        .tailscale_ips
        .iter()
        .map(|ip| format!("http://{ip}:{}", state.port))
        .collect();
    Json(json!({
        "status": "ok",
        "host_rss_bytes": state.telemetry.host_rss_bytes(),
        "sidecar_rss_bytes": state.supervisor.rss_bytes(),
        "sidecar_state": state.supervisor.state().as_str(),
        "uptime_seconds": state.started.elapsed().as_secs(),
        "port": state.port,
        "tailscale_urls": tailscale_urls,
    }))
}

/// `GET /api/pair` — pairing URL + QR code (SVG) for mobile devices.
///
/// This is the ONLY endpoint that ever returns the token; it is meant for
/// the desktop (loopback) user to scan with their phone.
pub async fn pair(State(state): State<SharedState>) -> Result<Json<Value>, (StatusCode, String)> {
    let host = state
        .tailscale_ips
        .first()
        .map(ToString::to_string)
        .unwrap_or_else(|| "127.0.0.1".to_string());
    let url = format!("http://{host}:{}/?token={}", state.port, state.token);
    let code = qrcode::QrCode::new(url.as_bytes()).map_err(|e| {
        (
            StatusCode::INTERNAL_SERVER_ERROR,
            format!("QR encode failed: {e}"),
        )
    })?;
    let svg = code
        .render::<qrcode::render::svg::Color>()
        .min_dimensions(256, 256)
        .build();
    Ok(Json(json!({
        "url": url,
        "token_qr_svg": svg,
    })))
}
