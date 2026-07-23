import { useEffect, useRef, useState } from "react";
import { Download, LoaderCircle, Play, RotateCw } from "lucide-react";
import { useStartBrowser } from "./browserApi";
import { browserStream } from "./browserStream";
import {
  selectInputBlocked,
  useBrowserStore,
} from "./browserStore";
import { attachInputBridge } from "./inputBridge";
import BrowserInstallDialog from "./BrowserInstallDialog";

const VIEWPORT_RESIZE_DEBOUNCE_MS = 150;

/**
 * The streamed Chromium surface (spec §10): renders the latest screencast
 * frame scaled to the panel, forwards input through the input bridge, and
 * reports viewport size changes up to the server.
 */
export default function BrowserViewport() {
  const installed = useBrowserStore((s) => s.installed);
  const installStatus = useBrowserStore((s) => s.installStatus);
  const processStatus = useBrowserStore((s) => s.processStatus);
  const frame = useBrowserStore((s) => s.frame);
  const inputBlocked = useBrowserStore(selectInputBlocked);

  const startBrowser = useStartBrowser();
  const [installOpen, setInstallOpen] = useState(false);
  const surfaceRef = useRef<HTMLDivElement | null>(null);
  const textareaRef = useRef<HTMLTextAreaElement | null>(null);
  const frameRef = useRef(frame);
  frameRef.current = frame;

  // Input bridge: pointer/keyboard/IME → stream commands.
  useEffect(() => {
    const surface = surfaceRef.current;
    const textarea = textareaRef.current;
    if (!surface || !textarea || processStatus !== "running") return;
    return attachInputBridge(surface, textarea, {
      sendMouse: (payload) => browserStream.sendMouse(payload),
      sendWheel: (payload) => browserStream.sendWheel(payload),
      sendKey: (payload) => browserStream.sendKey(payload),
      sendText: (payload) => browserStream.sendText(payload),
      onFocusAddress: () =>
        useBrowserStore.getState().requestAddressFocus(),
      isBlocked: () => selectInputBlocked(useBrowserStore.getState()),
      coordScale: () => {
        const current = frameRef.current;
        const el = surfaceRef.current;
        if (!current || !el) return { x: 1, y: 1 };
        const dsf = window.devicePixelRatio || 1;
        const rect = el.getBoundingClientRect();
        if (rect.width <= 0 || rect.height <= 0) return { x: 1, y: 1 };
        // Frames are device pixels; input is CSS pixels (spec §10.2).
        return {
          x: current.width / dsf / rect.width,
          y: current.height / dsf / rect.height,
        };
      },
    });
  }, [processStatus]);

  // Report viewport size changes up (debounced, spec §10.3).
  useEffect(() => {
    const surface = surfaceRef.current;
    if (!surface || processStatus !== "running") return;
    let timer: ReturnType<typeof setTimeout> | null = null;
    const observer = new ResizeObserver((entries) => {
      const entry = entries[0];
      if (!entry) return;
      if (timer !== null) clearTimeout(timer);
      timer = setTimeout(() => {
        const { width, height } = entry.contentRect;
        if (width < 1 || height < 1) return;
        browserStream.sendViewportResize({
          width: Math.round(width),
          height: Math.round(height),
          deviceScaleFactor: window.devicePixelRatio || 1,
        });
      }, VIEWPORT_RESIZE_DEBOUNCE_MS);
    });
    observer.observe(surface);
    return () => {
      if (timer !== null) clearTimeout(timer);
      observer.disconnect();
    };
  }, [processStatus]);

  // ---- states that replace the live surface ----

  if (!installed && installStatus !== "ready") {
    return (
      <div className="flex min-h-0 flex-1 flex-col items-center justify-center gap-3 bg-surface-0 p-6 text-center">
        <Download className="size-8 text-fg-subtle" aria-hidden />
        <p className="text-sm font-medium text-fg">Browser not installed</p>
        <p className="max-w-72 text-xs text-fg-muted">
          Native GPT Browser is an optional component with a dedicated Chromium
          runtime and Alibaba Page Agent support.
        </p>
        <button
          type="button"
          onClick={() => setInstallOpen(true)}
          className="min-h-11 rounded-xl bg-accent px-4 text-sm font-medium text-accent-contrast hover:bg-accent-hover"
        >
          Install Browser
        </button>
        <BrowserInstallDialog open={installOpen} onOpenChange={setInstallOpen} />
      </div>
    );
  }

  if (processStatus === "crashed") {
    return (
      <div className="flex min-h-0 flex-1 flex-col items-center justify-center gap-3 bg-surface-0 p-6 text-center">
        <p className="text-sm font-medium text-fg">Browser crashed</p>
        <p className="max-w-72 text-xs text-fg-muted">
          The browser process exited unexpectedly. Your profile and tabs are
          preserved.
        </p>
        <button
          type="button"
          onClick={() => startBrowser.mutate(undefined)}
          disabled={startBrowser.isPending}
          className="inline-flex min-h-11 items-center gap-2 rounded-xl bg-accent px-4 text-sm font-medium text-accent-contrast hover:bg-accent-hover disabled:opacity-50"
        >
          <RotateCw className="size-4" aria-hidden /> Restart browser
        </button>
      </div>
    );
  }

  if (processStatus === "stopped") {
    return (
      <div className="flex min-h-0 flex-1 flex-col items-center justify-center gap-3 bg-surface-0 p-6 text-center">
        <p className="text-sm font-medium text-fg">Browser is not running</p>
        <p className="max-w-72 text-xs text-fg-muted">
          Start the browser to view and control pages in this panel.
        </p>
        <button
          type="button"
          onClick={() => startBrowser.mutate(undefined)}
          disabled={startBrowser.isPending}
          className="inline-flex min-h-11 items-center gap-2 rounded-xl bg-accent px-4 text-sm font-medium text-accent-contrast hover:bg-accent-hover disabled:opacity-50"
        >
          {startBrowser.isPending ? (
            <LoaderCircle className="size-4 animate-spin" aria-hidden />
          ) : (
            <Play className="size-4" aria-hidden />
          )}
          Start browser
        </button>
        {startBrowser.isError && (
          <p role="alert" className="text-xs text-danger">
            {startBrowser.error.message}
          </p>
        )}
      </div>
    );
  }

  // ---- live surface (running / starting / stopping) ----

  return (
    <div className="relative flex min-h-0 flex-1 flex-col bg-surface-0">
      <div
        ref={surfaceRef}
        aria-label="Browser viewport"
        className={`relative min-h-0 flex-1 overflow-hidden ${
          inputBlocked ? "cursor-progress" : ""
        }`}
      >
        {frame ? (
          <img
            src={frame.url}
            alt="Browser page"
            draggable={false}
            className="absolute inset-0 h-full w-full select-none object-fill"
          />
        ) : (
          <div className="flex h-full flex-col items-center justify-center gap-2 text-fg-muted">
            <LoaderCircle className="size-6 animate-spin" aria-hidden />
            <p className="text-xs">
              {processStatus === "running"
                ? "Waiting for the first frame…"
                : "Starting browser…"}
            </p>
          </div>
        )}
        {inputBlocked && (
          <div className="pointer-events-none absolute bottom-2 right-2 rounded-lg border border-border bg-surface-3/90 px-2 py-1 text-xs text-fg-muted shadow-sm">
            Agent controlling — input paused
          </div>
        )}
      </div>
      {/* Hidden textarea: keyboard focus target + IME/composition input. */}
      <textarea
        ref={textareaRef}
        aria-label="Browser keyboard input"
        autoCapitalize="off"
        autoComplete="off"
        autoCorrect="off"
        spellCheck={false}
        className="absolute bottom-0 left-0 h-px w-px resize-none border-0 bg-transparent p-0 text-transparent opacity-0 outline-none"
      />
    </div>
  );
}
