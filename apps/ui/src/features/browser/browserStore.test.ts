import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  clampPanelWidth,
  COMPACT_WIDTH,
  effectivePanelWidth,
  isOverlayContainer,
  MIN_BROWSER_WIDTH,
  MIN_CONTENT_WIDTH,
  resetBrowserStore,
  setPanelPersister,
  useBrowserStore,
} from "./browserStore";
import type { BrowserState } from "./types";

function serverState(overrides: Partial<BrowserState> = {}): BrowserState {
  return {
    installed: true,
    installStatus: "ready",
    processStatus: "running",
    profileId: "default",
    panelMode: "split",
    panelWidth: 700,
    previousPanelWidth: null,
    connected: true,
    activeTabId: "tab-1",
    tabs: [
      {
        id: "tab-1",
        title: "Example",
        url: "https://example.com",
        loading: false,
        canGoBack: true,
        canGoForward: false,
        internal: false,
      },
    ],
    task: null,
    manualControlEnabled: true,
    remoteViewerCount: 0,
    pendingApprovals: [],
    ...overrides,
  };
}

beforeEach(() => {
  resetBrowserStore();
  setPanelPersister(() => {});
});

afterEach(() => {
  setPanelPersister(() => {});
});

describe("clampPanelWidth", () => {
  it("clamps to the minimum browser width", () => {
    expect(clampPanelWidth(100, 2000)).toBe(MIN_BROWSER_WIDTH);
  });

  it("clamps so the center content keeps its minimum width", () => {
    expect(clampPanelWidth(1900, 2000)).toBe(2000 - MIN_CONTENT_WIDTH);
  });

  it("never drops below the minimum even in a tiny container", () => {
    expect(clampPanelWidth(500, 500)).toBe(MIN_BROWSER_WIDTH);
  });
});

describe("effectivePanelWidth", () => {
  it("hidden is zero", () => {
    expect(effectivePanelWidth("hidden", 640, 2000)).toBe(0);
  });

  it("compact is the clamped compact preset", () => {
    expect(effectivePanelWidth("compact", 640, 2000)).toBe(COMPACT_WIDTH);
  });

  it("split uses the stored width and defaults to 45% of the region", () => {
    expect(effectivePanelWidth("split", 700, 2000)).toBe(700);
    expect(effectivePanelWidth("split", 0, 2000)).toBe(900);
  });

  it("expanded is 65% of the region, clamped", () => {
    expect(effectivePanelWidth("expanded", 640, 2000)).toBe(1300);
    // 65% of 800 = 520, but the center content keeps its 420px minimum.
    expect(effectivePanelWidth("expanded", 640, 800)).toBe(380);
  });

  it("focus takes the entire region", () => {
    expect(effectivePanelWidth("focus", 640, 2000)).toBe(2000);
  });
});

describe("isOverlayContainer", () => {
  it("switches to overlay when both minimums cannot be satisfied", () => {
    expect(isOverlayContainer(MIN_BROWSER_WIDTH + MIN_CONTENT_WIDTH - 1)).toBe(true);
    expect(isOverlayContainer(MIN_BROWSER_WIDTH + MIN_CONTENT_WIDTH)).toBe(false);
    expect(isOverlayContainer(0)).toBe(false);
  });
});

describe("panel mode transitions", () => {
  it("hide → open restores the last visible mode", () => {
    const store = useBrowserStore.getState();
    store.setMode("expanded");
    store.hide();
    expect(useBrowserStore.getState().mode).toBe("hidden");
    useBrowserStore.getState().open();
    expect(useBrowserStore.getState().mode).toBe("expanded");
  });

  it("shrink: any mode → compact → restores previous custom width", () => {
    const store = useBrowserStore.getState();
    store.setContainerWidth(2000);
    store.setSplitWidth(700);
    store.shrink();
    let s = useBrowserStore.getState();
    expect(s.mode).toBe("compact");
    expect(s.previousPanelWidth).toBe(700);
    s.shrink();
    s = useBrowserStore.getState();
    expect(s.mode).toBe("split");
    expect(s.panelWidth).toBe(700);
  });

  it("expand: split → expanded → focus → expanded", () => {
    const store = useBrowserStore.getState();
    store.setMode("split");
    store.expand();
    expect(useBrowserStore.getState().mode).toBe("expanded");
    useBrowserStore.getState().expand();
    expect(useBrowserStore.getState().mode).toBe("focus");
    useBrowserStore.getState().expand();
    expect(useBrowserStore.getState().mode).toBe("expanded");
  });

  it("double-click splitter toggles compact ↔ 50/50 split", () => {
    const store = useBrowserStore.getState();
    store.setContainerWidth(2000);
    store.setMode("split");
    store.toggleCompactSplit();
    expect(useBrowserStore.getState().mode).toBe("compact");
    useBrowserStore.getState().toggleCompactSplit();
    const s = useBrowserStore.getState();
    expect(s.mode).toBe("split");
    expect(s.panelWidth).toBe(1000);
  });

  it("setSplitWidth clamps to the container", () => {
    const store = useBrowserStore.getState();
    store.setContainerWidth(1000);
    store.setSplitWidth(900);
    expect(useBrowserStore.getState().panelWidth).toBe(1000 - MIN_CONTENT_WIDTH);
    store.setSplitWidth(10);
    expect(useBrowserStore.getState().panelWidth).toBe(MIN_BROWSER_WIDTH);
  });
});

describe("hydration from server state", () => {
  it("applies a full state snapshot", () => {
    useBrowserStore.getState().applyServerState(
      serverState({
        pendingApprovals: [
          {
            id: "a-1",
            capability: "upload_file",
            origin: "https://example.com",
            description: "Upload 1 file(s): resume.pdf",
            createdAt: "2026-01-01T00:00:00Z",
          },
        ],
      }),
    );
    const s = useBrowserStore.getState();
    expect(s.mode).toBe("split");
    expect(s.panelWidth).toBe(700);
    expect(s.processStatus).toBe("running");
    expect(s.tabs).toHaveLength(1);
    expect(s.pendingApprovals).toHaveLength(1);
    expect(s.pendingApprovals[0]?.capability).toBe("upload_file");
  });

  it("does not override panel fields while the user is dragging", () => {
    const store = useBrowserStore.getState();
    store.setContainerWidth(2000);
    store.setSplitWidth(800);
    store.setDragging(true);
    store.applyServerState(serverState({ panelWidth: 400, panelMode: "compact" }));
    const s = useBrowserStore.getState();
    expect(s.mode).toBe("split");
    expect(s.panelWidth).toBe(800);
    // Runtime fields still update while dragging.
    expect(s.processStatus).toBe("running");
  });

  it("tracks the last visible mode from server snapshots", () => {
    useBrowserStore.getState().applyServerState(serverState({ panelMode: "expanded" }));
    useBrowserStore.getState().applyServerState(serverState({ panelMode: "hidden" }));
    const s = useBrowserStore.getState();
    expect(s.mode).toBe("hidden");
    expect(s.lastVisibleMode).toBe("expanded");
  });
});

describe("panel persistence", () => {
  it("debounces POST /api/browser/panel through the injected persister", () => {
    vi.useFakeTimers();
    try {
      const persist = vi.fn();
      setPanelPersister(persist);
      const store = useBrowserStore.getState();
      store.setContainerWidth(2000);
      store.setSplitWidth(700);
      store.setMode("compact");
      expect(persist).not.toHaveBeenCalled();
      vi.advanceTimersByTime(500);
      expect(persist).toHaveBeenCalledTimes(1);
      expect(persist).toHaveBeenCalledWith({
        mode: "compact",
        width: 700,
        containerWidth: 2000,
      });
    } finally {
      vi.useRealTimers();
    }
  });
});
