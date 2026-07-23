//! Dedicated Chromium process lifecycle (spec §3, §14): executable
//! resolution (bundled component first, then a configured system browser),
//! spawn with CDP enabled, graceful shutdown, and crash detection.
//!
//! Chromium always runs `--headless=new`: the panel is rendered from CDP
//! screencast frames, so no foreign native window ever appears and behavior
//! is identical across platforms (ADR-0009).

use std::path::{Path, PathBuf};
use std::time::Duration;

use tokio::io::{AsyncBufReadExt, BufReader};
use tokio::process::{Child, Command};
use tracing::{debug, info, warn};

use super::component::ComponentManager;

/// How long to wait for the `DevTools listening on ws://…` stderr line.
const DEVTOOLS_LINE_TIMEOUT: Duration = Duration::from_secs(30);
/// Grace period between `Browser.close` and a hard kill.
const GRACEFUL_SHUTDOWN_TIMEOUT: Duration = Duration::from_secs(5);

#[derive(Debug, thiserror::Error)]
pub enum ChromiumError {
    #[error("io error: {0}")]
    Io(#[from] std::io::Error),
    #[error("no browser executable found (component not installed and no system path configured)")]
    NoExecutable,
    #[error("configured executable does not exist: {0}")]
    ExecutableMissing(PathBuf),
    #[error("Chromium exited before reporting its DevTools endpoint (status: {0:?})")]
    EarlyExit(Option<i32>),
    #[error("timed out waiting for the DevTools endpoint")]
    DevToolsTimeout,
    #[error("failed to parse the DevTools endpoint from Chromium output")]
    DevToolsParse,
}

/// A spawned Chromium child with its CDP endpoint.
pub struct ChromiumProcess {
    child: Child,
    /// `ws://127.0.0.1:<port>/devtools/browser/<id>`
    pub websocket_url: String,
    pub port: u16,
    pub executable: PathBuf,
}

impl ChromiumProcess {
    pub fn pid(&self) -> Option<u32> {
        self.child.id()
    }

    /// Wait for the child to exit; used by the manager's crash watcher.
    pub async fn wait(&mut self) -> std::io::Result<std::process::ExitStatus> {
        self.child.wait().await
    }

    /// Hard kill (used after the graceful-shutdown timeout expires).
    pub async fn kill(&mut self) {
        if let Err(e) = self.child.kill().await {
            debug!(error = %e, "chromium kill after graceful shutdown failed");
        }
    }

    /// Graceful stop: the caller should first try `Browser.close` over CDP;
    /// this waits briefly, then kills.
    pub async fn shutdown(mut self) {
        match tokio::time::timeout(GRACEFUL_SHUTDOWN_TIMEOUT, self.child.wait()).await {
            Ok(Ok(status)) => info!(?status, "chromium exited"),
            Ok(Err(e)) => warn!(error = %e, "error waiting for chromium exit"),
            Err(_) => {
                warn!("chromium did not exit after Browser.close; killing");
                self.kill().await;
            }
        }
    }
}

/// Executable candidates by platform inside the component's `chromium/` dir.
fn executable_names() -> &'static [&'static str] {
    #[cfg(target_os = "windows")]
    {
        &["chrome.exe", "chromium.exe"]
    }
    #[cfg(target_os = "macos")]
    {
        &["Chromium.app/Contents/MacOS/Chromium", "chrome", "chromium"]
    }
    #[cfg(all(unix, not(target_os = "macos")))]
    {
        &["chrome", "chromium", "headless_shell"]
    }
}

/// Depth-limited search for a Chromium executable under `dir`.
fn find_executable_in(dir: &Path, depth: u32) -> Option<PathBuf> {
    if depth > 4 || !dir.is_dir() {
        return None;
    }
    for name in executable_names() {
        let candidate = dir.join(name);
        if candidate.is_file() {
            return Some(candidate);
        }
    }
    let entries = std::fs::read_dir(dir).ok()?;
    for entry in entries.flatten() {
        let path = entry.path();
        if path.is_dir() {
            if let Some(found) = find_executable_in(&path, depth + 1) {
                return Some(found);
            }
        }
    }
    None
}

/// Resolve the executable: bundled component first, then the configured
/// system browser path (spec §3.2). The Native GPT profile directory is used
/// in both cases — never the user's own Chrome profile.
pub fn resolve_executable(
    component: &ComponentManager,
    system_path: Option<&Path>,
) -> Result<PathBuf, ChromiumError> {
    if let Some(chromium_dir) = component.installed_chromium_dir() {
        if let Some(exe) = find_executable_in(&chromium_dir, 0) {
            return Ok(exe);
        }
    }
    if let Some(system) = system_path {
        if system.is_file() {
            return Ok(system.to_path_buf());
        }
        return Err(ChromiumError::ExecutableMissing(system.to_path_buf()));
    }
    Err(ChromiumError::NoExecutable)
}

/// Launch Chromium headless with CDP on an OS-assigned port, parsing the
/// chosen port from the `DevTools listening on ws://…` stderr line.
pub async fn launch(
    executable: &Path,
    profile_dir: &Path,
    extension_dir: Option<&Path>,
) -> Result<ChromiumProcess, ChromiumError> {
    let mut command = Command::new(executable);
    command
        .arg("--headless=new")
        .arg("--remote-debugging-port=0")
        .arg(format!("--user-data-dir={}", profile_dir.display()))
        .arg("--no-first-run")
        .arg("--no-default-browser-check")
        .arg("--disable-sync")
        // The screencast renders the page; no GPU window is needed.
        .arg("--disable-gpu")
        .arg("--mute-audio")
        .arg("about:blank")
        .stdin(std::process::Stdio::null())
        .stdout(std::process::Stdio::null())
        .stderr(std::process::Stdio::piped())
        .kill_on_drop(true);
    if let Some(extension) = extension_dir {
        command.arg(format!("--load-extension={}", extension.display()));
    }

    info!(executable = %executable.display(), profile = %profile_dir.display(), "launching chromium");
    let mut child = command.spawn()?;
    let stderr = child.stderr.take().map(BufReader::new);
    let Some(mut lines) = stderr.map(|s| s.lines()) else {
        return Err(ChromiumError::DevToolsParse);
    };

    let websocket_url = tokio::time::timeout(DEVTOOLS_LINE_TIMEOUT, async {
        while let Ok(Some(line)) = lines.next_line().await {
            if let Some(url) = parse_devtools_line(&line) {
                return Ok(url);
            }
        }
        // stderr closed without a DevTools line: the process died.
        let status = child.try_wait().ok().flatten().and_then(|s| s.code());
        Err(ChromiumError::EarlyExit(status))
    })
    .await
    .map_err(|_| ChromiumError::DevToolsTimeout)??;

    let port = parse_port(&websocket_url).ok_or(ChromiumError::DevToolsParse)?;
    info!(port, "chromium devtools endpoint ready");
    Ok(ChromiumProcess {
        child,
        websocket_url,
        port,
        executable: executable.to_path_buf(),
    })
}

/// Extract the WebSocket URL from a `DevTools listening on ws://…` line.
pub fn parse_devtools_line(line: &str) -> Option<String> {
    let marker = "DevTools listening on ";
    let start = line.find(marker)? + marker.len();
    let url = line[start..].trim();
    url.starts_with("ws://").then(|| url.to_string())
}

/// Parse the port out of `ws://127.0.0.1:<port>/devtools/browser/<id>`.
pub fn parse_port(ws_url: &str) -> Option<u16> {
    let rest = ws_url.strip_prefix("ws://")?;
    let authority = rest.split('/').next()?;
    let (_, port) = authority.rsplit_once(':')?;
    port.parse().ok()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_devtools_line() {
        let line = "[1234:5678:0101/120000.000:INFO:chrome_browser_main_loop.cc] \
                    DevTools listening on ws://127.0.0.1:51234/devtools/browser/abc-def";
        let url = parse_devtools_line(line).expect("url");
        assert_eq!(url, "ws://127.0.0.1:51234/devtools/browser/abc-def");
        assert_eq!(parse_port(&url), Some(51234));
        assert!(parse_devtools_line("some other log line").is_none());
        assert!(parse_devtools_line("DevTools listening on http://nope").is_none());
    }

    #[test]
    fn find_executable_walks_component_layout() {
        let root =
            std::env::temp_dir().join(format!("agentgpt-chromium-test-{}", uuid::Uuid::now_v7()));
        let name = executable_names()[0];
        let exe = root.join("nested").join("bin").join(name);
        std::fs::create_dir_all(exe.parent().unwrap()).unwrap();
        std::fs::write(&exe, b"fake").unwrap();
        assert_eq!(find_executable_in(&root, 0), Some(exe));
        assert!(find_executable_in(&root.join("missing"), 0).is_none());
        let _ = std::fs::remove_dir_all(&root);
    }

    #[test]
    fn system_executable_fallback_requires_existing_file() {
        let component = ComponentManager::new(std::env::temp_dir().join("definitely-missing"));
        match resolve_executable(&component, None) {
            Err(ChromiumError::NoExecutable) => {}
            other => panic!("expected NoExecutable, got {:?}", other.is_ok()),
        }
        let missing = Path::new("/no/such/browser");
        match resolve_executable(&component, Some(missing)) {
            Err(ChromiumError::ExecutableMissing(p)) => assert_eq!(p, missing),
            other => panic!("expected ExecutableMissing, got {:?}", other.is_ok()),
        }
    }
}
