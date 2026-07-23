import { create } from "zustand";
import { updateBrowserPanel } from "./browserApi";
import {
  isTaskActiveStatus,
  type BrowserPanelMode,
  type BrowserState,
  type BrowserTab,
  type BrowserTaskState,
  type InstallStatus,
  type PendingApproval,
  type ProcessStatus,
  type TaskStatus,
} from "./types";

// ---- width rules (spec §2.3) ----

export const MIN_BROWSER_WIDTH = 320;
export const MIN_CONTENT_WIDTH = 420;
export const COMPACT_WIDTH = 360;
export const SPLIT_DEFAULT_RATIO = 0.45;
export const EXPANDED_RATIO = 0.65;

/** Mirror of `manager::clamp_panel_width` on the server. */
export function clampPanelWidth(width: number, containerWidth: number): number {
  const max = Math.max(containerWidth - MIN_CONTENT_WIDTH, MIN_BROWSER_WIDTH);
  return Math.min(Math.max(Math.round(width), MIN_BROWSER_WIDTH), max);
}

/**
 * When the window is too narrow to satisfy both minimums the panel becomes a
 * full-height overlay instead of crushing the chat (spec §2.3).
 */
export function isOverlayContainer(containerWidth: number): boolean {
  return (
    containerWidth > 0 &&
    containerWidth < MIN_BROWSER_WIDTH + MIN_CONTENT_WIDTH
  );
}

/** Effective pixel width of the panel for a mode within the content region. */
export function effectivePanelWidth(
  mode: BrowserPanelMode,
  panelWidth: number,
  containerWidth: number,
): number {
  if (mode === "hidden") return 0;
  if (containerWidth <= 0) return COMPACT_WIDTH;
  switch (mode) {
    case "compact":
      return clampPanelWidth(COMPACT_WIDTH, containerWidth);
    case "split":
      return clampPanelWidth(
        panelWidth > 0 ? panelWidth : containerWidth * SPLIT_DEFAULT_RATIO,
        containerWidth,
      );
    case "expanded":
      return clampPanelWidth(containerWidth * EXPANDED_RATIO, containerWidth);
    case "focus":
      return containerWidth;
  }
}

// ---- persistence (POST /api/browser/panel, debounced) ----

export interface PanelPersistInput {
  mode: BrowserPanelMode;
  width: number;
  containerWidth?: number;
}

type PanelPersister = (input: PanelPersistInput) => void;

let persister: PanelPersister = (input) => {
  updateBrowserPanel({
    mode: input.mode,
    width: input.width,
    containerWidth: input.containerWidth,
  }).catch(() => {
    /* panel prefs persist is best-effort; the stream re-syncs state */
  });
};

/** Tests swap the persister to avoid network calls. */
export function setPanelPersister(next: PanelPersister): void {
  persister = next;
}

const PERSIST_DEBOUNCE_MS = 400;
let persistTimer: ReturnType<typeof setTimeout> | null = null;

function schedulePersist(get: () => BrowserStoreState): void {
  if (persistTimer !== null) clearTimeout(persistTimer);
  persistTimer = setTimeout(() => {
    persistTimer = null;
    const s = get();
    persister({
      mode: s.mode,
      width: s.panelWidth,
      containerWidth: s.containerWidth > 0 ? s.containerWidth : undefined,
    });
  }, PERSIST_DEBOUNCE_MS);
}

// ---- store ----

export interface FrameInfo {
  url: string;
  width: number;
  height: number;
  frameId: number;
}

interface BrowserStoreState {
  // panel prefs (persisted server-side)
  mode: BrowserPanelMode;
  panelWidth: number;
  previousPanelWidth: number | null;
  /** Last visible mode — what Hide → reopen restores. */
  lastVisibleMode: Exclude<BrowserPanelMode, "hidden">;
  // runtime state (server-owned; stream wins after connect)
  installed: boolean;
  installStatus: InstallStatus;
  installError: string | null;
  installProgress: number | null;
  installedVersion: string | null;
  processStatus: ProcessStatus;
  profileId: string;
  connected: boolean;
  activeTabId: string | null;
  tabs: BrowserTab[];
  task: BrowserTaskState | null;
  manualControlEnabled: boolean;
  remoteViewerCount: number;
  pendingApprovals: PendingApproval[];
  // local-only UI state
  streamConnected: boolean;
  frame: FrameInfo | null;
  dragging: boolean;
  containerWidth: number;
  addressFocusNonce: number;
  /** Spec §16: keep the panel hidden while an agent task runs. */
  keepHiddenDuringAutomation: boolean;

  // actions
  applyServerState: (state: BrowserState) => void;
  upsertTab: (tab: BrowserTab) => void;
  removeTab: (id: string) => void;
  setActiveTabUrl: (url: string) => void;
  setTask: (task: BrowserTaskState | null) => void;
  setTaskActivity: (activity: string) => void;
  setTaskStatus: (status: TaskStatus) => void;
  setProcessStatus: (status: ProcessStatus) => void;
  setFrame: (frame: FrameInfo | null) => void;
  setStreamConnected: (connected: boolean) => void;
  setDragging: (dragging: boolean) => void;
  setContainerWidth: (width: number) => void;
  setMode: (mode: BrowserPanelMode) => void;
  open: () => void;
  hide: () => void;
  shrink: () => void;
  expand: () => void;
  setSplitWidth: (width: number) => void;
  /** Double-click on the splitter: compact ↔ 50/50 split (spec §2.2). */
  toggleCompactSplit: () => void;
  requestAddressFocus: () => void;
  setKeepHiddenDuringAutomation: (keep: boolean) => void;
}

const initialState = {
  mode: "hidden" as BrowserPanelMode,
  panelWidth: 640,
  previousPanelWidth: null as number | null,
  lastVisibleMode: "split" as Exclude<BrowserPanelMode, "hidden">,
  installed: false,
  installStatus: "not_installed" as InstallStatus,
  installError: null as string | null,
  installProgress: null as number | null,
  installedVersion: null as string | null,
  processStatus: "stopped" as ProcessStatus,
  profileId: "default",
  connected: false,
  activeTabId: null as string | null,
  tabs: [] as BrowserTab[],
  task: null as BrowserTaskState | null,
  manualControlEnabled: true,
  remoteViewerCount: 0,
  pendingApprovals: [] as PendingApproval[],
  streamConnected: false,
  frame: null as FrameInfo | null,
  dragging: false,
  containerWidth: 0,
  addressFocusNonce: 0,
  keepHiddenDuringAutomation: false,
};

/** Test hook: restore the store to its initial state. */
export function resetBrowserStore(): void {
  if (persistTimer !== null) {
    clearTimeout(persistTimer);
    persistTimer = null;
  }
  useBrowserStore.setState(initialState);
}

export const useBrowserStore = create<BrowserStoreState>((set, get) => ({
  ...initialState,

  applyServerState: (state) =>
    set((s) => {
      // While the user drags the splitter, don't let an in-flight server
      // snapshot snap the width/mode back (spec §2.2: selected width persists).
      const panelFields = s.dragging
        ? {}
        : {
            mode: state.panelMode,
            panelWidth: state.panelWidth,
            previousPanelWidth: state.previousPanelWidth ?? null,
            lastVisibleMode:
              state.panelMode === "hidden"
                ? s.lastVisibleMode
                : state.panelMode,
          };
      return {
        ...panelFields,
        installed: state.installed,
        installStatus: state.installStatus,
        installError: state.installError ?? null,
        installProgress: state.installProgress ?? null,
        installedVersion: state.installedVersion ?? null,
        processStatus: state.processStatus,
        profileId: state.profileId,
        connected: state.connected,
        activeTabId: state.activeTabId ?? null,
        tabs: state.tabs,
        task: state.task ?? null,
        manualControlEnabled: state.manualControlEnabled,
        remoteViewerCount: state.remoteViewerCount,
        pendingApprovals: state.pendingApprovals ?? [],
      };
    }),

  upsertTab: (tab) =>
    set((s) => {
      const index = s.tabs.findIndex((t) => t.id === tab.id);
      if (index === -1) return { tabs: [...s.tabs, tab] };
      const tabs = s.tabs.slice();
      tabs[index] = tab;
      return { tabs };
    }),

  removeTab: (id) =>
    set((s) => ({
      tabs: s.tabs.filter((t) => t.id !== id),
      activeTabId: s.activeTabId === id ? null : s.activeTabId,
    })),

  setActiveTabUrl: (url) =>
    set((s) => {
      if (!s.activeTabId) return {};
      const index = s.tabs.findIndex((t) => t.id === s.activeTabId);
      const current = index === -1 ? undefined : s.tabs[index];
      if (!current) return {};
      const tabs = s.tabs.slice();
      tabs[index] = { ...current, url };
      return { tabs };
    }),

  setTask: (task) => set({ task }),

  setTaskActivity: (activity) =>
    set((s) => (s.task ? { task: { ...s.task, activity } } : {})),

  setTaskStatus: (status) =>
    set((s) => (s.task ? { task: { ...s.task, status } } : {})),

  setProcessStatus: (processStatus) => set({ processStatus }),

  setFrame: (frame) => set({ frame }),

  setStreamConnected: (streamConnected) => set({ streamConnected }),

  setDragging: (dragging) => set({ dragging }),

  setContainerWidth: (containerWidth) => set({ containerWidth }),

  setMode: (mode) => {
    set((s) => ({
      mode,
      lastVisibleMode: mode === "hidden" ? s.lastVisibleMode : mode,
    }));
    schedulePersist(get);
  },

  open: () => {
    get().setMode(get().lastVisibleMode);
  },

  hide: () => {
    get().setMode("hidden");
  },

  /** Shrink: compact ↔ restore previous custom width (spec §2.2). */
  shrink: () => {
    const s = get();
    if (s.mode === "compact") {
      const restored = s.previousPanelWidth ?? s.panelWidth;
      set({
        mode: "split",
        lastVisibleMode: "split",
        panelWidth:
          restored > 0
            ? clampPanelWidth(restored, s.containerWidth || restored)
            : s.containerWidth > 0
              ? clampPanelWidth(s.containerWidth * SPLIT_DEFAULT_RATIO, s.containerWidth)
              : 640,
      });
    } else {
      set({
        previousPanelWidth: effectivePanelWidth(
          s.mode,
          s.panelWidth,
          s.containerWidth,
        ),
        mode: "compact",
        lastVisibleMode: "compact",
      });
    }
    schedulePersist(get);
  },

  /** Expand: → large preset → focus; from focus back to large (spec §2.2). */
  expand: () => {
    const s = get();
    const next: BrowserPanelMode =
      s.mode === "focus"
        ? "expanded"
        : s.mode === "expanded"
          ? "focus"
          : "expanded";
    set({ mode: next, lastVisibleMode: next });
    schedulePersist(get);
  },

  setSplitWidth: (width) => {
    const s = get();
    set({
      mode: "split",
      lastVisibleMode: "split",
      panelWidth:
        s.containerWidth > 0
          ? clampPanelWidth(width, s.containerWidth)
          : Math.max(MIN_BROWSER_WIDTH, Math.round(width)),
    });
    schedulePersist(get);
  },

  toggleCompactSplit: () => {
    const s = get();
    if (s.mode === "compact") {
      // compact → 50/50 split
      const width =
        s.containerWidth > 0
          ? clampPanelWidth(s.containerWidth * 0.5, s.containerWidth)
          : Math.max(MIN_BROWSER_WIDTH, s.panelWidth);
      set({ mode: "split", lastVisibleMode: "split", panelWidth: width });
    } else {
      // any visible mode → compact, remembering the current width
      set({
        previousPanelWidth: effectivePanelWidth(
          s.mode,
          s.panelWidth,
          s.containerWidth,
        ),
        mode: "compact",
        lastVisibleMode: "compact",
      });
    }
    schedulePersist(get);
  },

  requestAddressFocus: () =>
    set((s) => ({ addressFocusNonce: s.addressFocusNonce + 1 })),

  setKeepHiddenDuringAutomation: (keep) =>
    set({ keepHiddenDuringAutomation: keep }),
}));

// ---- derived helpers ----

export function selectTaskActive(s: {
  task: BrowserTaskState | null;
  manualControlEnabled: boolean;
}): boolean {
  return s.task !== null && isTaskActiveStatus(s.task.status);
}

/** Manual input is blocked while the agent owns the tab (spec §2.5/§10.2). */
export function selectInputBlocked(s: {
  task: BrowserTaskState | null;
  manualControlEnabled: boolean;
}): boolean {
  return selectTaskActive(s) && !s.manualControlEnabled;
}

/** Visible (non-internal) tabs for the tab strip. */
export function selectVisibleTabs(s: { tabs: BrowserTab[] }): BrowserTab[] {
  return s.tabs.filter((t) => !t.internal);
}
