//! Bearer-token auth for non-localhost requests (ADR-0003).
//!
//! Loopback clients are always allowed. Everyone else must present the
//! pairing token as `Authorization: Bearer <token>` or `?token=<token>`.
//! PWA install assets and `/api/health` are exempt so iOS can install the
//! app. The token is never logged.

use std::net::SocketAddr;
use std::sync::Arc;

use axum::extract::{ConnectInfo, Request, State};
use axum::http::{header, StatusCode};
use axum::middleware::Next;
use axum::response::{IntoResponse, Response};

use crate::state::AppState;

/// Paths that never require a token, even off-localhost.
const EXEMPT_EXACT: &[&str] = &[
    "/api/health",
    "/manifest.webmanifest",
    "/sw.js",
    "/registerSW.js",
];

/// Path prefixes that never require a token (PWA icons etc.).
const EXEMPT_PREFIXES: &[&str] = &["/icons/", "/favicon", "/apple-touch-icon"];

pub fn is_exempt_path(path: &str) -> bool {
    EXEMPT_EXACT.contains(&path) || EXEMPT_PREFIXES.iter().any(|p| path.starts_with(p))
}

/// Constant-time string comparison to avoid token timing leaks.
fn token_eq(a: &str, b: &str) -> bool {
    let (a, b) = (a.as_bytes(), b.as_bytes());
    if a.len() != b.len() {
        return false;
    }
    a.iter().zip(b).fold(0u8, |acc, (x, y)| acc | (x ^ y)) == 0
}

/// Extract the `token` query parameter without logging anything.
fn query_token(uri: &axum::http::Uri) -> Option<String> {
    uri.query()?.split('&').find_map(|pair| {
        let (key, value) = pair.split_once('=')?;
        (key == "token").then(|| value.to_string())
    })
}

/// Pure authorization check (unit-tested directly).
pub fn is_authorized(auth_header: Option<&str>, query_token: Option<&str>, expected: &str) -> bool {
    if let Some(bearer) = auth_header.and_then(|h| h.strip_prefix("Bearer ")) {
        if token_eq(bearer, expected) {
            return true;
        }
    }
    if let Some(token) = query_token {
        if token_eq(token, expected) {
            return true;
        }
    }
    false
}

pub async fn auth_middleware(
    State(state): State<Arc<AppState>>,
    ConnectInfo(peer): ConnectInfo<SocketAddr>,
    req: Request,
    next: Next,
) -> Response {
    if is_exempt_path(req.uri().path()) || peer.ip().is_loopback() {
        return next.run(req).await;
    }
    let auth_header = req
        .headers()
        .get(header::AUTHORIZATION)
        .and_then(|v| v.to_str().ok());
    let token_param = query_token(req.uri());
    if is_authorized(auth_header, token_param.as_deref(), &state.token) {
        next.run(req).await
    } else {
        (
            StatusCode::UNAUTHORIZED,
            axum::Json(serde_json::json!({"error": "unauthorized"})),
        )
            .into_response()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use axum::body::Body;
    use axum::routing::get;
    use axum::Router;
    use tower::ServiceExt;

    fn app(token: &str) -> Router {
        let test_state = crate::state::test_state(token);
        let state = test_state.state.clone();
        // Keep the rig alive for the router's lifetime by leaking it; auth
        // tests are short-lived and this avoids tempdir cleanup races.
        std::mem::forget(test_state);
        Router::new()
            .route("/protected", get(|| async { "secret" }))
            .route("/api/health", get(|| async { "health" }))
            .route("/sw.js", get(|| async { "sw" }))
            .route("/icons/icon-192.png", get(|| async { "icon" }))
            .layer(axum::middleware::from_fn_with_state(
                state.clone(),
                auth_middleware,
            ))
            .with_state(state)
    }

    fn request(path: &str, peer: [u8; 4], auth: Option<&str>) -> Request {
        let mut builder = Request::builder().uri(path);
        if let Some(auth) = auth {
            builder = builder.header(header::AUTHORIZATION, auth);
        }
        let mut req = builder.body(Body::empty()).unwrap();
        req.extensions_mut()
            .insert(ConnectInfo(SocketAddr::from((peer, 40_000))));
        req
    }

    const LAN: [u8; 4] = [192, 168, 1, 50];
    const LOOPBACK: [u8; 4] = [127, 0, 0, 1];

    #[tokio::test]
    async fn loopback_is_always_allowed() {
        let res = app("tok")
            .oneshot(request("/protected", LOOPBACK, None))
            .await
            .unwrap();
        assert_eq!(res.status(), StatusCode::OK);
    }

    #[tokio::test]
    async fn non_localhost_without_token_is_rejected() {
        let res = app("tok")
            .oneshot(request("/protected", LAN, None))
            .await
            .unwrap();
        assert_eq!(res.status(), StatusCode::UNAUTHORIZED);
    }

    #[tokio::test]
    async fn wrong_token_is_rejected() {
        let res = app("tok")
            .oneshot(request("/protected", LAN, Some("Bearer nope")))
            .await
            .unwrap();
        assert_eq!(res.status(), StatusCode::UNAUTHORIZED);
    }

    #[tokio::test]
    async fn bearer_token_is_accepted() {
        let res = app("tok")
            .oneshot(request("/protected", LAN, Some("Bearer tok")))
            .await
            .unwrap();
        assert_eq!(res.status(), StatusCode::OK);
    }

    #[tokio::test]
    async fn query_token_is_accepted() {
        let res = app("tok")
            .oneshot(request("/protected?token=tok", LAN, None))
            .await
            .unwrap();
        assert_eq!(res.status(), StatusCode::OK);
    }

    #[tokio::test]
    async fn exempt_paths_skip_auth() {
        for path in ["/api/health", "/sw.js", "/icons/icon-192.png"] {
            let res = app("tok").oneshot(request(path, LAN, None)).await.unwrap();
            assert_eq!(res.status(), StatusCode::OK, "path {path}");
        }
    }

    #[test]
    fn exempt_path_matching() {
        assert!(is_exempt_path("/api/health"));
        assert!(is_exempt_path("/manifest.webmanifest"));
        assert!(is_exempt_path("/registerSW.js"));
        assert!(is_exempt_path("/favicon.ico"));
        assert!(is_exempt_path("/apple-touch-icon.png"));
        assert!(is_exempt_path("/icons/icon-512.png"));
        assert!(!is_exempt_path("/api/pair"));
        assert!(!is_exempt_path("/ws"));
        assert!(!is_exempt_path("/index.html"));
    }

    #[test]
    fn pure_authorization_logic() {
        assert!(is_authorized(Some("Bearer t"), None, "t"));
        assert!(is_authorized(None, Some("t"), "t"));
        assert!(!is_authorized(Some("Bearer x"), None, "t"));
        assert!(!is_authorized(Some("bearer t"), None, "t")); // scheme is case-sensitive
        assert!(!is_authorized(None, None, "t"));
        assert!(!is_authorized(None, Some(""), "t"));
    }
}
