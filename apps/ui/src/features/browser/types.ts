/**
 * Wire types for the Native GPT Browser UI (spec §9.2/§9.3/§18).
 * These mirror `crates/server/src/browser/protocol.rs` exactly — field names
 * and serde casing included. Do not drift from the server definitions.
 */

// ---- panel / install / process / task states ----

export type BrowserPanelMode =
  | "hidden"
  | "compact"
  | "split"
  | "expanded"
  | "focus";

export type InstallStatus =
  | "not_installed"
  | "downloading"
  | "verifying"
  | "extracting"
  | "ready"
  | "error";

export type ProcessStatus =
  | "stopped"
  | "starting"
  | "running"
  | "stopping"
  | "crashed";

export type TaskStatus =
  | "awaiting_approval"
  | "starting"
  | "running"
  | "paused_for_user"
  | "stopping"
  | "completed"
  | "failed"
  | "cancelled";

/** Mirrors `TaskStatus::is_active` (spec §19): the task holds the profile slot. */
export function isTaskActiveStatus(status: TaskStatus): boolean {
  return (
    status === "awaiting_approval" ||
    status === "starting" ||
    status === "running" ||
    status === "paused_for_user" ||
    status === "stopping"
  );
}

// ---- state model (spec §18) ----

export interface BrowserTab {
  id: string;
  title: string;
  url: string;
  faviconUrl?: string | null;
  loading: boolean;
  canGoBack: boolean;
  canGoForward: boolean;
  /** Internal targets (e.g. the Page Agent Hub tab) hidden from the tab strip. */
  internal: boolean;
}

export interface BrowserTaskState {
  id: string;
  conversationId?: string | null;
  runId?: string | null;
  toolCallId?: string | null;
  description: string;
  status: TaskStatus;
  startedAt?: string | null;
  activity?: string | null;
}

export type PermissionCapability =
  | "navigate_public_web"
  | "navigate_private_network"
  | "upload_file"
  | "download_file"
  | "submit_form"
  | "send_message"
  | "publish_content"
  | "delete_content"
  | "financial_transaction"
  | "credential_entry"
  | (string & {});

export type PermissionScope = "once" | "task" | "conversation" | "origin" | "profile";

export interface PendingApproval {
  id: string;
  capability: PermissionCapability;
  origin?: string | null;
  description: string;
  taskId?: string | null;
  createdAt: string;
}

/** Snapshot served by `GET /api/browser/state` and `browser.state` events. */
export interface BrowserState {
  installed: boolean;
  installStatus: InstallStatus;
  installError?: string | null;
  installProgress?: number | null;
  installedVersion?: string | null;
  processStatus: ProcessStatus;
  profileId: string;
  panelMode: BrowserPanelMode;
  panelWidth: number;
  previousPanelWidth?: number | null;
  connected: boolean;
  activeTabId?: string | null;
  tabs: BrowserTab[];
  task?: BrowserTaskState | null;
  manualControlEnabled: boolean;
  remoteViewerCount: number;
  pendingApprovals: PendingApproval[];
}

// ---- component + profiles (mod.rs handlers) ----

export interface BrowserComponentInfo {
  status: InstallStatus;
  progress: number | null;
  error: string | null;
  installed: boolean;
  installedVersion: string | null;
  availableVersion: string;
  pageAgentExtensionId: string;
}

export interface BrowserProfile {
  id: string;
  name: string;
  engine: string;
  executablePath: string | null;
  profilePath: string;
  createdAt: string;
  updatedAt: string;
  lastUsedAt: string | null;
}

/** Response of `POST /api/browser/panel` (prefs_json in mod.rs). */
export interface BrowserPanelPrefs {
  profileId: string;
  panelMode: string;
  panelWidth: number;
  previousPanelWidth: number | null;
  autoOpenOnToolCall: boolean;
  keepRunningWhenHidden: boolean;
  remoteStreamingEnabled: boolean;
  modelMode: "follow_conversation" | "fixed" | string;
  modelEndpointId: string | null;
  modelId: string | null;
}

// ---- stream events (server → viewer, spec §9.3) ----

export interface TabClosedPayload {
  id: string;
}

export interface NavigationPayload {
  url: string;
}

export interface TaskActivityPayload {
  taskId: string;
  activity: string;
  url?: string | null;
}

export interface TaskFinishedPayload {
  taskId: string;
  success: boolean;
}

export interface TaskFailedPayload {
  taskId: string;
  errorCode: string;
  errorMessage: string;
}

export type BrowserStreamEvent =
  | { type: "browser.state"; payload: BrowserState }
  | { type: "browser.tab.created"; payload: BrowserTab }
  | { type: "browser.tab.updated"; payload: BrowserTab }
  | { type: "browser.tab.closed"; payload: TabClosedPayload }
  | { type: "browser.navigation"; payload: NavigationPayload }
  | { type: "browser.task.started"; payload: BrowserTaskState }
  | { type: "browser.task.activity"; payload: TaskActivityPayload }
  | { type: "browser.task.finished"; payload: TaskFinishedPayload }
  | { type: "browser.task.failed"; payload: TaskFailedPayload }
  | { type: "browser.file_chooser"; payload: { sessionId?: string } }
  | {
      type: "browser.download";
      payload: {
        id: string;
        filename?: string;
        status: string;
        sourceUrl?: string | null;
        sizeBytes?: number | null;
      };
    }
  | { type: "browser.crashed"; payload: Record<string, unknown> };

// ---- client commands (viewer → server, spec §9.3) ----

export type MouseButtonName = "none" | "left" | "middle" | "right";
export type MouseEventKindName = "move" | "down" | "up";

export interface MouseInput {
  kind: MouseEventKindName;
  /** Viewport coordinates in CSS pixels. */
  x: number;
  y: number;
  button?: MouseButtonName;
  clickCount?: number;
  /** Modifier bitmask (Alt=1, Ctrl=2, Meta=4, Shift=8), CDP convention. */
  modifiers?: number;
}

export interface WheelInput {
  x: number;
  y: number;
  deltaX: number;
  deltaY: number;
  modifiers?: number;
}

export interface KeyInput {
  /** `rawKeyDown` | `keyDown` | `keyUp` | `char`. */
  kind: string;
  key?: string;
  code?: string;
  text?: string;
  windowsVirtualKeyCode?: number;
  modifiers?: number;
}

export interface TextInput {
  text: string;
}

export interface ViewportSize {
  width: number;
  height: number;
  deviceScaleFactor?: number;
}

// ---- binary screencast frame codec (spec §9.3) ----

export const FRAME_VERSION = 1;
/** version(1) + frame_id(8) + width(4) + height(4) + format(1), big-endian. */
export const FRAME_HEADER_LEN = 18;

export type FrameFormat = "jpeg" | "webp";

export interface ParsedFrame {
  frameId: number;
  width: number;
  height: number;
  format: FrameFormat;
  mime: string;
  image: Uint8Array<ArrayBuffer>;
}
