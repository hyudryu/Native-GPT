import { useEffect, useRef } from "react";
import { useBrowserState } from "./browserApi";
import { startBrowserStream } from "./browserStream";
import {
  effectivePanelWidth,
  isOverlayContainer,
  useBrowserStore,
} from "./browserStore";
import BrowserTabs from "./BrowserTabs";
import BrowserTaskBanner from "./BrowserTaskBanner";
import BrowserToolbar from "./BrowserToolbar";
import BrowserViewport from "./BrowserViewport";

/**
 * The right-side Native GPT Browser panel (spec §2/§17). Lives in AppShell so
 * it survives route changes. Renders the splitter, tab row, toolbar, task
 * banner, and viewport; owns panel-mode width math and server hydration.
 */
export default function BrowserPanel() {
  const mode = useBrowserStore((s) => s.mode);
  const panelWidth = useBrowserStore((s) => s.panelWidth);
  const containerWidth = useBrowserStore((s) => s.containerWidth);
  const setContainerWidth = useBrowserStore((s) => s.setContainerWidth);
  const setDragging = useBrowserStore((s) => s.setDragging);
  const setSplitWidth = useBrowserStore((s) => s.setSplitWidth);
  const toggleCompactSplit = useBrowserStore((s) => s.toggleCompactSplit);

  const panelRef = useRef<HTMLElement | null>(null);

  // Initial state fetch → store hydration. After the stream connects it owns
  // live updates (stream wins; no polling here).
  const stateQuery = useBrowserState();
  useEffect(() => {
    if (stateQuery.data) {
      useBrowserStore.getState().applyServerState(stateQuery.data);
    }
  }, [stateQuery.data]);

  // Dedicated stream socket for the lifetime of the app shell (spec §9.3).
  useEffect(() => startBrowserStream(), []);

  // Measure the content region (the panel's flex-row parent) for width
  // clamping and the overlay derivation (spec §2.3).
  useEffect(() => {
    const parent = panelRef.current?.parentElement;
    if (!parent) return;
    setContainerWidth(parent.clientWidth);
    const observer = new ResizeObserver((entries) => {
      const entry = entries[0];
      if (entry) setContainerWidth(entry.contentRect.width);
    });
    observer.observe(parent);
    return () => observer.disconnect();
  }, [mode, setContainerWidth]);

  if (mode === "hidden") return null;

  const width = effectivePanelWidth(mode, panelWidth, containerWidth);
  const overlay = isOverlayContainer(containerWidth);

  const onSplitterPointerDown = (event: React.PointerEvent) => {
    event.preventDefault();
    const startX = event.clientX;
    const startWidth =
      panelRef.current?.getBoundingClientRect().width ?? width;
    setDragging(true);
    const onMove = (move: PointerEvent) => {
      // Panel is on the right: dragging left grows it.
      setSplitWidth(startWidth + (startX - move.clientX));
    };
    const onUp = () => {
      setDragging(false);
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", onUp);
    };
    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", onUp);
  };

  const content = (
    <div className="flex min-w-0 flex-1 flex-col">
      <BrowserTabs />
      <BrowserToolbar />
      <BrowserTaskBanner />
      <BrowserViewport />
    </div>
  );

  const splitter = mode !== "focus" && (
    <div
      role="separator"
      aria-orientation="vertical"
      aria-label="Resize browser panel (double-click to toggle compact / half)"
      onPointerDown={onSplitterPointerDown}
      onDoubleClick={toggleCompactSplit}
      className="w-1.5 shrink-0 cursor-col-resize bg-transparent transition-colors hover:bg-accent/40"
    />
  );

  if (overlay && mode !== "focus") {
    // Window too narrow for side-by-side: full-height right overlay (spec §2.3).
    return (
      <aside
        ref={panelRef}
        aria-label="Native GPT Browser"
        style={{ width: Math.min(width, containerWidth) }}
        className="absolute inset-y-0 right-0 z-30 flex border-l border-border bg-surface-1 shadow-xl"
      >
        {splitter}
        {content}
      </aside>
    );
  }

  return (
    <aside
      ref={panelRef}
      aria-label="Native GPT Browser"
      style={mode === "focus" ? undefined : { width }}
      className={`flex h-full min-w-0 flex-row border-l border-border bg-surface-1 ${
        mode === "focus" ? "flex-1" : "shrink-0"
      }`}
    >
      {splitter}
      {content}
    </aside>
  );
}
