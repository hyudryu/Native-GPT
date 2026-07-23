/**
 * Native GPT Browser stream protocol (spec §9.3) — TypeScript types.
 * Hand-written to mirror schemas/browser-stream.json, which mirrors
 * crates/server/src/browser/protocol.rs serde output exactly.
 *
 * These messages travel over the dedicated `/api/browser/stream` WebSocket,
 * NOT the chat `/ws` envelope protocol — they are intentionally kept out of
 * the Envelope/messages.json contract.
 */

// ---- panel / install / process / task states ----

export type BrowserPanelMode = "hidden" | "compact" | "split" | "expanded" | "focus";

export type InstallStatus =
  | "not_installed"
  | "downloading"
  | "verifying"
  | "extracting"
  | "ready"
  | "error";

export type ProcessStatus = "stopped" | "starting" | "running" | "stopping" | "crashed";

export type TaskStatus =
  | "awaiting_approval"
  | "starting"
  | "running"
  | "paused_for_user"
  | "stopping"
  | "completed"
  | "failed"
  | "cancelled";

// ---- state model (spec §18) ----

export interface BrowserTab {
  id: string;
  title: string;
  url: string;
  faviconUrl?: string;
  loading: boolean;
  canGoBack: boolean;
  canGoForward: boolean;
  /** Internal targets (e.g. the Page Agent Hub tab) hidden from the tab strip. */
  internal: boolean;
}

export interface BrowserTaskState {
  id: string;
  conversationId?: string;
  runId?: string;
  toolCallId?: string;
  description: string;
  status: TaskStatus;
  startedAt?: string;
  activity?: string;
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
  | "credential_entry";

/** A permission request surfaced to the UI dialog (spec §11.2). */
export interface PendingApproval {
  id: string;
  capability: PermissionCapability;
  origin?: string;
  description: string;
  taskId?: string;
  createdAt: string;
}

/** Snapshot served by `GET /api/browser/state` and `browser.state` events. */
export interface BrowserState {
  installed: boolean;
  installStatus: InstallStatus;
  installError?: string;
  installProgress?: number;
  installedVersion?: string;
  processStatus: ProcessStatus;
  profileId: string;
  panelMode: BrowserPanelMode;
  panelWidth: number;
  previousPanelWidth?: number;
  connected: boolean;
  activeTabId?: string;
  tabs: BrowserTab[];
  task?: BrowserTaskState;
  manualControlEnabled: boolean;
  remoteViewerCount: number;
  pendingApprovals: PendingApproval[];
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
  url?: string;
}

export interface TaskFinishedPayload {
  taskId: string;
  success: boolean;
}

export interface TaskFailedPayload {
  taskId: string;
  /** Stable spec §7.3 code, e.g. TASK_CANCELLED, BROWSER_CRASHED. */
  errorCode: string;
  errorMessage?: string;
}

export interface FileChooserPayload {
  sessionId?: string;
}

export interface DownloadPayload {
  id: string;
  profileId?: string;
  filename?: string;
  status?: "in_progress" | "completed" | "cancelled";
  sourceUrl?: string;
  sizeBytes?: number;
}

export interface CrashedPayload {
  pid?: number;
  sessionId?: string;
  target?: boolean;
}

/**
 * Server → viewer text events on `/api/browser/stream`. Screencast frames
 * are separate binary WebSocket messages, never part of this union.
 */
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
  | { type: "browser.file_chooser"; payload: FileChooserPayload }
  | { type: "browser.download"; payload: DownloadPayload }
  | { type: "browser.crashed"; payload: CrashedPayload };

export type BrowserStreamEventType = BrowserStreamEvent["type"];

// ---- client commands (viewer → server, spec §9.3) ----

export type MouseButton = "none" | "left" | "middle" | "right";
export type MouseEventKind = "move" | "down" | "up";

export interface MouseInput {
  kind: MouseEventKind;
  /** Panel coordinates in CSS pixels. */
  x: number;
  y: number;
  button?: MouseButton;
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
  /** Device pixel ratio of the viewer surface. Defaults to 1.0 server-side. */
  deviceScaleFactor?: number;
}

/**
 * Viewer → server commands on `/api/browser/stream`. `frame.ack` and
 * `tab.activate` keep snake_case fields, mirroring the Rust `ClientCommand`
 * enum, whose variant fields are not renamed.
 */
export type BrowserClientCommand =
  | { type: "input.mouse"; payload: MouseInput }
  | { type: "input.wheel"; payload: WheelInput }
  | { type: "input.key"; payload: KeyInput }
  | { type: "input.text"; payload: TextInput }
  | { type: "viewport.resize"; payload: ViewportSize }
  | { type: "frame.ack"; frame_id: number }
  | { type: "tab.activate"; tab_id: string };

export type BrowserClientCommandType = BrowserClientCommand["type"];
