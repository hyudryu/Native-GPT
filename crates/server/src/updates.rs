//! GitHub release update checks. Installation remains user-controlled.

use std::time::Duration;

use axum::Json;
use serde::Deserialize;
use serde_json::{json, Value};

use crate::error::ApiError;

const RELEASES_URL: &str = "https://api.github.com/repos/hyudryu/Native-GPT/releases/latest";
const REPOSITORY_URL: &str = "https://github.com/hyudryu/Native-GPT";

#[derive(Debug, Deserialize)]
struct GitHubRelease {
    tag_name: String,
    html_url: String,
    #[serde(default)]
    name: Option<String>,
    #[serde(default)]
    body: Option<String>,
    #[serde(default)]
    published_at: Option<String>,
    #[serde(default)]
    draft: bool,
    #[serde(default)]
    prerelease: bool,
}

fn version_tuple(value: &str) -> Option<(u64, u64, u64)> {
    let normalized = value.trim().trim_start_matches(['v', 'V']);
    let core = normalized.split(['-', '+']).next()?;
    let mut parts = core.split('.');
    Some((
        parts.next()?.parse().ok()?,
        parts.next().unwrap_or("0").parse().ok()?,
        parts.next().unwrap_or("0").parse().ok()?,
    ))
}

fn is_newer(current: &str, latest: &str) -> bool {
    match (version_tuple(current), version_tuple(latest)) {
        (Some(current), Some(latest)) => latest > current,
        _ => latest.trim_start_matches(['v', 'V']) != current.trim_start_matches(['v', 'V']),
    }
}

pub async fn check() -> Result<Json<Value>, ApiError> {
    let current = env!("CARGO_PKG_VERSION");
    let client = reqwest::Client::builder()
        .timeout(Duration::from_secs(12))
        .user_agent("Native-GPT update checker")
        .build()
        .map_err(|error| ApiError::internal(error.to_string()))?;
    let response = client
        .get(RELEASES_URL)
        .header(reqwest::header::ACCEPT, "application/vnd.github+json")
        .send()
        .await
        .map_err(|error| ApiError::bad_gateway("update_check_failed", error.to_string()))?;

    if response.status() == reqwest::StatusCode::NOT_FOUND {
        return Ok(Json(json!({
            "current_version": current,
            "latest_version": null,
            "update_available": false,
            "release_url": REPOSITORY_URL,
            "message": "No published releases were found.",
        })));
    }
    if !response.status().is_success() {
        return Err(ApiError::bad_gateway(
            "update_check_failed",
            format!("GitHub returned HTTP {}", response.status()),
        ));
    }
    let release: GitHubRelease = response
        .json()
        .await
        .map_err(|error| ApiError::bad_gateway("update_check_failed", error.to_string()))?;
    let available = !release.draft && !release.prerelease && is_newer(current, &release.tag_name);
    Ok(Json(json!({
        "current_version": current,
        "latest_version": release.tag_name.trim_start_matches(['v', 'V']),
        "update_available": available,
        "release_url": release.html_url,
        "release_name": release.name,
        "release_notes": release.body,
        "published_at": release.published_at,
        "message": if available { "A new version is available." } else { "Native GPT is up to date." },
    })))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn semantic_versions_compare_numerically() {
        assert!(is_newer("0.1.0", "v0.2.0"));
        assert!(is_newer("1.9.9", "1.10.0"));
        assert!(!is_newer("2.0.0", "v1.99.0"));
        assert!(!is_newer("1.2.3", "v1.2.3"));
    }
}
