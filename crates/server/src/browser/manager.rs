//! `BrowserManager`: orchestrates the optional component, profile locks,
//! the Chromium process, CDP, screencast, input, the Page Agent Hub bridge,
//! task lifecycle, permissions, downloads, and viewer streams (spec §4.1).
//!
//! Concurrency (spec §19): one Chromium process per profile, one Page Agent
//! task per profile; a second task request fails with `TASK_BUSY` carrying
//! the active task's metadata. Construction is cheap — Chromium starts lazily.

use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
use std::sync::Arc;
use std::time::Duration;

use axum::http::StatusCode;
use dashmap::DashMap;
use serde_json::{json, Value};
use tokio::sync::{broadcast, mpsc, oneshot, Mutex, Notify, RwLock};
use tracing::{debug, info, warn};

use agentgpt_telemetry::Telemetry;

use crate::db::{BrowserProfileRow, BrowserTaskRow, Db, DbError};
use crate::error::ApiError;

use super::cdp::{CdpClient, CdpError};
use super::chromium::{self, ChromiumProcess};
use super::component::{ComponentManager, COMPONENT_MANIFEST};
use super::downloads::DownloadTracker;
use super::page_agent_hub::{hub_tab_url, HubInbound, HubOutbound};
use super::permissions::{self, ApprovedRoots, NavigationDecision};
use super::profile::{self, ProfileLock};
use super::protocol::{
    error_codes, BrowserState, BrowserTab, BrowserTaskState, PendingApproval, PermissionCapability,
    PermissionScope, ProcessStatus, StreamEvent, TaskStatus, ViewportSize,
};
use super::screencast::ScreencastPump;

/// Default Page Agent task timeout (spec §7.1 `timeout_seconds`).
const DEFAULT_TASK_TIMEOUT: Duration = Duration::from_secs(600);
/// Idle shutdown delays (spec §14.2).
const HIDDEN_IDLE_TIMEOUT: Duration = Duration::from_secs(30 * 60);
const BLANK_IDLE_TIMEOUT: Duration = Duration::from_secs(10 * 60);
/// How long to wait for the extension Hub tab to connect and report ready.
const HUB_READY_TIMEOUT: Duration = Duration::from_secs(45);

#[derive(Debug, Clone, Copy)]
pub struct ManagerConfig {
    pub task_timeout: Duration,
    pub hidden_idle_timeout: Duration,
    pub blank_idle_timeout: Duration,
}

impl Default for ManagerConfig {
    fn default() -> Self {
        Self {
            task_timeout: DEFAULT_TASK_TIMEOUT,
            hidden_idle_timeout: HIDDEN_IDLE_TIMEOUT,
            blank_idle_timeout: BLANK_IDLE_TIMEOUT,
        }
    }
}

/// Short-lived token minted per task for the model proxy (spec §5.4).
#[derive(Debug, Clone)]
pub struct TaskTokenGrant {
    pub task_id: String,
    pub conversation_id: Option<String>,
    pub endpoint_id: Option<String>,
    pub model_id: Option<String>,
}

/// User decision on a pending approval.
#[derive(Debug, Clone, Copy)]
pub struct ApprovalResolution {
    pub allow: bool,
    pub scope: PermissionScope,
}

/// Result of a finished Page Agent task, returned to wait=true callers.
#[derive(Debug, Clone)]
pub struct TaskOutcome {
    pub task_id: String,
    pub success: bool,
    pub result_text: Option<String>,
    pub error_code: Option<String>,
    pub error_message: Option<String>,
    pub final_url: Option<String>,
}

/// Stable-coded browser errors mapped onto `ApiError` (spec §7.3).
#[derive(Debug, thiserror::Error)]
pub enum BrowserError {
    #[error("the browser component is not installed")]
    NotInstalled,
    #[error("failed to start the browser: {0}")]
    StartFailed(String),
    #[error("the browser profile is locked by another running process (pid {0})")]
    ProfileLocked(u32),
    #[error("the browser is not running")]
    NotRunning,
    #[error("the Page Agent extension is not connected")]
    PageAgentNotConnected,
    #[error("profile already has an active task")]
    TaskBusy,
    #[error("the browser task timed out")]
    TaskTimeout,
    #[error("the browser task was cancelled")]
    TaskCancelled,
    #[error("navigation to this URL is blocked")]
    NavigationBlocked,
    #[error("approval is required to navigate to this origin")]
    OriginPermissionRequired,
    #[error("approval is required to upload these files")]
    FilePermissionRequired,
    #[error("file not found or not approved: {0}")]
    FileNotFound(String),
    #[error("this download is blocked")]
    DownloadBlocked,
    #[error("the browser process crashed")]
    Crashed,
    #[error("the approval was denied")]
    ApprovalDenied,
    #[error("CDP error: {0}")]
    Cdp(#[from] CdpError),
    #[error("database error: {0}")]
    Db(#[from] DbError),
    #[error("io error: {0}")]
    Io(#[from] std::io::Error),
    #[error("{0}")]
    BadRequest(String),
}

impl BrowserError {
    pub fn code(&self) -> &'static str {
        match self {
            Self::NotInstalled => error_codes::BROWSER_NOT_INSTALLED,
            Self::StartFailed(_) => error_codes::BROWSER_START_FAILED,
            Self::ProfileLocked(_) => error_codes::PROFILE_LOCKED,
            Self::NotRunning => error_codes::BROWSER_START_FAILED,
            Self::PageAgentNotConnected => error_codes::PAGE_AGENT_NOT_CONNECTED,
            Self::TaskBusy => error_codes::TASK_BUSY,
            Self::TaskTimeout => error_codes::TASK_TIMEOUT,
            Self::TaskCancelled | Self::ApprovalDenied => error_codes::TASK_CANCELLED,
            Self::NavigationBlocked => error_codes::NAVIGATION_BLOCKED,
            Self::OriginPermissionRequired => error_codes::ORIGIN_PERMISSION_REQUIRED,
            Self::FilePermissionRequired => error_codes::FILE_PERMISSION_REQUIRED,
            Self::FileNotFound(_) => error_codes::FILE_NOT_FOUND,
            Self::DownloadBlocked => error_codes::DOWNLOAD_BLOCKED,
            Self::Crashed => error_codes::BROWSER_CRASHED,
            Self::Cdp(e) => e.code(),
            Self::Db(_) | Self::Io(_) | Self::BadRequest(_) => "internal_error",
        }
    }

    pub fn into_api_error(self) -> ApiError {
        let status = match self {
            Self::NotInstalled => StatusCode::PRECONDITION_FAILED,
            Self::OriginPermissionRequired | Self::FilePermissionRequired => {
                StatusCode::PRECONDITION_REQUIRED
            }
            Self::TaskBusy | Self::ProfileLocked(_) | Self::TaskCancelled => StatusCode::CONFLICT,
            Self::FileNotFound(_) => StatusCode::NOT_FOUND,
            Self::NavigationBlocked | Self::DownloadBlocked | Self::ApprovalDenied => {
                StatusCode::FORBIDDEN
            }
            Self::TaskTimeout => StatusCode::GATEWAY_TIMEOUT,
            Self::PageAgentNotConnected | Self::NotRunning => StatusCode::SERVICE_UNAVAILABLE,
            Self::BadRequest(_) => StatusCode::BAD_REQUEST,
            _ => StatusCode::INTERNAL_SERVER_ERROR,
        };
        ApiError::new(status, self.code(), self.to_string())
    }
}

/// A browser tab tracked by the manager.
#[derive(Debug, Clone)]
struct TabEntry {
    tab: BrowserTab,
    target_id: String,
    session_id: Option<String>,
}

/// Mutable runtime state (everything shown in `/api/browser/state`).
struct RuntimeState {
    process_status: ProcessStatus,
    profile_id: String,
    tabs: Vec<TabEntry>,
    active_tab_id: Option<String>,
    task: Option<BrowserTaskState>,
    hub_target_id: Option<String>,
    last_error: Option<String>,
}

impl RuntimeState {
    fn new() -> Self {
        Self {
            process_status: ProcessStatus::Stopped,
            profile_id: "default".to_string(),
            tabs: Vec::new(),
            active_tab_id: None,
            task: None,
            hub_target_id: None,
            last_error: None,
        }
    }

    fn public_tabs(&self) -> Vec<BrowserTab> {
        self.tabs.iter().map(|entry| entry.tab.clone()).collect()
    }
}

struct Inner {
    runtime: RwLock<RuntimeState>,
    cdp: RwLock<Option<Arc<CdpClient>>>,
    profile_lock: Mutex<Option<ProfileLock>>,
    hub: RwLock<Option<mpsc::UnboundedSender<String>>>,
    hub_events: broadcast::Sender<HubInbound>,
    hub_token: RwLock<String>,
    hub_connected: AtomicBool,
    pump: Mutex<Option<ScreencastPump>>,
    process_shutdown: Mutex<Option<oneshot::Sender<()>>>,
    events: broadcast::Sender<StreamEvent>,
    viewers: AtomicUsize,
    remote_viewers: AtomicUsize,
    approvals: DashMap<String, oneshot::Sender<ApprovalResolution>>,
    approval_meta: DashMap<String, PendingApproval>,
    task_tokens: DashMap<String, TaskTokenGrant>,
    task_cancel: Mutex<Option<TaskCancelHandle>>,
    task_done: Mutex<Option<oneshot::Receiver<TaskOutcome>>>,
    manual_control: AtomicBool,
    idle_timer: Mutex<Option<tokio::task::JoinHandle<()>>>,
    config: ManagerConfig,
}

struct TaskCancelHandle {
    task_id: String,
    cancelled: Arc<AtomicBool>,
    notify: Arc<Notify>,
}

/// Events that drive the task state machine (unit-tested directly).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum TaskEvent {
    Approved,
    Denied,
    Started,
    Completed,
    Failed,
    CancelRequested,
    TimedOut,
}

/// Pure task state transition table. Returns the next status and the error
/// code to record for terminal failures, or `None` for illegal transitions.
pub fn transition_task(
    current: TaskStatus,
    event: TaskEvent,
) -> Option<(TaskStatus, Option<&'static str>)> {
    use TaskEvent as E;
    use TaskStatus as S;
    match (current, event) {
        (S::AwaitingApproval, E::Approved) => Some((S::Starting, None)),
        (S::AwaitingApproval, E::Denied) => Some((S::Cancelled, Some(error_codes::TASK_CANCELLED))),
        (S::AwaitingApproval, E::CancelRequested) => {
            Some((S::Cancelled, Some(error_codes::TASK_CANCELLED)))
        }
        (S::Starting, E::Started) => Some((S::Running, None)),
        (S::Starting, E::Failed) => Some((S::Failed, Some(error_codes::BROWSER_START_FAILED))),
        (S::Running, E::Completed) => Some((S::Completed, None)),
        (S::Running, E::Failed) => Some((S::Failed, None)),
        (S::Running, E::TimedOut) => Some((S::Failed, Some(error_codes::TASK_TIMEOUT))),
        (S::Running, E::CancelRequested) => Some((S::Cancelled, Some(error_codes::TASK_CANCELLED))),
        (S::PausedForUser, E::Started) => Some((S::Running, None)),
        (S::PausedForUser, E::CancelRequested) => {
            Some((S::Cancelled, Some(error_codes::TASK_CANCELLED)))
        }
        (S::Stopping, E::Completed) => Some((S::Cancelled, Some(error_codes::TASK_CANCELLED))),
        (S::Stopping, E::Failed) => Some((S::Cancelled, Some(error_codes::TASK_CANCELLED))),
        _ => None,
    }
}

/// Clamp a requested panel width to spec §2.3 bounds: minimum panel 320px,
/// keeping at least 420px of center content (the UI switches to an overlay
/// below that, so the clamp never crushes the chat).
pub fn clamp_panel_width(width: u32, container_width: u32) -> u32 {
    const MIN_PANEL: u32 = 320;
    const MIN_CONTENT: u32 = 420;
    let max = container_width.saturating_sub(MIN_CONTENT).max(MIN_PANEL);
    width.clamp(MIN_PANEL, max)
}

pub struct BrowserManager {
    db: Db,
    repo_root: PathBuf,
    data_root: PathBuf,
    server_port: u16,
    telemetry: Telemetry,
    pub component: Arc<ComponentManager>,
    inner: Arc<Inner>,
}

impl BrowserManager {
    /// Cheap constructor: filesystem scan only; Chromium is never started here.
    pub fn new(db: Db, repo_root: PathBuf, server_port: u16) -> Self {
        let data_root = profile::browser_data_root(&repo_root);
        let component = Arc::new(ComponentManager::new(data_root.clone()));
        let (events, _) = broadcast::channel(256);
        let (hub_events, _) = broadcast::channel(64);
        Self {
            db,
            repo_root,
            data_root: data_root.clone(),
            server_port,
            telemetry: Telemetry::new(),
            component,
            inner: Arc::new(Inner {
                runtime: RwLock::new(RuntimeState::new()),
                cdp: RwLock::new(None),
                profile_lock: Mutex::new(None),
                hub: RwLock::new(None),
                hub_events,
                hub_token: RwLock::new(String::new()),
                hub_connected: AtomicBool::new(false),
                pump: Mutex::new(None),
                process_shutdown: Mutex::new(None),
                events,
                viewers: AtomicUsize::new(0),
                remote_viewers: AtomicUsize::new(0),
                approvals: DashMap::new(),
                approval_meta: DashMap::new(),
                task_tokens: DashMap::new(),
                task_cancel: Mutex::new(None),
                task_done: Mutex::new(None),
                manual_control: AtomicBool::new(true),
                idle_timer: Mutex::new(None),
                config: ManagerConfig::default(),
            }),
        }
    }

    pub fn data_root(&self) -> &Path {
        &self.data_root
    }

    pub fn subscribe_events(&self) -> broadcast::Receiver<StreamEvent> {
        self.inner.events.subscribe()
    }

    /// The origin the Hub extension must present (configurable via the
    /// component manifest, spec §5.2).
    pub fn expected_extension_origin(&self) -> String {
        super::page_agent_hub::expected_extension_origin(COMPONENT_MANIFEST.page_agent_extension_id)
    }

    pub fn emit(&self, event: StreamEvent) {
        let _ = self.inner.events.send(event);
    }

    // ---- state snapshot ----

    pub async fn state_snapshot(
        &self,
        panel_mode: super::protocol::BrowserPanelMode,
        panel_width: u32,
        previous_panel_width: Option<u32>,
    ) -> BrowserState {
        let runtime = self.inner.runtime.read().await;
        let component = self.component.snapshot().await;
        BrowserState {
            installed: component.installed(),
            install_status: component.status,
            install_error: component.error,
            install_progress: component.progress,
            installed_version: component.installed_version,
            process_status: runtime.process_status,
            profile_id: runtime.profile_id.clone(),
            panel_mode,
            panel_width,
            previous_panel_width,
            connected: self
                .inner
                .cdp
                .read()
                .await
                .as_ref()
                .is_some_and(|c| !c.is_closed()),
            active_tab_id: runtime.active_tab_id.clone(),
            tabs: runtime.public_tabs(),
            task: runtime.task.clone(),
            manual_control_enabled: self.inner.manual_control.load(Ordering::SeqCst),
            remote_viewer_count: self.inner.remote_viewers.load(Ordering::SeqCst) as u32,
            pending_approvals: self
                .inner
                .approval_meta
                .iter()
                .map(|entry| entry.value().clone())
                .collect(),
        }
    }

    pub async fn emit_state_now(&self) {
        let prefs = self
            .db
            .get_browser_preferences("default")
            .await
            .ok()
            .flatten();
        let (mode, width, previous) = match prefs {
            Some(p) => (
                super::protocol::BrowserPanelMode::parse(&p.panel_mode).unwrap_or_default(),
                u32::try_from(p.panel_width.max(0)).unwrap_or(640),
                p.previous_panel_width
                    .and_then(|w| u32::try_from(w.max(0)).ok()),
            ),
            None => Default::default(),
        };
        let snapshot = self.state_snapshot(mode, width, previous).await;
        self.emit(StreamEvent::State(Box::new(snapshot)));
    }

    // ---- approvals ----

    /// Register a pending approval; resolves when the UI calls
    /// [`Self::resolve_approval`].
    pub fn request_approval(
        &self,
        capability: PermissionCapability,
        origin: Option<String>,
        description: String,
        task_id: Option<String>,
    ) -> (String, oneshot::Receiver<ApprovalResolution>) {
        let id = uuid::Uuid::now_v7().to_string();
        let (tx, rx) = oneshot::channel();
        let meta = PendingApproval {
            id: id.clone(),
            capability: capability.as_str().to_string(),
            origin,
            description,
            task_id,
            created_at: chrono::Utc::now().to_rfc3339(),
        };
        self.inner.approvals.insert(id.clone(), tx);
        self.inner.approval_meta.insert(id.clone(), meta);
        (id, rx)
    }

    pub fn resolve_approval(&self, id: &str, resolution: ApprovalResolution) -> bool {
        self.inner.approval_meta.remove(id);
        if let Some((_, tx)) = self.inner.approvals.remove(id) {
            let _ = tx.send(resolution);
            true
        } else {
            false
        }
    }

    // ---- task tokens (model proxy) ----

    pub fn mint_task_token(&self, grant: TaskTokenGrant) -> String {
        let token = uuid::Uuid::now_v7().simple().to_string();
        self.inner.task_tokens.insert(token.clone(), grant);
        token
    }

    pub fn validate_task_token(&self, token: &str) -> Option<TaskTokenGrant> {
        self.inner.task_tokens.get(token).map(|g| g.clone())
    }

    pub fn revoke_task_token(&self, token: &str) {
        self.inner.task_tokens.remove(token);
    }

    // ---- Hub bridge ----

    pub async fn hub_token(&self) -> String {
        self.inner.hub_token.read().await.clone()
    }

    /// Register the extension Hub WebSocket writer (one at a time).
    pub async fn register_hub(&self, sender: mpsc::UnboundedSender<String>) {
        *self.inner.hub.write().await = Some(sender);
        self.inner.hub_connected.store(true, Ordering::SeqCst);
        debug!("page agent hub connected");
    }

    pub async fn unregister_hub(&self) {
        *self.inner.hub.write().await = None;
        self.inner.hub_connected.store(false, Ordering::SeqCst);
        debug!("page agent hub disconnected");
    }

    pub fn hub_connected(&self) -> bool {
        self.inner.hub_connected.load(Ordering::SeqCst)
    }

    async fn hub_send(&self, message: HubOutbound) -> Result<(), BrowserError> {
        let text =
            serde_json::to_string(&message).map_err(|e| BrowserError::BadRequest(e.to_string()))?;
        let guard = self.inner.hub.read().await;
        let sender = guard.as_ref().ok_or(BrowserError::PageAgentNotConnected)?;
        sender
            .send(text)
            .map_err(|_| BrowserError::PageAgentNotConnected)
    }

    /// Handle one raw text frame from the Hub socket.
    pub async fn dispatch_hub_inbound(&self, text: &str) {
        match HubInbound::parse(text) {
            Ok(message) => {
                if let HubInbound::Activity { message, url, .. } = &message {
                    let mut runtime = self.inner.runtime.write().await;
                    if let Some(task) = runtime.task.as_mut() {
                        task.activity = Some(message.clone());
                        let payload = json!({
                            "taskId": task.id,
                            "activity": message,
                            "url": url,
                        });
                        drop(runtime);
                        self.emit(StreamEvent::TaskActivity(payload));
                    }
                }
                let _ = self.inner.hub_events.send(message);
            }
            Err(e) => debug!(error = %e, "ignoring malformed hub message"),
        }
    }

    // ---- lifecycle ----

    async fn cdp(&self) -> Result<Arc<CdpClient>, BrowserError> {
        self.inner
            .cdp
            .read()
            .await
            .clone()
            .ok_or(BrowserError::NotRunning)
    }

    fn process_is_alive(&self, pid: u32) -> bool {
        self.telemetry.process_rss_bytes(pid).is_some()
    }

    /// Resolve a profile's on-disk directory, filling in the seeded empty
    /// `profile_path` lazily (migration 0009 stores '' for env-dependent paths).
    async fn resolve_profile_dir(
        &self,
        profile: &BrowserProfileRow,
    ) -> Result<PathBuf, BrowserError> {
        if !profile.profile_path.is_empty() {
            return Ok(PathBuf::from(&profile.profile_path));
        }
        let dir = profile::profile_dir(&self.data_root, &profile.id);
        let mut updated = profile.clone();
        updated.profile_path = dir.to_string_lossy().into_owned();
        updated.updated_at = chrono::Utc::now().to_rfc3339();
        self.db.update_browser_profile(&updated).await?;
        Ok(dir)
    }

    /// Start Chromium for `profile_id` (idempotent while running).
    pub async fn start(&self, profile_id: &str) -> Result<(), BrowserError> {
        {
            let runtime = self.inner.runtime.read().await;
            if runtime.process_status == ProcessStatus::Running
                || runtime.process_status == ProcessStatus::Starting
            {
                return Ok(());
            }
        }
        {
            let mut runtime = self.inner.runtime.write().await;
            runtime.process_status = ProcessStatus::Starting;
            runtime.profile_id = profile_id.to_string();
            runtime.last_error = None;
        }

        let result = self.start_inner(profile_id).await;
        if let Err(e) = &result {
            let mut runtime = self.inner.runtime.write().await;
            runtime.process_status = ProcessStatus::Stopped;
            runtime.last_error = Some(e.to_string());
        }
        result
    }

    async fn start_inner(&self, profile_id: &str) -> Result<(), BrowserError> {
        let profile = self
            .db
            .get_browser_profile(profile_id)
            .await?
            .ok_or_else(|| BrowserError::BadRequest(format!("profile {profile_id} not found")))?;
        let profile_dir = self.resolve_profile_dir(&profile).await?;
        std::fs::create_dir_all(profile::downloads_dir(&profile_dir))?;
        std::fs::create_dir_all(profile::uploads_dir(&profile_dir))?;

        let system_path = profile
            .executable_path
            .as_deref()
            .filter(|p| !p.is_empty())
            .map(PathBuf::from);
        let executable = chromium::resolve_executable(&self.component, system_path.as_deref())
            .map_err(|e| match e {
                chromium::ChromiumError::NoExecutable => BrowserError::NotInstalled,
                other => BrowserError::StartFailed(other.to_string()),
            })?;

        // One Chromium process per profile (spec §6.3).
        let lock =
            ProfileLock::acquire(&profile_dir, |pid| self.process_is_alive(pid)).map_err(|e| {
                match e {
                    profile::ProfileError::Locked(pid) => BrowserError::ProfileLocked(pid),
                    other => BrowserError::StartFailed(other.to_string()),
                }
            })?;

        let extension_dir = self.component.installed_extension_dir();
        let process = chromium::launch(&executable, &profile_dir, extension_dir.as_deref())
            .await
            .map_err(|e| BrowserError::StartFailed(e.to_string()))?;

        let cdp = Arc::new(
            CdpClient::connect(&process.websocket_url)
                .await
                .map_err(|e| BrowserError::StartFailed(e.to_string()))?,
        );
        *self.inner.cdp.write().await = Some(Arc::clone(&cdp));
        *self.inner.profile_lock.lock().await = Some(lock);

        // Downloads land in the profile's Downloads/ with events enabled.
        let downloads_dir = profile::downloads_dir(&profile_dir);
        let _ = cdp
            .set_download_behavior(&downloads_dir.to_string_lossy())
            .await;

        // Fresh hub token per browser start.
        *self.inner.hub_token.write().await = uuid::Uuid::now_v7().simple().to_string();

        // Discover existing page targets.
        let targets = cdp
            .call("Target.getTargets", json!({}), None)
            .await
            .unwrap_or(Value::Null);
        let mut runtime = self.inner.runtime.write().await;
        runtime.tabs.clear();
        if let Some(infos) = targets.get("targetInfos").and_then(Value::as_array) {
            for info in infos {
                if info.get("type").and_then(Value::as_str) != Some("page") {
                    continue;
                }
                let target_id = info
                    .get("targetId")
                    .and_then(Value::as_str)
                    .unwrap_or_default()
                    .to_string();
                let url = info
                    .get("url")
                    .and_then(Value::as_str)
                    .unwrap_or("about:blank")
                    .to_string();
                let title = info
                    .get("title")
                    .and_then(Value::as_str)
                    .unwrap_or_default()
                    .to_string();
                let id = uuid::Uuid::now_v7().to_string();
                runtime.tabs.push(TabEntry {
                    tab: BrowserTab {
                        id: id.clone(),
                        title,
                        url,
                        favicon_url: None,
                        loading: false,
                        can_go_back: false,
                        can_go_forward: false,
                        internal: false,
                    },
                    target_id,
                    session_id: None,
                });
                if runtime.active_tab_id.is_none() {
                    runtime.active_tab_id = Some(id);
                }
            }
        }
        drop(runtime);

        // Launch the internal Page Agent Hub tab (spec §5.1).
        let hub_url = hub_tab_url(
            COMPONENT_MANIFEST.page_agent_extension_id,
            self.server_port,
            &self.hub_token().await,
        );
        match cdp.create_target(&hub_url).await {
            Ok(target_id) => {
                let mut runtime = self.inner.runtime.write().await;
                runtime.hub_target_id = Some(target_id);
            }
            Err(e) => warn!(error = %e, "failed to open page agent hub tab"),
        }

        let _ = cdp
            .call(
                "Target.setDiscoverTargets",
                json!({ "discover": true }),
                None,
            )
            .await;

        self.spawn_cdp_event_pump();
        self.spawn_crash_watcher(process).await;
        self.db
            .touch_browser_profile(profile_id, &chrono::Utc::now().to_rfc3339())
            .await?;

        let mut runtime = self.inner.runtime.write().await;
        runtime.process_status = ProcessStatus::Running;
        drop(runtime);
        info!(profile_id, "browser started");
        Ok(())
    }

    /// Stop Chromium gracefully. Active tasks make this a no-op error
    /// (spec §7.2 `close_browser` confirms no task is active).
    pub async fn stop(&self) -> Result<(), BrowserError> {
        {
            let runtime = self.inner.runtime.read().await;
            if runtime.process_status == ProcessStatus::Stopped {
                return Ok(());
            }
            if runtime.task.as_ref().is_some_and(|t| t.status.is_active()) {
                return Err(BrowserError::TaskBusy);
            }
        }
        {
            let mut runtime = self.inner.runtime.write().await;
            runtime.process_status = ProcessStatus::Stopping;
        }
        self.stop_pump().await;
        if let Ok(cdp) = self.cdp().await {
            let _ = cdp.browser_close().await;
        }
        // Ask the watcher to enforce the graceful-shutdown deadline.
        if let Some(shutdown) = self.inner.process_shutdown.lock().await.take() {
            let _ = shutdown.send(());
        }
        *self.inner.cdp.write().await = None;
        *self.inner.hub.write().await = None;
        self.inner.hub_connected.store(false, Ordering::SeqCst);
        *self.inner.profile_lock.lock().await = None;
        if let Some(timer) = self.inner.idle_timer.lock().await.take() {
            timer.abort();
        }
        let mut runtime = self.inner.runtime.write().await;
        runtime.process_status = ProcessStatus::Stopped;
        runtime.tabs.clear();
        runtime.active_tab_id = None;
        runtime.hub_target_id = None;
        info!("browser stopped");
        Ok(())
    }

    /// The watcher owns the child process: it observes natural exits (crash
    /// flow, spec §14.4) and enforces the graceful-shutdown deadline when
    /// [`Self::stop`] signals it (Browser.close, then kill after timeout).
    async fn spawn_crash_watcher(&self, mut process: ChromiumProcess) {
        const GRACEFUL: Duration = Duration::from_secs(5);
        let pid = process.pid();
        let inner = Arc::clone(&self.inner);
        let this = self.clone_handle();
        let (shutdown_tx, shutdown_rx) = oneshot::channel::<()>();
        *self.inner.process_shutdown.lock().await = Some(shutdown_tx);
        tokio::spawn(async move {
            tokio::select! {
                status = process.wait() => {
                    let mut runtime = inner.runtime.write().await;
                    // Only treat as a crash when we didn't initiate the stop.
                    if runtime.process_status == ProcessStatus::Running
                        || runtime.process_status == ProcessStatus::Starting
                    {
                        warn!(?status, pid, "chromium exited unexpectedly");
                        runtime.process_status = ProcessStatus::Crashed;
                        let task_id = runtime.task.as_ref().map(|t| t.id.clone());
                        if let Some(task) = runtime.task.as_mut() {
                            task.status = TaskStatus::Failed;
                        }
                        drop(runtime);
                        if let Some(task_id) = task_id {
                            let _ = this
                                .db
                                .finish_browser_task(
                                    &task_id,
                                    "failed",
                                    None,
                                    Some(error_codes::BROWSER_CRASHED),
                                    Some("the browser process crashed"),
                                    None,
                                    &chrono::Utc::now().to_rfc3339(),
                                )
                                .await;
                            this.emit(StreamEvent::TaskFailed(json!({
                                "taskId": task_id,
                                "errorCode": error_codes::BROWSER_CRASHED,
                            })));
                        }
                        this.emit(StreamEvent::Crashed(json!({ "pid": pid })));
                        *inner.cdp.write().await = None;
                        *inner.hub.write().await = None;
                        inner.hub_connected.store(false, Ordering::SeqCst);
                        *inner.profile_lock.lock().await = None;
                    }
                }
                _ = shutdown_rx => {
                    // stop() already tried Browser.close; give Chromium a
                    // moment to flush the profile, then kill (spec §14.2).
                    match tokio::time::timeout(GRACEFUL, process.wait()).await {
                        Ok(_) => info!(pid, "chromium exited after Browser.close"),
                        Err(_) => {
                            warn!(pid, "chromium did not exit in time; killing");
                            process.kill().await;
                        }
                    }
                }
            }
        });
    }

    /// Cloneable handle sharing all interior state (used by WS tasks).
    pub fn clone_handle(&self) -> BrowserManager {
        BrowserManager {
            db: self.db.clone(),
            repo_root: self.repo_root.clone(),
            data_root: self.data_root.clone(),
            server_port: self.server_port,
            telemetry: Telemetry::new(),
            component: Arc::clone(&self.component),
            inner: Arc::clone(&self.inner),
        }
    }

    /// Profile the running (or last-started) browser is bound to.
    pub async fn active_profile_id(&self) -> String {
        self.inner.runtime.read().await.profile_id.clone()
    }

    /// Route CDP events to tabs, downloads, file chooser and crash flows.
    fn spawn_cdp_event_pump(&self) {
        let inner = Arc::clone(&self.inner);
        let this = self.clone_handle();
        tokio::spawn(async move {
            let Some(cdp) = inner.cdp.read().await.clone() else {
                return;
            };
            let mut events = cdp.subscribe();
            loop {
                match events.recv().await {
                    Ok(event) => this.handle_cdp_event(event).await,
                    Err(broadcast::error::RecvError::Lagged(_)) => continue,
                    Err(broadcast::error::RecvError::Closed) => break,
                }
            }
        });
    }

    async fn handle_cdp_event(&self, event: super::cdp::CdpEvent) {
        let inner = &self.inner;
        match event.method.as_str() {
            "Target.targetCreated" => {
                let info = &event.params["targetInfo"];
                if info["type"].as_str() != Some("page") {
                    return;
                }
                let url = info["url"].as_str().unwrap_or_default();
                if url.starts_with("chrome-extension://") {
                    return; // the Hub tab is internal, not in the tab strip
                }
                let id = uuid::Uuid::now_v7().to_string();
                let tab = BrowserTab {
                    id: id.clone(),
                    title: info["title"].as_str().unwrap_or_default().to_string(),
                    url: url.to_string(),
                    favicon_url: None,
                    loading: false,
                    can_go_back: false,
                    can_go_forward: false,
                    internal: false,
                };
                let entry = TabEntry {
                    tab: tab.clone(),
                    target_id: info["targetId"].as_str().unwrap_or_default().to_string(),
                    session_id: None,
                };
                let mut runtime = inner.runtime.write().await;
                if runtime.tabs.iter().any(|t| t.target_id == entry.target_id) {
                    return; // already tracked (e.g. created via new_tab)
                }
                runtime.tabs.push(entry);
                if runtime.active_tab_id.is_none() {
                    runtime.active_tab_id = Some(id);
                }
                drop(runtime);
                self.emit(StreamEvent::TabCreated(tab));
            }
            "Target.targetDestroyed" => {
                let target_id = event.params["targetId"].as_str().unwrap_or_default();
                let mut runtime = inner.runtime.write().await;
                let Some(position) = runtime.tabs.iter().position(|t| t.target_id == target_id)
                else {
                    return;
                };
                let removed = runtime.tabs.remove(position);
                if runtime.active_tab_id.as_deref() == Some(removed.tab.id.as_str()) {
                    runtime.active_tab_id = runtime.tabs.first().map(|t| t.tab.id.clone());
                }
                drop(runtime);
                self.emit(StreamEvent::TabClosed(json!({ "id": removed.tab.id })));
            }
            "Target.targetInfoChanged" => {
                let info = &event.params["targetInfo"];
                let target_id = info["targetId"].as_str().unwrap_or_default();
                let mut runtime = inner.runtime.write().await;
                let Some(entry) = runtime.tabs.iter_mut().find(|t| t.target_id == target_id) else {
                    return;
                };
                if let Some(title) = info["title"].as_str() {
                    entry.tab.title = title.to_string();
                }
                if let Some(url) = info["url"].as_str() {
                    entry.tab.url = url.to_string();
                }
                let tab = entry.tab.clone();
                drop(runtime);
                self.emit(StreamEvent::TabUpdated(tab));
            }
            "Page.fileChooserOpened" => {
                self.emit(StreamEvent::FileChooser(json!({
                    "sessionId": event.session_id,
                })));
            }
            "Browser.downloadWillBegin" => {
                let guid = event.params["guid"]
                    .as_str()
                    .unwrap_or_default()
                    .to_string();
                let suggested = event.params["suggestedFilename"]
                    .as_str()
                    .unwrap_or("download")
                    .to_string();
                let url = event.params["url"].as_str().map(str::to_string);
                let runtime = inner.runtime.read().await;
                let profile_id = runtime.profile_id.clone();
                let task_id = runtime.task.as_ref().map(|t| t.id.clone());
                let dir =
                    profile::downloads_dir(&profile::profile_dir(&self.data_root, &profile_id));
                drop(runtime);
                let tracker =
                    DownloadTracker::new(self.db.clone(), profile_id, inner.events.clone());
                tracker
                    .will_begin(&guid, &suggested, url.as_deref(), &dir, task_id.as_deref())
                    .await;
            }
            "Browser.downloadProgress" => {
                let guid = event.params["guid"].as_str().unwrap_or_default();
                let state = event.params["state"].as_str().unwrap_or("inProgress");
                let received = event.params["receivedBytes"].as_i64();
                let runtime = inner.runtime.read().await;
                let profile_id = runtime.profile_id.clone();
                drop(runtime);
                let tracker =
                    DownloadTracker::new(self.db.clone(), profile_id, inner.events.clone());
                tracker.progress(guid, state, received).await;
            }
            "Inspector.targetCrashed" => {
                self.emit(StreamEvent::Crashed(json!({
                    "sessionId": event.session_id,
                    "target": true,
                })));
            }
            _ => {}
        }
    }

    // ---- tabs ----

    async fn active_session(&self) -> Result<(String, String), BrowserError> {
        let runtime = self.inner.runtime.write().await;
        let active_id = runtime
            .active_tab_id
            .clone()
            .ok_or(BrowserError::BadRequest("no active tab".into()))?;
        let position = runtime
            .tabs
            .iter()
            .position(|t| t.tab.id == active_id)
            .ok_or(BrowserError::BadRequest("active tab not found".into()))?;
        if let Some(session) = &runtime.tabs[position].session_id {
            return Ok((active_id, session.clone()));
        }
        let target_id = runtime.tabs[position].target_id.clone();
        drop(runtime);
        let cdp = self.cdp().await?;
        let session = cdp.attach_to_target(&target_id).await?;
        cdp.page_enable(&session).await?;
        cdp.runtime_enable(&session).await?;
        let _ = cdp.intercept_file_chooser(&session).await;
        let mut runtime = self.inner.runtime.write().await;
        if let Some(entry) = runtime.tabs.get_mut(position) {
            entry.session_id = Some(session.clone());
        }
        Ok((active_id, session))
    }

    pub async fn new_tab(&self, url: Option<&str>) -> Result<BrowserTab, BrowserError> {
        let cdp = self.cdp().await?;
        let url = url.unwrap_or("about:blank");
        let target_id = cdp.create_target(url).await?;
        // The tab appears through Target.targetCreated; fall back to a
        // locally constructed entry when discovery is disabled.
        let id = uuid::Uuid::now_v7().to_string();
        let tab = BrowserTab {
            id: id.clone(),
            title: String::new(),
            url: url.to_string(),
            favicon_url: None,
            loading: true,
            can_go_back: false,
            can_go_forward: false,
            internal: false,
        };
        let mut runtime = self.inner.runtime.write().await;
        if !runtime.tabs.iter().any(|t| t.target_id == target_id) {
            runtime.tabs.push(TabEntry {
                tab: tab.clone(),
                target_id,
                session_id: None,
            });
        }
        runtime.active_tab_id = Some(id);
        drop(runtime);
        self.emit(StreamEvent::TabCreated(tab.clone()));
        Ok(tab)
    }

    pub async fn close_tab(&self, tab_id: &str) -> Result<(), BrowserError> {
        let target_id = {
            let runtime = self.inner.runtime.read().await;
            runtime
                .tabs
                .iter()
                .find(|t| t.tab.id == tab_id)
                .map(|t| t.target_id.clone())
        };
        let Some(target_id) = target_id else {
            return Err(BrowserError::BadRequest(format!("tab {tab_id} not found")));
        };
        let cdp = self.cdp().await?;
        cdp.close_target(&target_id).await?;
        Ok(())
    }

    pub async fn activate_tab(&self, tab_id: &str) -> Result<(), BrowserError> {
        {
            let mut runtime = self.inner.runtime.write().await;
            if !runtime.tabs.iter().any(|t| t.tab.id == tab_id) {
                return Err(BrowserError::BadRequest(format!("tab {tab_id} not found")));
            }
            runtime.active_tab_id = Some(tab_id.to_string());
        }
        // Restart the frame pump against the newly active tab when viewers
        // are watching.
        if self.inner.viewers.load(Ordering::SeqCst) > 0 {
            self.restart_pump(None).await.ok();
        }
        Ok(())
    }

    /// Manual or agent navigation with the private-network policy (spec §11.4).
    pub async fn navigate(
        &self,
        url: &str,
        agent_initiated: bool,
        conversation_id: Option<&str>,
    ) -> Result<(), BrowserError> {
        match permissions::navigation_decision(url, agent_initiated, self.server_port) {
            NavigationDecision::Blocked => return Err(BrowserError::NavigationBlocked),
            NavigationDecision::NeedApproval => {
                let origin = url_origin(url);
                let profile_id = self.inner.runtime.read().await.profile_id.clone();
                match permissions::check(
                    &self.db,
                    &profile_id,
                    PermissionCapability::NavigatePrivateNetwork,
                    origin.as_deref(),
                    conversation_id,
                )
                .await?
                {
                    permissions::PermissionDecision::Allow => {}
                    permissions::PermissionDecision::NeedApproval => {
                        return Err(BrowserError::OriginPermissionRequired)
                    }
                }
            }
            NavigationDecision::Allow => {}
        }
        let cdp = self.cdp().await?;
        let (_tab, session) = self.active_session().await?;
        cdp.navigate(&session, url).await?;
        self.emit(StreamEvent::Navigation(json!({ "url": url })));
        Ok(())
    }

    // ---- screencast pump / viewers ----

    pub async fn viewer_connected(&self, remote: bool) {
        self.inner.viewers.fetch_add(1, Ordering::SeqCst);
        if remote {
            self.inner.remote_viewers.fetch_add(1, Ordering::SeqCst);
        }
        if self.inner.viewers.load(Ordering::SeqCst) == 1 {
            // Resume streaming when the first viewer connects (spec §10.1).
            self.restart_pump(None).await.ok();
        }
        self.cancel_idle_timer().await;
    }

    pub async fn viewer_disconnected(&self, remote: bool) {
        let viewers = self.inner.viewers.fetch_sub(1, Ordering::SeqCst) - 1;
        if remote {
            self.inner.remote_viewers.fetch_sub(1, Ordering::SeqCst);
        }
        if viewers == 0 {
            // Pause screencast when nobody is watching (spec §10.1/§14.3).
            self.stop_pump().await;
            self.schedule_idle_timer().await;
        }
    }

    pub async fn subscribe_frames(
        &self,
    ) -> Option<broadcast::Receiver<Arc<super::protocol::Frame>>> {
        let pump = self.inner.pump.lock().await;
        pump.as_ref().map(|p| p.subscribe())
    }

    async fn stop_pump(&self) {
        if let Some(pump) = self.inner.pump.lock().await.take() {
            pump.stop().await;
        }
    }

    async fn restart_pump(&self, viewport: Option<ViewportSize>) -> Result<(), BrowserError> {
        self.stop_pump().await;
        let cdp = self.cdp().await?;
        let (_tab, session) = self.active_session().await?;
        let viewport = viewport.unwrap_or(ViewportSize {
            width: 1280,
            height: 800,
            device_scale_factor: 1.0,
        });
        let viewers = self.inner.viewers.load(Ordering::SeqCst);
        let pump = ScreencastPump::start(cdp, session, viewport, viewers.max(1)).await?;
        *self.inner.pump.lock().await = Some(pump);
        Ok(())
    }

    /// Panel resize → device metrics override + screencast restart
    /// (spec §10.3: no browser relaunch).
    pub async fn viewport_resize(&self, size: ViewportSize) -> Result<(), BrowserError> {
        let cdp = self.cdp().await?;
        let (_tab, session) = self.active_session().await?;
        cdp.set_device_metrics(&session, size.width, size.height, size.device_scale_factor)
            .await?;
        if self.inner.viewers.load(Ordering::SeqCst) > 0 {
            self.restart_pump(Some(size)).await?;
        }
        Ok(())
    }

    // ---- input ----

    pub fn manual_input_allowed(&self) -> bool {
        self.inner.manual_control.load(Ordering::SeqCst)
    }

    pub async fn dispatch_input(
        &self,
        command: &super::protocol::ClientCommand,
    ) -> Result<(), BrowserError> {
        use super::protocol::ClientCommand as C;
        let cdp = self.cdp().await?;
        match command {
            C::ViewportResize { payload } => return self.viewport_resize(*payload).await,
            C::TabActivate { tab_id } => return self.activate_tab(tab_id).await,
            C::FrameAck { .. } => return Ok(()), // flow control is bounded by the broadcast
            _ => {}
        }
        if !self.manual_input_allowed() {
            return Err(BrowserError::TaskBusy);
        }
        let (_tab, session) = self.active_session().await?;
        match command {
            C::Mouse { payload } => {
                let params = super::input::mouse_params(payload, 1.0);
                cdp.dispatch_mouse_event(&session, params).await?;
            }
            C::Wheel { payload } => {
                let params = super::input::wheel_params(payload, 1.0);
                cdp.dispatch_mouse_event(&session, params).await?;
            }
            C::Key { payload } => {
                let params = super::input::key_params(payload);
                cdp.dispatch_key_event(&session, params).await?;
            }
            C::Text { payload } => {
                cdp.insert_text(&session, &payload.text).await?;
            }
            _ => unreachable!("handled above"),
        }
        Ok(())
    }

    // ---- idle timers (spec §14.2) ----

    async fn cancel_idle_timer(&self) {
        if let Some(timer) = self.inner.idle_timer.lock().await.take() {
            timer.abort();
        }
    }

    async fn schedule_idle_timer(&self) {
        self.cancel_idle_timer().await;
        let this = self.clone_handle();
        let inner = Arc::clone(&self.inner);
        let timer = tokio::spawn(async move {
            let timeout = {
                let runtime = inner.runtime.read().await;
                if runtime.task.as_ref().is_some_and(|t| t.status.is_active()) {
                    return; // a task is active: never idle-stop (spec §14.2)
                }
                let only_blank = runtime
                    .tabs
                    .iter()
                    .all(|t| t.tab.url == "about:blank" || t.tab.internal);
                if only_blank {
                    inner.config.blank_idle_timeout
                } else {
                    inner.config.hidden_idle_timeout
                }
            };
            tokio::time::sleep(timeout).await;
            if inner.viewers.load(Ordering::SeqCst) == 0 {
                this.stop().await.ok();
            }
        });
        *self.inner.idle_timer.lock().await = Some(timer);
    }

    // ---- tasks (spec §5.3, §19) ----

    pub async fn active_task(&self) -> Option<BrowserTaskState> {
        self.inner.runtime.read().await.task.clone()
    }

    /// Execute a Page Agent task. When `wait` is false the task runs in the
    /// background and its metadata is returned immediately.
    pub async fn execute_task(
        &self,
        task_text: &str,
        initial_url: Option<&str>,
        conversation_id: Option<&str>,
        run_id: Option<&str>,
        tool_call_id: Option<&str>,
        wait: bool,
    ) -> Result<TaskOutcome, BrowserError> {
        if let Some(active) = self.active_task().await {
            if active.status.is_active() {
                return Err(BrowserError::TaskBusy);
            }
        }
        self.start("default").await?;

        let task_id = uuid::Uuid::now_v7().to_string();
        let task_state = BrowserTaskState {
            id: task_id.clone(),
            conversation_id: conversation_id.map(str::to_string),
            run_id: run_id.map(str::to_string),
            tool_call_id: tool_call_id.map(str::to_string),
            description: task_text.to_string(),
            status: TaskStatus::AwaitingApproval,
            started_at: Some(chrono::Utc::now().to_rfc3339()),
            activity: None,
        };
        let row = BrowserTaskRow {
            id: task_id.clone(),
            profile_id: "default".to_string(),
            conversation_id: conversation_id.map(str::to_string),
            run_id: run_id.map(str::to_string),
            tool_call_id: tool_call_id.map(str::to_string),
            task_text: task_text.to_string(),
            initial_url: initial_url.map(str::to_string),
            final_url: None,
            status: TaskStatus::AwaitingApproval.as_str().to_string(),
            result_text: None,
            error_code: None,
            error_message: None,
            started_at: chrono::Utc::now().to_rfc3339(),
            finished_at: None,
        };
        self.db.insert_browser_task(&row).await?;
        self.set_task_state(Some(task_state.clone())).await;
        self.emit(StreamEvent::TaskStarted(task_state));

        let (done_tx, done_rx) = oneshot::channel::<TaskOutcome>();
        if wait {
            *self.inner.task_done.lock().await = Some(done_rx);
        }
        let this = self.clone_handle();
        let task_id_owned = task_id.clone();
        let task_text = task_text.to_string();
        let initial_url = initial_url.map(str::to_string);
        let conversation_id = conversation_id.map(str::to_string);
        tokio::spawn(async move {
            let outcome = this
                .run_task(&task_id_owned, &task_text, initial_url, conversation_id)
                .await;
            let _ = done_tx.send(outcome);
        });

        if !wait {
            return Ok(TaskOutcome {
                task_id,
                success: true,
                result_text: None,
                error_code: None,
                error_message: None,
                final_url: None,
            });
        }
        let rx = self
            .inner
            .task_done
            .lock()
            .await
            .take()
            .ok_or(BrowserError::TaskCancelled)?;
        rx.await.map_err(|_| BrowserError::TaskCancelled)
    }

    async fn set_task_state(&self, task: Option<BrowserTaskState>) {
        self.inner.runtime.write().await.task = task;
    }

    async fn transition_active_task(&self, event: TaskEvent) -> Option<TaskStatus> {
        let mut runtime = self.inner.runtime.write().await;
        let task = runtime.task.as_mut()?;
        let (next, _) = transition_task(task.status, event)?;
        task.status = next;
        Some(next)
    }

    /// Full task lifecycle: approval → navigate → hub execute → result.
    async fn run_task(
        &self,
        task_id: &str,
        task_text: &str,
        initial_url: Option<String>,
        conversation_id: Option<String>,
    ) -> TaskOutcome {
        let result = self
            .run_task_inner(
                task_id,
                task_text,
                initial_url.as_deref(),
                conversation_id.as_deref(),
            )
            .await;
        let outcome = match result {
            Ok(outcome) => {
                if let Err(e) = self
                    .finish_task(
                        task_id,
                        TaskStatus::Completed,
                        outcome.result_text.as_deref(),
                        None,
                        None,
                        outcome.final_url.as_deref(),
                    )
                    .await
                {
                    warn!(error = %e, "failed to persist task completion");
                }
                self.emit(StreamEvent::TaskFinished(json!({
                    "taskId": task_id,
                    "success": true,
                })));
                outcome
            }
            Err(e) => {
                // Errors before/inside the hub phase still finish the audit
                // row (approval-phase failures already finished it — the
                // UPDATE is idempotent in effect, matching status again).
                let code = e.code();
                let status = if code == error_codes::TASK_CANCELLED {
                    TaskStatus::Cancelled
                } else {
                    TaskStatus::Failed
                };
                if let Err(db_err) = self
                    .finish_task(
                        task_id,
                        status,
                        None,
                        Some(code),
                        Some(&e.to_string()),
                        None,
                    )
                    .await
                {
                    warn!(error = %db_err, "failed to persist task failure");
                }
                self.emit(StreamEvent::TaskFailed(json!({
                    "taskId": task_id,
                    "errorCode": code,
                    "errorMessage": e.to_string(),
                })));
                TaskOutcome {
                    task_id: task_id.to_string(),
                    success: false,
                    result_text: None,
                    error_code: Some(code.to_string()),
                    error_message: Some(e.to_string()),
                    final_url: None,
                }
            }
        };
        // Cleanup on every exit path: the profile slot frees and manual
        // control resumes (spec §2.5).
        self.set_task_state(None).await;
        *self.inner.task_cancel.lock().await = None;
        self.inner.manual_control.store(true, Ordering::SeqCst);
        outcome
    }

    async fn run_task_inner(
        &self,
        task_id: &str,
        task_text: &str,
        initial_url: Option<&str>,
        conversation_id: Option<&str>,
    ) -> Result<TaskOutcome, BrowserError> {
        let profile_id = "default";

        // 1. Task-level approval (spec §11.2) unless a stored grant covers it.
        let origin = initial_url.and_then(url_origin);
        let decision = permissions::check(
            &self.db,
            profile_id,
            PermissionCapability::NavigatePublicWeb,
            origin.as_deref(),
            conversation_id,
        )
        .await?;
        if matches!(decision, permissions::PermissionDecision::NeedApproval) {
            let (approval_id, rx) = self.request_approval(
                PermissionCapability::NavigatePublicWeb,
                origin.clone(),
                format!("Native GPT wants to control the browser: {task_text}"),
                Some(task_id.to_string()),
            );
            self.emit_state_now().await;
            debug!(approval_id, "task waiting for approval");
            let resolution = match tokio::time::timeout(self.inner.config.task_timeout, rx).await {
                Ok(Ok(resolution)) => resolution,
                _ => return Err(BrowserError::TaskTimeout),
            };
            if !resolution.allow {
                self.transition_active_task(TaskEvent::Denied).await;
                return Err(BrowserError::ApprovalDenied);
            }
            if !matches!(resolution.scope, PermissionScope::Once) {
                permissions::grant(
                    &self.db,
                    profile_id,
                    PermissionCapability::NavigatePublicWeb,
                    resolution.scope,
                    origin.as_deref(),
                    conversation_id,
                )
                .await?;
            }
        }
        self.transition_active_task(TaskEvent::Approved).await;
        self.db
            .update_browser_task_status(task_id, TaskStatus::Starting.as_str())
            .await?;

        // 2. Manual control is suspended while the agent owns the tab.
        self.inner.manual_control.store(false, Ordering::SeqCst);
        let cancel = TaskCancelHandle {
            task_id: task_id.to_string(),
            cancelled: Arc::new(AtomicBool::new(false)),
            notify: Arc::new(Notify::new()),
        };
        let cancel_notify = Arc::clone(&cancel.notify);
        *self.inner.task_cancel.lock().await = Some(cancel);

        let result = self
            .run_task_with_hub(
                task_id,
                task_text,
                initial_url,
                conversation_id,
                cancel_notify,
            )
            .await;

        // 3. Cleanup: manual control resumes regardless of outcome (spec §2.5).
        *self.inner.task_cancel.lock().await = None;
        self.inner.manual_control.store(true, Ordering::SeqCst);
        result
    }

    async fn run_task_with_hub(
        &self,
        task_id: &str,
        task_text: &str,
        initial_url: Option<&str>,
        conversation_id: Option<&str>,
        cancel_notify: Arc<Notify>,
    ) -> Result<TaskOutcome, BrowserError> {
        // 3. Navigate to the initial URL (agent policy applies).
        if let Some(url) = initial_url {
            self.navigate(url, true, conversation_id).await?;
        }

        // 4. Wait for the extension Hub to connect and say ready.
        if !self.hub_connected() {
            return Err(BrowserError::PageAgentNotConnected);
        }
        let mut hub_events = self.inner.hub_events.subscribe();
        tokio::time::timeout(
            self.inner.config.task_timeout.min(HUB_READY_TIMEOUT),
            async {
                loop {
                    match hub_events.recv().await {
                        Ok(HubInbound::Ready) => return Ok(()),
                        Ok(_) => continue,
                        Err(broadcast::error::RecvError::Lagged(_)) => continue,
                        Err(broadcast::error::RecvError::Closed) => {
                            return Err(BrowserError::PageAgentNotConnected)
                        }
                    }
                }
            },
        )
        .await
        .map_err(|_| BrowserError::PageAgentNotConnected)??;

        // 5. Mint the model-proxy token and send `execute` (spec §5.4).
        let grant = self.model_grant(task_id, conversation_id).await;
        let token = self.mint_task_token(grant);
        let config = json!({
            "baseURL": format!("http://127.0.0.1:{}/internal/page-agent/v1", self.server_port),
            "apiKey": token,
            "model": self.model_name_for_config(conversation_id).await,
        });
        self.transition_active_task(TaskEvent::Started).await;
        self.db
            .update_browser_task_status(task_id, TaskStatus::Running.as_str())
            .await?;
        self.hub_send(HubOutbound::Execute {
            task: task_text.to_string(),
            config,
        })
        .await?;

        // 6. Await result/error with timeout and cancellation.
        let wait_result = tokio::time::timeout(self.inner.config.task_timeout, async {
            loop {
                tokio::select! {
                    _ = cancel_notify.notified() => {
                        return Err(BrowserError::TaskCancelled);
                    }
                    message = hub_events.recv() => {
                        match message {
                            Ok(HubInbound::Result { success, data }) => {
                                return Ok((success, data));
                            }
                            Ok(HubInbound::Error { message }) => {
                                return Ok((false, Value::String(message)));
                            }
                            Ok(_) => continue,
                            Err(broadcast::error::RecvError::Lagged(_)) => continue,
                            Err(broadcast::error::RecvError::Closed) => {
                                return Err(BrowserError::Cdp(CdpError::Disconnected));
                            }
                        }
                    }
                }
            }
        })
        .await;

        self.revoke_task_token(&token);
        match wait_result {
            Err(_) => {
                // TASK_TIMEOUT (spec §7.3); try to stop the agent best-effort.
                let _ = self.hub_send(HubOutbound::Stop).await;
                Err(BrowserError::TaskTimeout)
            }
            Ok(Err(e)) => {
                // Cancelled (stop / take over).
                let _ = self.hub_send(HubOutbound::Stop).await;
                self.transition_active_task(TaskEvent::CancelRequested)
                    .await;
                Err(e)
            }
            Ok(Ok((success, data))) => {
                let text = data.as_str().unwrap_or(&data.to_string()).to_string();
                let final_url = {
                    let runtime = self.inner.runtime.read().await;
                    let active = runtime.active_tab_id.clone();
                    runtime
                        .tabs
                        .iter()
                        .find(|t| Some(&t.tab.id) == active.as_ref())
                        .map(|t| t.tab.url.clone())
                };
                if success {
                    self.transition_active_task(TaskEvent::Completed).await;
                    Ok(TaskOutcome {
                        task_id: task_id.to_string(),
                        success: true,
                        result_text: Some(text),
                        error_code: None,
                        error_message: None,
                        final_url,
                    })
                } else {
                    self.transition_active_task(TaskEvent::Failed).await;
                    Err(BrowserError::BadRequest(format!(
                        "page agent failed: {text}"
                    )))
                }
            }
        }
    }

    /// Model selection for the Hub config (spec §5.3): fixed selection from
    /// preferences, or the conversation model when following it.
    async fn model_grant(&self, task_id: &str, conversation_id: Option<&str>) -> TaskTokenGrant {
        let prefs = self
            .db
            .get_browser_preferences("default")
            .await
            .ok()
            .flatten();
        let (endpoint_id, model_id) = match prefs {
            Some(p) if p.model_mode == "fixed" => (p.model_endpoint_id, p.model_id),
            _ => (None, None),
        };
        TaskTokenGrant {
            task_id: task_id.to_string(),
            conversation_id: conversation_id.map(str::to_string),
            endpoint_id,
            model_id,
        }
    }

    async fn model_name_for_config(&self, conversation_id: Option<&str>) -> String {
        let prefs = self
            .db
            .get_browser_preferences("default")
            .await
            .ok()
            .flatten();
        if let Some(p) = prefs {
            if p.model_mode == "fixed" {
                if let Some(model) = p.model_id {
                    return model;
                }
            }
        }
        if let Some(conversation_id) = conversation_id {
            if let Ok(resolved) = self.db.resolve_conversation_model(conversation_id).await {
                return resolved.model_id;
            }
        }
        "default".to_string()
    }

    async fn finish_task(
        &self,
        task_id: &str,
        status: TaskStatus,
        result_text: Option<&str>,
        error_code: Option<&str>,
        error_message: Option<&str>,
        final_url: Option<&str>,
    ) -> Result<(), BrowserError> {
        self.db
            .finish_browser_task(
                task_id,
                status.as_str(),
                result_text,
                error_code,
                error_message,
                final_url,
                &chrono::Utc::now().to_rfc3339(),
            )
            .await?;
        Ok(())
    }

    /// Stop the current task (cancel; manual control resumes) or take over
    /// (same cancellation, explicitly user-initiated). Both paths share the
    /// cancellation flow; take_over additionally guarantees manual control.
    pub async fn stop_task(&self, task_id: &str) -> Result<(), BrowserError> {
        let guard = self.inner.task_cancel.lock().await;
        let Some(handle) = guard.as_ref() else {
            return Err(BrowserError::BadRequest("no active task".into()));
        };
        if handle.task_id != task_id {
            return Err(BrowserError::BadRequest(format!(
                "task {task_id} is not the active task"
            )));
        }
        handle.cancelled.store(true, Ordering::SeqCst);
        handle.notify.notify_one();
        Ok(())
    }

    /// Take over: cancel the task and immediately re-enable manual control
    /// (spec §2.5), without waiting for the task runner to observe the
    /// cancellation.
    pub async fn take_over(&self, task_id: &str) -> Result<(), BrowserError> {
        self.stop_task(task_id).await?;
        self.inner.manual_control.store(true, Ordering::SeqCst);
        self.emit_state_now().await;
        Ok(())
    }

    // ---- screenshot / upload ----

    /// Capture the active tab viewport; bytes are saved under
    /// `<data-root>/screenshots/` and the path is returned.
    pub async fn screenshot(&self) -> Result<PathBuf, BrowserError> {
        let cdp = self.cdp().await?;
        let (_tab, session) = self.active_session().await?;
        let data = cdp.capture_screenshot(&session, "jpeg", Some(80)).await?;
        use base64::Engine;
        let bytes = base64::engine::general_purpose::STANDARD
            .decode(&data)
            .map_err(|e| BrowserError::BadRequest(e.to_string()))?;
        let dir = self.data_root.join("screenshots");
        std::fs::create_dir_all(&dir)?;
        let path = dir.join(format!(
            "screenshot-{}.jpg",
            chrono::Utc::now().format("%Y%m%d-%H%M%S%.3f")
        ));
        std::fs::write(&path, bytes)?;
        Ok(path)
    }

    /// Approved upload roots (spec §6.5): the app-data tree (conversation
    /// attachments, generated assets, project files) and the profile's
    /// Uploads/ staging directory.
    pub fn approved_roots(&self) -> ApprovedRoots {
        ApprovedRoots::new(vec![
            self.repo_root.join("app-data"),
            profile::uploads_dir(&profile::profile_dir(&self.data_root, "default")),
        ])
    }

    /// Validate upload file paths against approved roots (spec §15.1).
    pub fn validate_upload_files(&self, paths: &[String]) -> Result<Vec<PathBuf>, BrowserError> {
        if paths.is_empty() {
            return Err(BrowserError::BadRequest("no files given".into()));
        }
        let roots = self.approved_roots();
        let mut validated = Vec::with_capacity(paths.len());
        for raw in paths {
            let path = PathBuf::from(raw);
            if !path.exists() {
                return Err(BrowserError::FileNotFound(raw.clone()));
            }
            if !roots.is_allowed(&path) {
                return Err(BrowserError::FileNotFound(format!(
                    "{raw} is outside the approved upload roots"
                )));
            }
            validated.push(path);
        }
        Ok(validated)
    }

    /// Set approved files on the page's file input via CDP (no native picker).
    pub async fn upload_files(&self, paths: &[String]) -> Result<(), BrowserError> {
        let validated = self.validate_upload_files(paths)?;
        let cdp = self.cdp().await?;
        let (_tab, session) = self.active_session().await?;
        let document = cdp
            .call("DOM.getDocument", json!({ "depth": 1 }), Some(&session))
            .await?;
        let node_id = document["root"]["nodeId"]
            .as_u64()
            .ok_or(BrowserError::BadRequest("no document".into()))?;
        let input = cdp
            .call(
                "DOM.querySelector",
                json!({ "nodeId": node_id, "selector": "input[type=file]" }),
                Some(&session),
            )
            .await?;
        let file_node = input["nodeId"].as_u64().unwrap_or(0);
        if file_node == 0 {
            return Err(BrowserError::BadRequest(
                "no file input is visible on the page".into(),
            ));
        }
        let files: Vec<String> = validated
            .iter()
            .map(|p| p.to_string_lossy().into_owned())
            .collect();
        cdp.set_file_input_files(&session, file_node, &files)
            .await?;
        Ok(())
    }
}

/// Extract an `https://host[:port]` origin from a URL.
fn url_origin(url: &str) -> Option<String> {
    let scheme_end = url.find("://")?;
    let scheme = &url[..scheme_end];
    let rest = &url[scheme_end + 3..];
    let authority = rest.split(['/', '?', '#']).next()?;
    if authority.is_empty() {
        return None;
    }
    Some(format!("{scheme}://{authority}"))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn clamp_panel_width_respects_minimums() {
        assert_eq!(clamp_panel_width(100, 2000), 320);
        assert_eq!(clamp_panel_width(640, 2000), 640);
        // Keep at least 420px of center content.
        assert_eq!(clamp_panel_width(1900, 2000), 1580);
        // Very narrow windows: the panel never goes below 320 (UI overlays).
        assert_eq!(clamp_panel_width(500, 500), 320);
    }

    #[test]
    fn task_transitions_cover_lifecycle() {
        use TaskEvent as E;
        use TaskStatus as S;
        assert_eq!(
            transition_task(S::AwaitingApproval, E::Approved),
            Some((S::Starting, None))
        );
        assert_eq!(
            transition_task(S::AwaitingApproval, E::Denied),
            Some((S::Cancelled, Some(error_codes::TASK_CANCELLED)))
        );
        assert_eq!(
            transition_task(S::Starting, E::Started),
            Some((S::Running, None))
        );
        assert_eq!(
            transition_task(S::Running, E::Completed),
            Some((S::Completed, None))
        );
        assert_eq!(
            transition_task(S::Running, E::TimedOut),
            Some((S::Failed, Some(error_codes::TASK_TIMEOUT)))
        );
        assert_eq!(
            transition_task(S::Running, E::CancelRequested),
            Some((S::Cancelled, Some(error_codes::TASK_CANCELLED)))
        );
        // Terminal states accept no further transitions.
        assert_eq!(transition_task(S::Completed, E::Started), None);
        assert_eq!(transition_task(S::Cancelled, E::Completed), None);
        assert_eq!(transition_task(S::Failed, E::CancelRequested), None);
    }

    #[test]
    fn error_codes_are_stable() {
        assert_eq!(BrowserError::NotInstalled.code(), "BROWSER_NOT_INSTALLED");
        assert_eq!(BrowserError::TaskBusy.code(), "TASK_BUSY");
        assert_eq!(BrowserError::TaskTimeout.code(), "TASK_TIMEOUT");
        assert_eq!(
            BrowserError::OriginPermissionRequired.code(),
            "ORIGIN_PERMISSION_REQUIRED"
        );
        assert_eq!(
            BrowserError::Cdp(CdpError::Disconnected).code(),
            "CDP_DISCONNECTED"
        );
        assert_eq!(BrowserError::Crashed.code(), "BROWSER_CRASHED");
    }

    #[test]
    fn url_origin_extraction() {
        assert_eq!(
            url_origin("https://example.com/path?q=1"),
            Some("https://example.com".to_string())
        );
        assert_eq!(
            url_origin("http://localhost:3000/app"),
            Some("http://localhost:3000".to_string())
        );
        assert_eq!(url_origin("not-a-url"), None);
    }
}
