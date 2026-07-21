//! Embedded axum HTTP + WebSocket server for the AgentGPT host (ADR-0001).
//!
//! Serves the React UI from `apps/ui/dist` (with an SPA fallback and a
//! placeholder page when the UI is not built), exposes `/api/health`,
//! `/api/pair` and `/ws`, binds localhost plus Tailscale interfaces only
//! (ADR-0003), and relays protocol envelopes to the Python sidecar.

pub mod auth;
pub mod db;
pub mod error;
pub mod net;
pub mod protocol;
pub mod relay;
pub mod secrets;
pub mod state;
pub mod ws;

mod analytics;
mod chat;
mod endpoints;
mod handlers;
mod knowledge;
mod phase3;
mod tools;
mod updates;

use std::net::{Ipv4Addr, SocketAddr};
use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::time::{Duration, Instant};

use agentgpt_supervisor::{Supervisor, SupervisorConfig};
use agentgpt_telemetry::Telemetry;
use anyhow::Context;
use axum::extract::Request;
use axum::http::{header, HeaderValue, StatusCode};
use axum::middleware::{self, Next};
use axum::response::{Html, IntoResponse, Response};
use axum::routing::{delete, get, patch, post};
use axum::Router;
use state::{AppState, SharedState};
use tokio::net::TcpListener;
use tower::ServiceExt;
use tower_http::services::{ServeDir, ServeFile};
use tracing::{info, warn};

/// Strict CSP applied to HTML responses (ADR-0003).
const CSP: &str = "default-src 'self'; connect-src 'self' ws: http:; img-src 'self' data:; style-src 'self' 'unsafe-inline'";

/// Served when `apps/ui/dist` does not exist (UI not built yet).
const PLACEHOLDER_HTML: &str = "<!doctype html><html><head><meta charset=\"utf-8\">\
<title>AgentGPT</title></head><body style=\"font-family:system-ui,sans-serif;margin:3rem\">\
<h1>AgentGPT host is running</h1>\
<p>The UI has not been built yet (<code>apps/ui/dist</code> is missing). \
Build the UI package and reload this page.</p></body></html>";

/// Server configuration (CLI args resolved by the host binary).
#[derive(Debug, Clone)]
pub struct ServerConfig {
    /// TCP port; 0 = OS-assigned high port.
    pub port: u16,
    /// Bind 0.0.0.0 (explicit opt-in, logged as a warning).
    pub bind_all: bool,
    /// Repo root override; falls back to `AGENTGPT_REPO_ROOT`, then to
    /// walking up from `CARGO_MANIFEST_DIR`.
    pub repo_root: Option<PathBuf>,
    /// Auth token override; falls back to `AGENTGPT_TOKEN`, then a
    /// freshly generated token.
    pub token: Option<String>,
    /// Sidecar idle timeout (ADR-0004).
    pub idle_timeout: Duration,
}

impl Default for ServerConfig {
    fn default() -> Self {
        Self {
            port: 0,
            bind_all: false,
            repo_root: None,
            token: None,
            idle_timeout: agentgpt_supervisor::DEFAULT_IDLE_TIMEOUT,
        }
    }
}

/// A bound server ready to serve. `port` is the effective port.
pub struct BoundServer {
    listeners: Vec<TcpListener>,
    state: SharedState,
    /// Effective bound port.
    pub port: u16,
    /// `http://127.0.0.1:<port>`
    pub local_url: String,
    /// `http://<tailscale-ip>:<port>` for each bound Tailscale interface.
    pub tailscale_urls: Vec<String>,
}

/// Resolve the repo root: explicit override, `AGENTGPT_REPO_ROOT`, or the
/// nearest ancestor of this crate's manifest dir containing a workspace
/// `Cargo.toml`.
pub fn resolve_repo_root(override_root: Option<&Path>) -> PathBuf {
    if let Some(root) = override_root {
        return root.to_path_buf();
    }
    if let Ok(env_root) = std::env::var("AGENTGPT_REPO_ROOT") {
        if !env_root.trim().is_empty() {
            return PathBuf::from(env_root);
        }
    }
    let manifest_dir = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
    for ancestor in manifest_dir.ancestors() {
        let candidate = ancestor.join("Cargo.toml");
        if candidate.is_file()
            && std::fs::read_to_string(&candidate)
                .map(|c| c.contains("[workspace]"))
                .unwrap_or(false)
        {
            return ancestor.to_path_buf();
        }
    }
    manifest_dir
}

/// Generate a 32-byte random token, hex-encoded.
fn generate_token() -> String {
    use rand::RngCore;
    let mut bytes = [0u8; 32];
    rand::rng().fill_bytes(&mut bytes);
    let mut hex = String::with_capacity(64);
    for b in bytes {
        hex.push_str(&format!("{b:02x}"));
    }
    hex
}

/// Resolve the auth token: config, `AGENTGPT_TOKEN`, or generate one.
/// The generated token is printed once (it never appears in request logs).
fn resolve_token(config_token: Option<String>) -> String {
    if let Some(token) = config_token.filter(|t| !t.is_empty()) {
        return token;
    }
    if let Ok(token) = std::env::var("AGENTGPT_TOKEN") {
        if !token.is_empty() {
            return token;
        }
    }
    let token = generate_token();
    // Printed exactly once at startup so the user can pair a device before
    // keychain persistence exists. Never logged per-request.
    eprintln!("[agentgpt] generated pairing token (shown once): {token}");
    token
}

/// Bind listeners, build state, print the effective URLs.
pub async fn bind(config: ServerConfig) -> anyhow::Result<BoundServer> {
    let repo_root = resolve_repo_root(config.repo_root.as_deref());
    let token = resolve_token(config.token);
    let tailscale_ips = if config.bind_all {
        Vec::new()
    } else {
        net::tailscale_ipv4_addrs()
    };

    let mut listeners: Vec<TcpListener> = Vec::new();
    let port;
    if config.bind_all {
        warn!("--bind-all: listening on 0.0.0.0; reachable beyond the tailnet");
        let listener = TcpListener::bind((Ipv4Addr::UNSPECIFIED, config.port))
            .await
            .context("bind 0.0.0.0")?;
        port = listener.local_addr()?.port();
        listeners.push(listener);
    } else {
        // Bind localhost first so port 0 yields one consistent port for all
        // subsequent Tailscale-interface binds.
        let listener = TcpListener::bind((Ipv4Addr::LOCALHOST, config.port))
            .await
            .context("bind 127.0.0.1")?;
        port = listener.local_addr()?.port();
        listeners.push(listener);
        for ip in &tailscale_ips {
            match TcpListener::bind((*ip, port)).await {
                Ok(listener) => listeners.push(listener),
                Err(e) => warn!("failed to bind tailscale interface {ip}:{port}: {e}"),
            }
        }
    }

    let supervisor = Supervisor::new(SupervisorConfig::from_env(
        repo_root.clone(),
        config.idle_timeout,
    ));
    supervisor.start_idle_watchdog();

    let db_path = db::default_path(&repo_root);
    let db = db::Db::open(&db_path)
        .with_context(|| format!("failed to open database at {}", db_path.display()))?;
    let interrupted = db
        .interrupt_running_runs()
        .await
        .context("failed to recover interrupted chat runs")?;
    if interrupted > 0 {
        warn!(interrupted, "marked unfinished chat runs as interrupted");
    }
    info!(path = %db_path.display(), "database ready");

    let state = Arc::new(AppState {
        token,
        port,
        started: Instant::now(),
        tailscale_ips: tailscale_ips.clone(),
        ui_dist: repo_root.join("apps/ui/dist"),
        repo_root: repo_root.clone(),
        supervisor,
        telemetry: Telemetry::new(),
        db,
        secrets: Arc::new(agentgpt_secure_store::SecureStore::new("agentgpt")),
    });

    let local_url = format!("http://127.0.0.1:{port}");
    let tailscale_urls: Vec<String> = tailscale_ips
        .iter()
        .map(|ip| format!("http://{ip}:{port}"))
        .collect();
    info!(%local_url, "agentgpt host listening");
    for url in &tailscale_urls {
        info!(%url, "reachable via tailscale");
    }
    println!("AgentGPT host: {local_url}");
    for url in &tailscale_urls {
        println!("Tailscale URL:  {url}");
    }

    Ok(BoundServer {
        listeners,
        state,
        port,
        local_url,
        tailscale_urls,
    })
}

/// Bind and serve until Ctrl-C, then shut the sidecar down gracefully.
pub async fn run(config: ServerConfig) -> anyhow::Result<()> {
    bind(config).await?.wait().await
}

impl BoundServer {
    /// Serve all listeners until Ctrl-C; then graceful sidecar shutdown.
    pub async fn wait(self) -> anyhow::Result<()> {
        let app = build_router(self.state.clone());
        for listener in self.listeners {
            let app = app.clone();
            tokio::spawn(async move {
                if let Err(e) = axum::serve(
                    listener,
                    app.into_make_service_with_connect_info::<SocketAddr>(),
                )
                .await
                {
                    tracing::error!("http server error: {e}");
                }
            });
        }
        tokio::signal::ctrl_c().await.ok();
        info!("shutting down");
        self.state.supervisor.shutdown().await;
        Ok(())
    }

    /// Shared state (e.g. for tests that drive the router directly).
    pub fn state(&self) -> SharedState {
        self.state.clone()
    }
}

/// Build the axum router: API routes, static UI with SPA fallback, auth and
/// security-header middleware.
pub fn build_router(state: SharedState) -> Router {
    // Static service for `apps/ui/dist`: files when present, SPA fallback to
    // `index.html`, placeholder page when the UI is not built. Built inline
    // so the compiler sees the concrete service type (its Future must be
    // provably Send for `fallback_service`).
    let index = state.ui_dist.join("index.html");
    let spa_or_placeholder = tower::service_fn(move |req: Request| {
        let index = index.clone();
        async move {
            if index.is_file() {
                match ServeFile::new(&index).oneshot(req).await {
                    Ok(res) => Ok(res.into_response()),
                    Err(e) => Ok((
                        StatusCode::INTERNAL_SERVER_ERROR,
                        format!("failed to serve index.html: {e}"),
                    )
                        .into_response()),
                }
            } else {
                Ok(Html(PLACEHOLDER_HTML).into_response())
            }
        }
    });
    let static_service = ServeDir::new(&state.ui_dist)
        .append_index_html_on_directories(true)
        .fallback(spa_or_placeholder);

    Router::new()
        .route("/api/health", get(handlers::health))
        .route("/api/pair", get(handlers::pair))
        .route(
            "/api/endpoints",
            get(endpoints::list_endpoints).post(endpoints::create_endpoint),
        )
        .route(
            "/api/endpoints/{id}",
            patch(endpoints::patch_endpoint).delete(endpoints::delete_endpoint),
        )
        .route("/api/endpoints/{id}/test", post(endpoints::test_endpoint))
        .route(
            "/api/endpoints/{id}/models",
            get(endpoints::list_models).post(endpoints::add_model),
        )
        .route(
            "/api/endpoints/{id}/models/{model_id}",
            patch(endpoints::patch_model),
        )
        .route(
            "/api/projects",
            get(phase3::list_projects).post(phase3::create_project),
        )
        .route(
            "/api/projects/{id}",
            get(phase3::get_project)
                .patch(phase3::patch_project)
                .delete(phase3::delete_project),
        )
        .route(
            "/api/conversations",
            get(phase3::list_conversations).post(phase3::create_conversation),
        )
        .route(
            "/api/conversations/{id}",
            get(phase3::get_conversation)
                .patch(phase3::patch_conversation)
                .delete(phase3::delete_conversation),
        )
        .route(
            "/api/conversations/{id}/messages",
            get(phase3::list_messages).post(chat::send_message),
        )
        .route("/api/runs/{id}/cancel", post(chat::cancel_run))
        .route("/api/search", get(phase3::search))
        .route("/api/models", get(phase3::list_models))
        .route(
            "/api/knowledge",
            get(knowledge::list_sources).post(knowledge::ingest),
        )
        .route("/api/knowledge/search", get(knowledge::search))
        .route("/api/knowledge/{id}", delete(knowledge::delete_source))
        .route("/api/analytics/models", get(analytics::models))
        .route("/api/tools", get(tools::list))
        .route("/api/tools/{id}", patch(tools::patch))
        .route("/api/updates/check", get(updates::check))
        .route("/ws", get(ws::ws_handler))
        .fallback_service(static_service)
        .layer(middleware::from_fn(cache_headers))
        .layer(middleware::from_fn(security_headers))
        .layer(middleware::from_fn_with_state(
            state.clone(),
            auth::auth_middleware,
        ))
        .with_state(state)
}

/// Security headers on every response; strict CSP on HTML only (ADR-0003).
/// Entry-point documents must always be revalidated so a fresh deploy (or a
/// service-worker update) is picked up immediately; hashed assets under
/// /assets/ are immutable and may be cached forever.
async fn cache_headers(req: Request, next: Next) -> Response {
    let path = req.uri().path().to_owned();
    let mut res = next.run(req).await;
    let revalidate = path == "/"
        || path.ends_with(".html")
        || path == "/sw.js"
        || path == "/registerSW.js"
        || path == "/manifest.webmanifest";
    let headers = res.headers_mut();
    if revalidate {
        headers.insert(header::CACHE_CONTROL, HeaderValue::from_static("no-cache"));
    } else if path.starts_with("/assets/") || path.starts_with("/icons/") {
        headers.insert(
            header::CACHE_CONTROL,
            HeaderValue::from_static("public, max-age=31536000, immutable"),
        );
    }
    res
}

async fn security_headers(req: Request, next: Next) -> Response {
    let mut res = next.run(req).await;
    let is_html = res
        .headers()
        .get(header::CONTENT_TYPE)
        .and_then(|v| v.to_str().ok())
        .is_some_and(|ct| ct.contains("text/html"));
    let headers = res.headers_mut();
    headers.insert(
        header::X_CONTENT_TYPE_OPTIONS,
        HeaderValue::from_static("nosniff"),
    );
    headers.insert(
        header::REFERRER_POLICY,
        HeaderValue::from_static("no-referrer"),
    );
    headers.insert(header::X_FRAME_OPTIONS, HeaderValue::from_static("DENY"));
    if is_html {
        headers.insert(
            header::CONTENT_SECURITY_POLICY,
            HeaderValue::from_static(CSP),
        );
    }
    res
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn repo_root_resolves_to_workspace() {
        let root = resolve_repo_root(None);
        assert!(root.join("Cargo.toml").is_file());
        assert!(root.join("crates/server").is_dir());
    }

    #[test]
    fn repo_root_override_wins() {
        let root = resolve_repo_root(Some(Path::new("/tmp/whatever")));
        assert_eq!(root, PathBuf::from("/tmp/whatever"));
    }

    #[test]
    fn generated_token_is_64_hex_chars() {
        let token = generate_token();
        assert_eq!(token.len(), 64);
        assert!(token.chars().all(|c| c.is_ascii_hexdigit()));
    }
}
