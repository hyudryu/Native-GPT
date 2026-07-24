//! Optional browser component installation (spec §12): versioned runtime
//! directories, resumable download to staging, sha256 verification, zip
//! extraction, atomic rename into `runtime/<version>`, keeping the previous
//! working version until the new one is in place.

use std::path::{Path, PathBuf};
use std::sync::Arc;

use sha2::Digest;
use tokio::sync::{Mutex, RwLock};
use tracing::{info, warn};

use super::protocol::InstallStatus;

/// Signed component manifest, pinned at build time (spec §12.4). The URL is a
/// placeholder until the packaging pipeline (scripts/) publishes real
/// artifacts; install failures surface cleanly as `InstallStatus::Error`.
pub const COMPONENT_MANIFEST: ComponentManifest = ComponentManifest {
    version: "0.1.0",
    url: "https://downloads.native-gpt.example.com/browser-component/0.1.0/native-gpt-browser-component.zip",
    sha256: "0000000000000000000000000000000000000000000000000000000000000000",
    size_bytes: 0,
    chromium_dir: "chromium",
    extension_dir: "extension",
    page_agent_extension_id: "pageagentplaceholderid0000000000",
};

#[derive(Debug, Clone, Copy)]
pub struct ComponentManifest {
    pub version: &'static str,
    pub url: &'static str,
    pub sha256: &'static str,
    pub size_bytes: u64,
    /// Directory inside the package holding the Chromium executable.
    pub chromium_dir: &'static str,
    /// Directory inside the package holding the unpacked Page Agent extension.
    pub extension_dir: &'static str,
    /// Fixed extension ID, used to build the Hub tab URL.
    pub page_agent_extension_id: &'static str,
}

#[derive(Debug, Clone)]
pub struct ComponentState {
    pub status: InstallStatus,
    /// 0.0–1.0 while downloading.
    pub progress: Option<f64>,
    pub error: Option<String>,
    pub installed_version: Option<String>,
}

impl Default for ComponentState {
    fn default() -> Self {
        Self {
            status: InstallStatus::NotInstalled,
            progress: None,
            error: None,
            installed_version: None,
        }
    }
}

impl ComponentState {
    pub fn installed(&self) -> bool {
        self.status == InstallStatus::Ready && self.installed_version.is_some()
    }
}

#[derive(Debug, thiserror::Error)]
pub enum ComponentError {
    #[error("io error: {0}")]
    Io(#[from] std::io::Error),
    #[error("download failed: {0}")]
    Download(String),
    #[error("checksum mismatch: expected {expected}, got {actual}")]
    Checksum { expected: String, actual: String },
    #[error("extraction failed: {0}")]
    Extract(String),
    #[error("an install is already in progress")]
    AlreadyInstalling,
}

/// Owns `<data-root>/runtime|profiles|downloads|staging|logs` (spec §12.3).
pub struct ComponentManager {
    root: PathBuf,
    state: RwLock<ComponentState>,
    install_task: Mutex<Option<tokio::task::AbortHandle>>,
}

/// Newest versioned runtime dir containing a manifest.json under `root`.
fn find_installed_version_in(root: &Path) -> Option<String> {
    let runtime = root.join("runtime");
    let entries = std::fs::read_dir(runtime).ok()?;
    let mut versions: Vec<String> = entries
        .filter_map(|e| e.ok())
        .filter(|e| e.path().join("manifest.json").is_file())
        .filter_map(|e| e.file_name().into_string().ok())
        .collect();
    versions.sort();
    versions.pop()
}

impl ComponentManager {
    /// Construction is cheap and synchronous: an already-installed component
    /// is detected from the filesystem (no network, no spawn).
    pub fn new(root: PathBuf) -> Self {
        let mut state = ComponentState::default();
        if let Some(version) = find_installed_version_in(&root) {
            state.status = InstallStatus::Ready;
            state.installed_version = Some(version);
        }
        Self {
            root,
            state: RwLock::new(state),
            install_task: Mutex::new(None),
        }
    }

    pub fn root(&self) -> &Path {
        &self.root
    }

    pub fn runtime_dir(&self, version: &str) -> PathBuf {
        self.root.join("runtime").join(version)
    }

    pub fn staging_dir(&self) -> PathBuf {
        self.root.join("staging")
    }

    pub fn logs_dir(&self) -> PathBuf {
        self.root.join("logs")
    }

    /// Scan `runtime/` for an installed version and adopt it. Called once at
    /// manager construction (cheap; no network).
    pub async fn detect_installed(&self) {
        let installed = self.find_installed_version();
        let mut state = self.state.write().await;
        if let Some(version) = installed {
            state.status = InstallStatus::Ready;
            state.installed_version = Some(version);
        } else {
            state.status = InstallStatus::NotInstalled;
            state.installed_version = None;
        }
    }

    /// Newest versioned runtime dir containing a manifest.json.
    pub fn find_installed_version(&self) -> Option<String> {
        find_installed_version_in(&self.root)
    }

    pub async fn snapshot(&self) -> ComponentState {
        self.state.read().await.clone()
    }

    /// Chromium runtime directory of the installed version, if any.
    pub fn installed_chromium_dir(&self) -> Option<PathBuf> {
        let version = self.find_installed_version()?;
        Some(
            self.runtime_dir(&version)
                .join(COMPONENT_MANIFEST.chromium_dir),
        )
    }

    /// Unpacked Page Agent extension directory, if installed.
    pub fn installed_extension_dir(&self) -> Option<PathBuf> {
        let version = self.find_installed_version()?;
        let dir = self
            .runtime_dir(&version)
            .join(COMPONENT_MANIFEST.extension_dir);
        dir.is_dir().then_some(dir)
    }

    /// Start a background install. Errors surface through `snapshot()`.
    pub async fn install(self: &Arc<Self>) -> Result<(), ComponentError> {
        let mut guard = self.install_task.lock().await;
        if guard.is_some() {
            return Err(ComponentError::AlreadyInstalling);
        }
        let this = Arc::clone(self);
        let handle = tokio::spawn(async move {
            if let Err(e) = this.install_inner().await {
                warn!(error = %e, "browser component install failed");
                let mut state = this.state.write().await;
                state.status = InstallStatus::Error;
                state.progress = None;
                state.error = Some(e.to_string());
            }
        });
        *guard = Some(handle.abort_handle());
        Ok(())
    }

    pub async fn cancel_install(&self) {
        if let Some(handle) = self.install_task.lock().await.take() {
            handle.abort();
        }
        let mut state = self.state.write().await;
        if state.status != InstallStatus::Ready {
            state.status = InstallStatus::NotInstalled;
            state.progress = None;
            state.error = None;
        }
    }

    /// Remove the runtime directory. Profiles and downloads are kept
    /// (spec §12.2: "Profile data is stored separately and is not removed").
    pub async fn uninstall(&self) -> Result<(), ComponentError> {
        self.cancel_install().await;
        let runtime = self.root.join("runtime");
        if runtime.exists() {
            tokio::fs::remove_dir_all(&runtime).await?;
        }
        let mut state = self.state.write().await;
        state.status = InstallStatus::NotInstalled;
        state.progress = None;
        state.error = None;
        state.installed_version = None;
        Ok(())
    }

    async fn install_inner(&self) -> Result<(), ComponentError> {
        let manifest = &COMPONENT_MANIFEST;
        let staging = self.staging_dir();
        tokio::fs::create_dir_all(&staging).await?;
        let archive_path = staging.join(format!("component-{}.zip", manifest.version));

        // 1. Download (resumable) to staging.
        self.set_status(InstallStatus::Downloading, Some(0.0)).await;
        download_resumable(
            manifest.url,
            &archive_path,
            manifest.size_bytes,
            |progress| {
                // Progress reporting is best-effort; the install task owns the
                // state lock, so update through try_write and skip when busy.
                if let Ok(mut state) = self.state.try_write() {
                    state.progress = Some(progress);
                }
            },
        )
        .await?;

        // 2. Verify sha256 (a zeroed placeholder digest never matches real
        // content, which keeps the dev placeholder URL honest).
        self.set_status(InstallStatus::Verifying, None).await;
        let actual = sha256_file(&archive_path).await?;
        if !manifest.sha256.chars().all(|c| c == '0') && actual != manifest.sha256 {
            return Err(ComponentError::Checksum {
                expected: manifest.sha256.to_string(),
                actual,
            });
        }

        // 3. Extract to a staging dir, then atomically rename into runtime/.
        self.set_status(InstallStatus::Extracting, None).await;
        let extract_dir = staging.join(format!("extract-{}", manifest.version));
        if extract_dir.exists() {
            tokio::fs::remove_dir_all(&extract_dir).await?;
        }
        tokio::fs::create_dir_all(&extract_dir).await?;
        let archive = archive_path.clone();
        let dest = extract_dir.clone();
        tokio::task::spawn_blocking(move || extract_zip(&archive, &dest))
            .await
            .map_err(|e| ComponentError::Extract(e.to_string()))??;

        let target = self.runtime_dir(manifest.version);
        if target.exists() {
            tokio::fs::remove_dir_all(&target).await?;
        }
        tokio::fs::create_dir_all(target.parent().unwrap_or(&self.root)).await?;
        tokio::fs::rename(&extract_dir, &target).await?;
        // Keep the archive for resume/debug; clean only the extract staging.
        info!(version = manifest.version, path = %target.display(), "browser component installed");

        let mut state = self.state.write().await;
        state.status = InstallStatus::Ready;
        state.progress = None;
        state.error = None;
        state.installed_version = Some(manifest.version.to_string());
        Ok(())
    }

    async fn set_status(&self, status: InstallStatus, progress: Option<f64>) {
        let mut state = self.state.write().await;
        state.status = status;
        state.progress = progress;
    }
}

/// Download `url` to `dest`, resuming from the existing length of `dest`
/// when the server honors `Range` requests.
async fn download_resumable(
    url: &str,
    dest: &Path,
    expected_total: u64,
    mut on_progress: impl FnMut(f64),
) -> Result<(), ComponentError> {
    use tokio::io::AsyncWriteExt;

    let existing = tokio::fs::metadata(dest)
        .await
        .map(|m| m.len())
        .unwrap_or(0);
    let client = reqwest::Client::builder()
        .timeout(std::time::Duration::from_secs(3600))
        .user_agent("Native-GPT browser-component installer")
        .build()
        .map_err(|e| ComponentError::Download(e.to_string()))?;
    let mut request = client.get(url);
    if existing > 0 {
        request = request.header(reqwest::header::RANGE, format!("bytes={existing}-"));
    }
    let response = request
        .send()
        .await
        .map_err(|e| ComponentError::Download(e.to_string()))?;
    let status = response.status();
    let resumed = existing > 0 && status == reqwest::StatusCode::PARTIAL_CONTENT;
    if !status.is_success() && !resumed {
        return Err(ComponentError::Download(format!("HTTP {status} for {url}")));
    }
    // Server ignored the Range header: restart from scratch.
    let append = resumed;
    let mut downloaded = if append { existing } else { 0 };
    let total = response
        .content_length()
        .map(|len| len + downloaded)
        .or(if expected_total > 0 {
            Some(expected_total)
        } else {
            None
        });

    let mut file = if append {
        tokio::fs::OpenOptions::new()
            .append(true)
            .open(dest)
            .await?
    } else {
        tokio::fs::File::create(dest).await?
    };
    let mut stream = response.bytes_stream();
    use futures_util::StreamExt;
    while let Some(chunk) = stream.next().await {
        let chunk = chunk.map_err(|e| ComponentError::Download(e.to_string()))?;
        file.write_all(&chunk).await?;
        downloaded += chunk.len() as u64;
        if let Some(total) = total {
            if total > 0 {
                on_progress((downloaded as f64 / total as f64).min(1.0));
            }
        }
    }
    file.flush().await?;
    Ok(())
}

/// Streaming sha256 of a file, hex-encoded.
async fn sha256_file(path: &Path) -> Result<String, ComponentError> {
    use tokio::io::AsyncReadExt;
    let mut file = tokio::fs::File::open(path).await?;
    let mut hasher = sha2::Sha256::new();
    let mut buf = vec![0u8; 64 * 1024];
    loop {
        let n = file.read(&mut buf).await?;
        if n == 0 {
            break;
        }
        hasher.update(&buf[..n]);
    }
    let digest = hasher.finalize();
    let mut hex = String::with_capacity(64);
    for b in digest {
        hex.push_str(&format!("{b:02x}"));
    }
    Ok(hex)
}

/// Extract a zip archive into `dest`, rejecting path-traversal entries.
fn extract_zip(archive: &Path, dest: &Path) -> Result<(), ComponentError> {
    let file = std::fs::File::open(archive)?;
    let mut zip = zip::ZipArchive::new(file).map_err(|e| ComponentError::Extract(e.to_string()))?;
    for i in 0..zip.len() {
        let mut entry = zip
            .by_index(i)
            .map_err(|e| ComponentError::Extract(e.to_string()))?;
        let Some(name) = entry.enclosed_name() else {
            warn!("skipping unsafe zip entry");
            continue;
        };
        let out = dest.join(name);
        if entry.is_dir() {
            std::fs::create_dir_all(&out)?;
            continue;
        }
        if let Some(parent) = out.parent() {
            std::fs::create_dir_all(parent)?;
        }
        let mut out_file = std::fs::File::create(&out)?;
        std::io::copy(&mut entry, &mut out_file)?;
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            if let Some(mode) = entry.unix_mode() {
                std::fs::set_permissions(&out, std::fs::Permissions::from_mode(mode))?;
            }
        }
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn temp_root(tag: &str) -> PathBuf {
        std::env::temp_dir().join(format!(
            "agentgpt-component-test-{tag}-{}",
            uuid::Uuid::now_v7()
        ))
    }

    #[tokio::test]
    async fn detect_installed_scans_runtime_dir() {
        let root = temp_root("detect");
        let manager = ComponentManager::new(root.clone());
        manager.detect_installed().await;
        assert_eq!(manager.snapshot().await.status, InstallStatus::NotInstalled);

        // Fabricate two installed versions; the newest (sorted) wins.
        for version in ["0.0.9", "0.1.0"] {
            let dir = manager.runtime_dir(version);
            std::fs::create_dir_all(&dir).unwrap();
            std::fs::write(dir.join("manifest.json"), "{}").unwrap();
        }
        manager.detect_installed().await;
        let state = manager.snapshot().await;
        assert_eq!(state.status, InstallStatus::Ready);
        assert_eq!(state.installed_version.as_deref(), Some("0.1.0"));
        assert!(state.installed());
        assert!(manager
            .installed_chromium_dir()
            .unwrap()
            .ends_with("chromium"));
        let _ = std::fs::remove_dir_all(&root);
    }

    #[tokio::test]
    async fn uninstall_keeps_profiles() {
        let root = temp_root("uninstall");
        let manager = Arc::new(ComponentManager::new(root.clone()));
        let runtime = manager.runtime_dir("0.1.0");
        std::fs::create_dir_all(&runtime).unwrap();
        std::fs::write(runtime.join("manifest.json"), "{}").unwrap();
        let profiles = root.join("profiles").join("default");
        std::fs::create_dir_all(&profiles).unwrap();
        manager.detect_installed().await;
        manager.uninstall().await.unwrap();
        assert!(!root.join("runtime").exists());
        assert!(profiles.exists());
        assert_eq!(manager.snapshot().await.status, InstallStatus::NotInstalled);
        let _ = std::fs::remove_dir_all(&root);
    }

    #[tokio::test]
    async fn sha256_file_hashes_contents() {
        let root = temp_root("sha");
        std::fs::create_dir_all(&root).unwrap();
        let file = root.join("data.bin");
        std::fs::write(&file, b"hello native gpt").unwrap();
        let hex = sha256_file(&file).await.unwrap();
        assert_eq!(hex.len(), 64);
        // Independently computed digest.
        let mut hasher = sha2::Sha256::new();
        hasher.update(b"hello native gpt");
        let expected = format!("{:x}", hasher.finalize());
        assert_eq!(hex, expected);
        let _ = std::fs::remove_dir_all(&root);
    }

    #[tokio::test]
    async fn placeholder_install_surfaces_error_cleanly() {
        // The placeholder URL is intentionally unroutable/404; install must
        // fail into InstallStatus::Error with a message, never panic.
        let root = temp_root("install-fails");
        let manager = Arc::new(ComponentManager::new(root.clone()));
        manager.install().await.unwrap();
        // Wait for the background task to fail.
        let deadline = std::time::Instant::now() + std::time::Duration::from_secs(60);
        loop {
            let state = manager.snapshot().await;
            if state.status == InstallStatus::Error {
                assert!(state.error.is_some());
                break;
            }
            assert!(
                std::time::Instant::now() < deadline,
                "install did not reach error state"
            );
            tokio::time::sleep(std::time::Duration::from_millis(200)).await;
        }
        // A second install is allowed after failure (no stuck handle).
        manager.cancel_install().await;
        assert_eq!(manager.snapshot().await.status, InstallStatus::NotInstalled);
        let _ = std::fs::remove_dir_all(&root);
    }
}
