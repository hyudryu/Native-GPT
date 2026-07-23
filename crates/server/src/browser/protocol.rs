//! Shared types for the Native GPT Browser (spec §9, §18): panel modes,
//! install/process/task states, stream events, client commands, the binary
//! screencast frame codec, and stable error codes (spec §7.3).

use axum::body::Bytes;
use serde::{Deserialize, Serialize};

// ---- panel / install / process / task states ----

/// Right-panel layout mode (spec §2.3).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize, Default)]
#[serde(rename_all = "camelCase")]
pub enum BrowserPanelMode {
    #[default]
    Hidden,
    Compact,
    Split,
    Expanded,
    Focus,
}

impl BrowserPanelMode {
    pub fn as_str(&self) -> &'static str {
        match self {
            Self::Hidden => "hidden",
            Self::Compact => "compact",
            Self::Split => "split",
            Self::Expanded => "expanded",
            Self::Focus => "focus",
        }
    }

    pub fn parse(value: &str) -> Option<Self> {
        match value {
            "hidden" => Some(Self::Hidden),
            "compact" => Some(Self::Compact),
            "split" => Some(Self::Split),
            "expanded" => Some(Self::Expanded),
            "focus" => Some(Self::Focus),
            _ => None,
        }
    }
}

/// Optional browser component install state (spec §12.1, §18).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum InstallStatus {
    NotInstalled,
    Downloading,
    Verifying,
    Extracting,
    Ready,
    Error,
}

impl InstallStatus {
    pub fn as_str(&self) -> &'static str {
        match self {
            Self::NotInstalled => "not_installed",
            Self::Downloading => "downloading",
            Self::Verifying => "verifying",
            Self::Extracting => "extracting",
            Self::Ready => "ready",
            Self::Error => "error",
        }
    }
}

/// Chromium process lifecycle state (spec §18).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ProcessStatus {
    Stopped,
    Starting,
    Running,
    Stopping,
    Crashed,
}

impl ProcessStatus {
    pub fn as_str(&self) -> &'static str {
        match self {
            Self::Stopped => "stopped",
            Self::Starting => "starting",
            Self::Running => "running",
            Self::Stopping => "stopping",
            Self::Crashed => "crashed",
        }
    }
}

/// Page Agent task lifecycle (spec §18).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum TaskStatus {
    AwaitingApproval,
    Starting,
    Running,
    PausedForUser,
    Stopping,
    Completed,
    Failed,
    Cancelled,
}

impl TaskStatus {
    pub fn as_str(&self) -> &'static str {
        match self {
            Self::AwaitingApproval => "awaiting_approval",
            Self::Starting => "starting",
            Self::Running => "running",
            Self::PausedForUser => "paused_for_user",
            Self::Stopping => "stopping",
            Self::Completed => "completed",
            Self::Failed => "failed",
            Self::Cancelled => "cancelled",
        }
    }

    /// True while the task holds the one-task-per-profile slot (spec §19).
    pub fn is_active(&self) -> bool {
        matches!(
            self,
            Self::AwaitingApproval
                | Self::Starting
                | Self::Running
                | Self::PausedForUser
                | Self::Stopping
        )
    }
}

// ---- state model (spec §18) ----

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct BrowserTab {
    pub id: String,
    pub title: String,
    pub url: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub favicon_url: Option<String>,
    pub loading: bool,
    pub can_go_back: bool,
    pub can_go_forward: bool,
    /// Internal targets (e.g. the Page Agent Hub tab) hidden from the tab strip.
    pub internal: bool,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct BrowserTaskState {
    pub id: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub conversation_id: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub run_id: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub tool_call_id: Option<String>,
    pub description: String,
    pub status: TaskStatus,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub started_at: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub activity: Option<String>,
}

/// Snapshot served by `GET /api/browser/state` and `browser.state` events.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct BrowserState {
    pub installed: bool,
    pub install_status: InstallStatus,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub install_error: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub install_progress: Option<f64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub installed_version: Option<String>,
    pub process_status: ProcessStatus,
    pub profile_id: String,
    pub panel_mode: BrowserPanelMode,
    pub panel_width: u32,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub previous_panel_width: Option<u32>,
    pub connected: bool,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub active_tab_id: Option<String>,
    pub tabs: Vec<BrowserTab>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub task: Option<BrowserTaskState>,
    pub manual_control_enabled: bool,
    pub remote_viewer_count: u32,
    #[serde(default)]
    pub pending_approvals: Vec<PendingApproval>,
}

/// A permission request surfaced to the UI dialog (spec §11.2).
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct PendingApproval {
    pub id: String,
    pub capability: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub origin: Option<String>,
    pub description: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub task_id: Option<String>,
    pub created_at: String,
}

// ---- permissions (spec §11) ----

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum PermissionCapability {
    NavigatePublicWeb,
    NavigatePrivateNetwork,
    UploadFile,
    DownloadFile,
    SubmitForm,
    SendMessage,
    PublishContent,
    DeleteContent,
    FinancialTransaction,
    CredentialEntry,
}

impl PermissionCapability {
    pub fn as_str(&self) -> &'static str {
        match self {
            Self::NavigatePublicWeb => "navigate_public_web",
            Self::NavigatePrivateNetwork => "navigate_private_network",
            Self::UploadFile => "upload_file",
            Self::DownloadFile => "download_file",
            Self::SubmitForm => "submit_form",
            Self::SendMessage => "send_message",
            Self::PublishContent => "publish_content",
            Self::DeleteContent => "delete_content",
            Self::FinancialTransaction => "financial_transaction",
            Self::CredentialEntry => "credential_entry",
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum PermissionScope {
    Once,
    Task,
    Conversation,
    Origin,
    Profile,
}

impl PermissionScope {
    pub fn as_str(&self) -> &'static str {
        match self {
            Self::Once => "once",
            Self::Task => "task",
            Self::Conversation => "conversation",
            Self::Origin => "origin",
            Self::Profile => "profile",
        }
    }
}

// ---- stream events (spec §9.3) ----

/// Server → viewer text events on `/api/browser/stream`.
#[derive(Debug, Clone, Serialize)]
#[serde(tag = "type", content = "payload")]
pub enum StreamEvent {
    #[serde(rename = "browser.state")]
    State(Box<BrowserState>),
    #[serde(rename = "browser.tab.created")]
    TabCreated(BrowserTab),
    #[serde(rename = "browser.tab.updated")]
    TabUpdated(BrowserTab),
    #[serde(rename = "browser.tab.closed")]
    TabClosed(serde_json::Value),
    #[serde(rename = "browser.navigation")]
    Navigation(serde_json::Value),
    #[serde(rename = "browser.task.started")]
    TaskStarted(BrowserTaskState),
    #[serde(rename = "browser.task.activity")]
    TaskActivity(serde_json::Value),
    #[serde(rename = "browser.task.finished")]
    TaskFinished(serde_json::Value),
    #[serde(rename = "browser.task.failed")]
    TaskFailed(serde_json::Value),
    #[serde(rename = "browser.file_chooser")]
    FileChooser(serde_json::Value),
    #[serde(rename = "browser.download")]
    Download(serde_json::Value),
    #[serde(rename = "browser.crashed")]
    Crashed(serde_json::Value),
}

// ---- client commands (spec §9.3) ----

#[derive(Debug, Clone, Copy, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct ViewportSize {
    pub width: u32,
    pub height: u32,
    /// Device pixel ratio of the viewer surface.
    #[serde(default = "default_scale")]
    pub device_scale_factor: f64,
}

fn default_scale() -> f64 {
    1.0
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize, Default)]
#[serde(rename_all = "camelCase")]
pub enum MouseButton {
    #[default]
    None,
    Left,
    Middle,
    Right,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub enum MouseEventKind {
    Move,
    Down,
    Up,
}

#[derive(Debug, Clone, Copy, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct MouseInput {
    pub kind: MouseEventKind,
    /// Panel coordinates in CSS pixels.
    pub x: f64,
    pub y: f64,
    #[serde(default)]
    pub button: MouseButton,
    #[serde(default)]
    pub click_count: u32,
    /// Modifier bitmask (Alt=1, Ctrl=2, Meta=4, Shift=8), CDP convention.
    #[serde(default)]
    pub modifiers: u32,
}

#[derive(Debug, Clone, Copy, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct WheelInput {
    pub x: f64,
    pub y: f64,
    pub delta_x: f64,
    pub delta_y: f64,
    #[serde(default)]
    pub modifiers: u32,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct KeyInput {
    /// `rawKeyDown` | `keyDown` | `keyUp` | `char`.
    pub kind: String,
    #[serde(default)]
    pub key: String,
    #[serde(default)]
    pub code: String,
    #[serde(default)]
    pub text: String,
    #[serde(default)]
    pub windows_virtual_key_code: u32,
    #[serde(default)]
    pub modifiers: u32,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct TextInput {
    pub text: String,
}

/// Viewer → server commands on `/api/browser/stream`.
#[derive(Debug, Clone, Deserialize)]
#[serde(tag = "type")]
pub enum ClientCommand {
    #[serde(rename = "input.mouse")]
    Mouse { payload: MouseInput },
    #[serde(rename = "input.wheel")]
    Wheel { payload: WheelInput },
    #[serde(rename = "input.key")]
    Key { payload: KeyInput },
    #[serde(rename = "input.text")]
    Text { payload: TextInput },
    #[serde(rename = "viewport.resize")]
    ViewportResize { payload: ViewportSize },
    #[serde(rename = "frame.ack")]
    FrameAck { frame_id: u64 },
    #[serde(rename = "tab.activate")]
    TabActivate { tab_id: String },
}

// ---- binary screencast frame codec (spec §9.3) ----

/// Wire version of the binary frame header.
pub const FRAME_VERSION: u8 = 1;
/// version(1) + frame_id(8) + width(4) + height(4) + format(1), big-endian.
pub const FRAME_HEADER_LEN: usize = 18;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum FrameFormat {
    Jpeg,
    Webp,
}

impl FrameFormat {
    fn from_byte(b: u8) -> Option<Self> {
        match b {
            1 => Some(Self::Jpeg),
            2 => Some(Self::Webp),
            _ => None,
        }
    }

    fn as_byte(&self) -> u8 {
        match self {
            Self::Jpeg => 1,
            Self::Webp => 2,
        }
    }
}

/// One screencast frame, shared with every viewer via `Arc`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Frame {
    pub frame_id: u64,
    pub width: u32,
    pub height: u32,
    pub format: FrameFormat,
    pub data: Bytes,
}

#[derive(Debug, thiserror::Error, PartialEq, Eq)]
pub enum FrameCodecError {
    #[error("frame too short: {0} bytes")]
    TooShort(usize),
    #[error("unsupported frame version {0}")]
    UnsupportedVersion(u8),
    #[error("unsupported frame format byte {0}")]
    UnsupportedFormat(u8),
}

impl Frame {
    /// `[u8 version=1][u64 frame_id][u32 width][u32 height][u8 format][image]`
    /// — all integers big-endian.
    pub fn encode(&self) -> Vec<u8> {
        let mut out = Vec::with_capacity(FRAME_HEADER_LEN + self.data.len());
        out.push(FRAME_VERSION);
        out.extend_from_slice(&self.frame_id.to_be_bytes());
        out.extend_from_slice(&self.width.to_be_bytes());
        out.extend_from_slice(&self.height.to_be_bytes());
        out.push(self.format.as_byte());
        out.extend_from_slice(&self.data);
        out
    }

    pub fn decode(bytes: &[u8]) -> Result<Self, FrameCodecError> {
        if bytes.len() < FRAME_HEADER_LEN {
            return Err(FrameCodecError::TooShort(bytes.len()));
        }
        let version = bytes[0];
        if version != FRAME_VERSION {
            return Err(FrameCodecError::UnsupportedVersion(version));
        }
        let frame_id = u64::from_be_bytes(bytes[1..9].try_into().expect("slice len checked"));
        let width = u32::from_be_bytes(bytes[9..13].try_into().expect("slice len checked"));
        let height = u32::from_be_bytes(bytes[13..17].try_into().expect("slice len checked"));
        let format = FrameFormat::from_byte(bytes[17])
            .ok_or(FrameCodecError::UnsupportedFormat(bytes[17]))?;
        Ok(Self {
            frame_id,
            width,
            height,
            format,
            data: Bytes::copy_from_slice(&bytes[FRAME_HEADER_LEN..]),
        })
    }
}

// ---- stable error codes (spec §7.3) ----

pub mod error_codes {
    pub const BROWSER_NOT_INSTALLED: &str = "BROWSER_NOT_INSTALLED";
    pub const BROWSER_START_FAILED: &str = "BROWSER_START_FAILED";
    pub const PROFILE_LOCKED: &str = "PROFILE_LOCKED";
    pub const PAGE_AGENT_NOT_CONNECTED: &str = "PAGE_AGENT_NOT_CONNECTED";
    pub const TASK_BUSY: &str = "TASK_BUSY";
    pub const TASK_TIMEOUT: &str = "TASK_TIMEOUT";
    pub const TASK_CANCELLED: &str = "TASK_CANCELLED";
    pub const NAVIGATION_BLOCKED: &str = "NAVIGATION_BLOCKED";
    pub const ORIGIN_PERMISSION_REQUIRED: &str = "ORIGIN_PERMISSION_REQUIRED";
    pub const FILE_PERMISSION_REQUIRED: &str = "FILE_PERMISSION_REQUIRED";
    pub const FILE_NOT_FOUND: &str = "FILE_NOT_FOUND";
    pub const DOWNLOAD_BLOCKED: &str = "DOWNLOAD_BLOCKED";
    pub const CDP_DISCONNECTED: &str = "CDP_DISCONNECTED";
    pub const BROWSER_CRASHED: &str = "BROWSER_CRASHED";
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn frame_codec_roundtrip() {
        let frame = Frame {
            frame_id: 42,
            width: 1280,
            height: 720,
            format: FrameFormat::Jpeg,
            data: Bytes::from_static(b"\xff\xd8\xff\xe0jpeg-bytes"),
        };
        let encoded = frame.encode();
        assert_eq!(encoded[0], FRAME_VERSION);
        assert_eq!(encoded.len(), FRAME_HEADER_LEN + 14);
        let decoded = Frame::decode(&encoded).expect("decode");
        assert_eq!(decoded, frame);
    }

    #[test]
    fn frame_codec_webp_and_empty_payload() {
        let frame = Frame {
            frame_id: u64::MAX,
            width: 0,
            height: 0,
            format: FrameFormat::Webp,
            data: Bytes::new(),
        };
        let decoded = Frame::decode(&frame.encode()).expect("decode");
        assert_eq!(decoded, frame);
    }

    #[test]
    fn frame_codec_rejects_bad_input() {
        assert_eq!(Frame::decode(&[1, 2, 3]), Err(FrameCodecError::TooShort(3)));
        let mut bad_version = vec![9u8];
        bad_version.extend(std::iter::repeat_n(0, FRAME_HEADER_LEN - 1));
        assert_eq!(
            Frame::decode(&bad_version),
            Err(FrameCodecError::UnsupportedVersion(9))
        );
        let mut bad_format = vec![FRAME_VERSION];
        bad_format.extend(std::iter::repeat_n(0, 16));
        bad_format.push(77);
        assert_eq!(
            Frame::decode(&bad_format),
            Err(FrameCodecError::UnsupportedFormat(77))
        );
    }

    #[test]
    fn panel_mode_strings_roundtrip() {
        for mode in [
            BrowserPanelMode::Hidden,
            BrowserPanelMode::Compact,
            BrowserPanelMode::Split,
            BrowserPanelMode::Expanded,
            BrowserPanelMode::Focus,
        ] {
            assert_eq!(BrowserPanelMode::parse(mode.as_str()), Some(mode));
        }
        assert_eq!(BrowserPanelMode::parse("fullscreen"), None);
    }

    #[test]
    fn task_status_activity() {
        assert!(TaskStatus::Running.is_active());
        assert!(TaskStatus::AwaitingApproval.is_active());
        assert!(!TaskStatus::Completed.is_active());
        assert!(!TaskStatus::Cancelled.is_active());
    }

    #[test]
    fn stream_event_serializes_with_spec_type_names() {
        let state = BrowserState {
            installed: false,
            install_status: InstallStatus::NotInstalled,
            install_error: None,
            install_progress: None,
            installed_version: None,
            process_status: ProcessStatus::Stopped,
            profile_id: "default".into(),
            panel_mode: BrowserPanelMode::Hidden,
            panel_width: 640,
            previous_panel_width: None,
            connected: false,
            active_tab_id: None,
            tabs: vec![],
            task: None,
            manual_control_enabled: true,
            remote_viewer_count: 0,
            pending_approvals: vec![],
        };
        let json = serde_json::to_value(StreamEvent::State(Box::new(state))).unwrap();
        assert_eq!(json["type"], "browser.state");
        assert_eq!(json["payload"]["installStatus"], "not_installed");
        assert_eq!(json["payload"]["panelMode"], "hidden");
        assert_eq!(json["payload"]["manualControlEnabled"], true);
    }

    #[test]
    fn client_commands_parse_spec_shapes() {
        let ack: ClientCommand =
            serde_json::from_str(r#"{"type":"frame.ack","frame_id":123}"#).unwrap();
        assert!(matches!(ack, ClientCommand::FrameAck { frame_id: 123 }));

        let activate: ClientCommand =
            serde_json::from_str(r#"{"type":"tab.activate","tab_id":"t-1"}"#).unwrap();
        assert!(matches!(activate, ClientCommand::TabActivate { tab_id } if tab_id == "t-1"));

        let mouse: ClientCommand = serde_json::from_str(
            r#"{"type":"input.mouse","payload":{"kind":"down","x":10.5,"y":20,"button":"left","clickCount":2}}"#,
        )
        .unwrap();
        match mouse {
            ClientCommand::Mouse { payload } => {
                assert_eq!(payload.kind, MouseEventKind::Down);
                assert_eq!(payload.button, MouseButton::Left);
                assert_eq!(payload.click_count, 2);
                assert!((payload.x - 10.5).abs() < f64::EPSILON);
            }
            _ => panic!("expected mouse command"),
        }
    }
}
