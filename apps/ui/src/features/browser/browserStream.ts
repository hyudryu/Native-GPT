import { getToken } from "../../lib/auth";
import { backoffDelay } from "../../lib/ws";
import { useBrowserStore } from "./browserStore";
import {
  FRAME_HEADER_LEN,
  FRAME_VERSION,
  type BrowserStreamEvent,
  type KeyInput,
  type MouseInput,
  type ParsedFrame,
  type TextInput,
  type ViewportSize,
  type WheelInput,
} from "./types";

/**
 * Dedicated WebSocket client for `WS /api/browser/stream` (spec §9.3).
 * This is intentionally separate from lib/ws.ts (the chat envelope socket):
 * the browser stream carries high-frequency binary screencast frames and raw
 * JSON events, not protocol envelopes.
 */

/** ws(s)://<host>/api/browser/stream?token=<token> — mirrors lib/ws.ts. */
export function browserStreamUrl(
  loc: Pick<Location, "protocol" | "host"> = window.location,
  token: string | null = getToken(),
): string {
  const proto = loc.protocol === "https:" ? "wss:" : "ws:";
  const query = token ? `?token=${encodeURIComponent(token)}` : "";
  return `${proto}//${loc.host}/api/browser/stream${query}`;
}

/**
 * Parse a binary screencast frame:
 * `[u8 version=1][u64 frame_id][u32 width][u32 height][u8 format][image]`
 * — all integers big-endian. Returns null for malformed input.
 */
export function parseFrame(buffer: ArrayBuffer): ParsedFrame | null {
  if (buffer.byteLength < FRAME_HEADER_LEN) return null;
  const view = new DataView(buffer);
  const version = view.getUint8(0);
  if (version !== FRAME_VERSION) return null;
  const frameId = view.getBigUint64(1, false);
  const width = view.getUint32(9, false);
  const height = view.getUint32(13, false);
  const formatByte = view.getUint8(17);
  const format = formatByte === 1 ? "jpeg" : formatByte === 2 ? "webp" : null;
  if (!format) return null;
  return {
    frameId: Number(frameId),
    width,
    height,
    format,
    mime: format === "jpeg" ? "image/jpeg" : "image/webp",
    image: new Uint8Array(buffer.slice(FRAME_HEADER_LEN)),
  };
}

function isStreamEvent(value: unknown): value is BrowserStreamEvent {
  if (typeof value !== "object" || value === null) return false;
  const v = value as Record<string, unknown>;
  return typeof v.type === "string" && v.type.startsWith("browser.");
}

export interface BrowserStreamOptions {
  WebSocketImpl?: typeof WebSocket;
}

export class BrowserStream {
  private ws: WebSocket | null = null;
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  private attempts = 0;
  private manualClose = false;
  private lastFrameUrl: string | null = null;
  private readonly WSImpl: typeof WebSocket;

  constructor(opts: BrowserStreamOptions = {}) {
    if (!opts.WebSocketImpl && typeof WebSocket === "undefined") {
      throw new Error("WebSocket is not available in this environment");
    }
    this.WSImpl = opts.WebSocketImpl ?? WebSocket;
  }

  connect(): void {
    if (
      this.ws &&
      (this.ws.readyState === this.WSImpl.OPEN ||
        this.ws.readyState === this.WSImpl.CONNECTING)
    ) {
      return;
    }
    this.manualClose = false;
    this.clearReconnectTimer();

    const ws = new this.WSImpl(browserStreamUrl());
    ws.binaryType = "arraybuffer";
    this.ws = ws;

    ws.onopen = () => {
      this.attempts = 0;
      useBrowserStore.getState().setStreamConnected(true);
    };
    ws.onmessage = (event: MessageEvent) => {
      if (typeof event.data === "string") {
        this.handleText(event.data);
      } else if (event.data instanceof ArrayBuffer) {
        this.handleBinary(event.data);
      }
    };
    ws.onclose = () => {
      this.ws = null;
      useBrowserStore.getState().setStreamConnected(false);
      if (!this.manualClose) this.scheduleReconnect();
    };
    // onerror is always followed by onclose, which drives reconnects.
    ws.onerror = () => {};
  }

  /** Permanently close (no reconnect). */
  close(): void {
    this.manualClose = true;
    this.clearReconnectTimer();
    this.ws?.close();
    this.ws = null;
    this.revokeFrameUrl();
    useBrowserStore.getState().setStreamConnected(false);
  }

  /** Reconnect now if the socket is dead (app returned to foreground). */
  kick(): void {
    if (this.manualClose) return;
    if (
      !this.ws ||
      this.ws.readyState === this.WSImpl.CLOSED ||
      this.ws.readyState === this.WSImpl.CLOSING
    ) {
      this.attempts = 0;
      this.clearReconnectTimer();
      this.connect();
    }
  }

  // ---- commands (viewer → server) ----

  private send(command: Record<string, unknown>): boolean {
    if (this.ws && this.ws.readyState === this.WSImpl.OPEN) {
      this.ws.send(JSON.stringify(command));
      return true;
    }
    return false;
  }

  sendMouse(payload: MouseInput): boolean {
    return this.send({ type: "input.mouse", payload });
  }

  sendWheel(payload: WheelInput): boolean {
    return this.send({ type: "input.wheel", payload });
  }

  sendKey(payload: KeyInput): boolean {
    return this.send({ type: "input.key", payload });
  }

  sendText(payload: TextInput): boolean {
    return this.send({ type: "input.text", payload });
  }

  sendViewportResize(payload: ViewportSize): boolean {
    return this.send({ type: "viewport.resize", payload });
  }

  sendFrameAck(frameId: number): boolean {
    return this.send({ type: "frame.ack", frame_id: frameId });
  }

  sendTabActivate(tabId: string): boolean {
    return this.send({ type: "tab.activate", tab_id: tabId });
  }

  // ---- inbound ----

  private handleText(text: string): void {
    let parsed: unknown;
    try {
      parsed = JSON.parse(text);
    } catch {
      return;
    }
    if (!isStreamEvent(parsed)) return;
    const store = useBrowserStore.getState();
    switch (parsed.type) {
      case "browser.state":
        store.applyServerState(parsed.payload);
        break;
      case "browser.tab.created":
      case "browser.tab.updated":
        store.upsertTab(parsed.payload);
        break;
      case "browser.tab.closed":
        store.removeTab(parsed.payload.id);
        break;
      case "browser.navigation":
        store.setActiveTabUrl(parsed.payload.url);
        break;
      case "browser.task.started":
        store.setTask(parsed.payload);
        // A browser tool call reopens the hidden panel unless the user opted
        // to keep the browser hidden during automation (spec §2.2).
        if (store.mode === "hidden" && !store.keepHiddenDuringAutomation) {
          store.open();
        }
        break;
      case "browser.task.activity":
        store.setTaskActivity(parsed.payload.activity);
        break;
      case "browser.task.finished":
        store.setTaskStatus("completed");
        break;
      case "browser.task.failed":
        store.setTaskStatus("failed");
        break;
      case "browser.crashed":
        store.setProcessStatus("crashed");
        break;
      // browser.file_chooser / browser.download: approvals arrive via
      // browser.state pendingApprovals; downloads need no UI yet.
      default:
        break;
    }
  }

  private handleBinary(buffer: ArrayBuffer): void {
    const frame = parseFrame(buffer);
    if (!frame) return;
    this.revokeFrameUrl();
    let url = "";
    try {
      url = URL.createObjectURL(new Blob([frame.image], { type: frame.mime }));
    } catch {
      return; // environment without object URLs (tests) — drop the frame
    }
    this.lastFrameUrl = url;
    useBrowserStore.getState().setFrame({
      url,
      width: frame.width,
      height: frame.height,
      frameId: frame.frameId,
    });
    // Acknowledge after render so the server keeps at most one frame in
    // flight per viewer (spec §10.1).
    this.sendFrameAck(frame.frameId);
  }

  private revokeFrameUrl(): void {
    if (this.lastFrameUrl) {
      try {
        URL.revokeObjectURL(this.lastFrameUrl);
      } catch {
        /* environment without object URLs */
      }
      this.lastFrameUrl = null;
    }
  }

  private scheduleReconnect(): void {
    const delay = backoffDelay(this.attempts);
    this.attempts += 1;
    this.reconnectTimer = setTimeout(() => this.connect(), delay);
  }

  private clearReconnectTimer(): void {
    if (this.reconnectTimer !== null) {
      clearTimeout(this.reconnectTimer);
      this.reconnectTimer = null;
    }
  }
}

/** App-wide singleton, URL resolved lazily per connect (like lib/ws.ts). */
export const browserStream = new BrowserStream();

/**
 * Connect the singleton and re-kick it when the app re-foregrounds.
 * The socket stays open while the panel is hidden — the server pauses
 * screencast frames automatically, and state events keep flowing (spec §10.1).
 */
export function startBrowserStream(): () => void {
  browserStream.connect();
  if (typeof document !== "undefined") {
    const onVisibility = () => {
      if (document.visibilityState === "visible") browserStream.kick();
    };
    document.addEventListener("visibilitychange", onVisibility);
    return () => document.removeEventListener("visibilitychange", onVisibility);
  }
  return () => {};
}
