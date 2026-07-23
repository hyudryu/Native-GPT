import { Globe } from "lucide-react";
import { selectTaskActive, useBrowserStore } from "./browserStore";

/**
 * Compact browser-running indicator for the title bar (spec §2.2). Renders
 * only while the panel is hidden but the browser process is alive (or a task
 * is running); clicking it reopens the panel.
 */
export default function BrowserHiddenIndicator() {
  const mode = useBrowserStore((s) => s.mode);
  const processStatus = useBrowserStore((s) => s.processStatus);
  const taskActive = useBrowserStore(selectTaskActive);
  const open = useBrowserStore((s) => s.open);

  const running = processStatus === "running" || processStatus === "starting";
  if (mode !== "hidden" || (!running && !taskActive)) return null;

  return (
    <button
      type="button"
      onClick={open}
      title={taskActive ? "Browser task running — show panel" : "Browser running — show panel"}
      className="inline-flex h-7 shrink-0 items-center gap-1.5 rounded-full border border-border bg-surface-2 px-2.5 text-xs text-fg-muted transition-colors hover:bg-surface-3 hover:text-fg"
    >
      <Globe className="size-3.5" aria-hidden />
      <span>Browser</span>
      {taskActive && (
        <span className="size-1.5 animate-pulse rounded-full bg-accent" aria-hidden />
      )}
    </button>
  );
}
