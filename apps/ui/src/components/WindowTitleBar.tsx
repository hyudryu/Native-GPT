import type { MouseEvent as ReactMouseEvent } from "react";
import { isTauri } from "@tauri-apps/api/core";
import { getCurrentWindow } from "@tauri-apps/api/window";
import { Minus, Square, X } from "lucide-react";
import BrowserHiddenIndicator from "../features/browser/BrowserHiddenIndicator";

export interface WindowDragController {
  startDragging: () => Promise<void>;
  toggleMaximize: () => Promise<void>;
}

export async function handleTitleBarMouseDown(
  appWindow: WindowDragController,
  button: number,
  detail: number,
): Promise<boolean> {
  if (button !== 0) return false;
  if (detail === 2) {
    await appWindow.toggleMaximize();
  } else {
    await appWindow.startDragging();
  }
  return true;
}

const controlButton =
  "inline-flex h-full w-12 items-center justify-center text-fg-muted transition-colors hover:bg-surface-2 hover:text-fg focus-visible:z-10";

export default function WindowTitleBar() {
  if (!isTauri()) return null;

  const appWindow = getCurrentWindow();

  const handleDrag = (event: ReactMouseEvent<HTMLElement>) => {
    if (event.button !== 0 || event.target !== event.currentTarget) return;
    void handleTitleBarMouseDown(appWindow, event.button, event.detail);
  };

  return (
    <header className="flex h-10 shrink-0 select-none items-stretch border-b border-border bg-surface-1">
      <div
        onMouseDown={handleDrag}
        className="flex min-w-0 flex-1 items-center px-3 text-xs font-medium text-fg-muted"
      >
        Native GPT
      </div>
      <div className="flex shrink-0 items-center pr-1">
        <BrowserHiddenIndicator />
      </div>
      <div className="flex shrink-0" aria-label="Window controls">
        <button
          type="button"
          aria-label="Minimize window"
          onClick={() => void appWindow.minimize()}
          className={controlButton}
        >
          <Minus className="size-4" aria-hidden />
        </button>
        <button
          type="button"
          aria-label="Maximize or restore window"
          onClick={() => void appWindow.toggleMaximize()}
          className={controlButton}
        >
          <Square className="size-3.5" aria-hidden />
        </button>
        <button
          type="button"
          aria-label="Close window"
          onClick={() => void appWindow.close()}
          className={`${controlButton} hover:bg-danger hover:text-white`}
        >
          <X className="size-4" aria-hidden />
        </button>
      </div>
    </header>
  );
}
