import { Hand, Square } from "lucide-react";
import { useStopBrowserTask, useTakeOverBrowserTask } from "./browserApi";
import { useBrowserStore } from "./browserStore";
import { isTaskActiveStatus, type TaskStatus } from "./types";

const STATUS_TEXT: Record<TaskStatus, string | null> = {
  awaiting_approval: "Waiting for approval",
  starting: "Starting browser task…",
  running: null, // activity text is shown instead
  paused_for_user: "Paused — waiting for you",
  stopping: "Stopping task…",
  completed: null,
  failed: null,
  cancelled: null,
};

/**
 * Live agent status bar (spec §2.5): visible while a Page Agent task is
 * active, with Take over / Stop controls.
 */
export default function BrowserTaskBanner() {
  const task = useBrowserStore((s) => s.task);
  const stopTask = useStopBrowserTask();
  const takeOver = useTakeOverBrowserTask();

  if (!task || !isTaskActiveStatus(task.status)) return null;

  const statusText =
    STATUS_TEXT[task.status] ??
    (task.activity
      ? `Agent controlling this tab · ${task.activity}`
      : "Agent controlling this tab");
  const busy = stopTask.isPending || takeOver.isPending;

  return (
    <div
      role="status"
      className="flex items-center gap-2 border-b border-border bg-accent-subtle px-3 py-1.5"
    >
      <span className="size-2 shrink-0 animate-pulse rounded-full bg-accent" aria-hidden />
      <p className="min-w-0 flex-1 truncate text-xs text-fg">{statusText}</p>
      <button
        type="button"
        onClick={() => takeOver.mutate(task.id)}
        disabled={busy}
        className="inline-flex min-h-8 shrink-0 items-center gap-1 rounded-lg border border-border bg-surface-1 px-2 text-xs font-medium text-fg hover:bg-surface-2 disabled:opacity-50"
      >
        <Hand className="size-3.5" aria-hidden /> Take over
      </button>
      <button
        type="button"
        onClick={() => stopTask.mutate(task.id)}
        disabled={busy}
        className="inline-flex min-h-8 shrink-0 items-center gap-1 rounded-lg border border-border bg-surface-1 px-2 text-xs font-medium text-danger hover:bg-danger-subtle disabled:opacity-50"
      >
        <Square className="size-3.5" aria-hidden /> Stop
      </button>
    </div>
  );
}
