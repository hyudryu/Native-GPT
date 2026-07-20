import { create } from "zustand";
import { PROTOCOL_VERSION, type Envelope } from "@agentgpt/protocol-types";
import { getToken } from "./auth";

export type ConnectionState =
  | "idle"
  | "connecting"
  | "open"
  | "reconnecting"
  | "closed";

interface WsState {
  state: ConnectionState;
  /** Number of reconnect attempts since the last successful open. */
  attempts: number;
}

export const useWsStore = create<WsState>(() => ({
  state: "idle",
  attempts: 0,
}));

/** Structural validation of an incoming protocol envelope. */
export function isEnvelope(value: unknown): value is Envelope {
  if (typeof value !== "object" || value === null) return false;
  const v = value as Record<string, unknown>;
  return (
    v.protocol === PROTOCOL_VERSION &&
    typeof v.type === "string" &&
    typeof v.request_id === "string" &&
    v.request_id.length > 0 &&
    typeof v.timestamp === "string" &&
    typeof v.payload === "object" &&
    v.payload !== null
  );
}

export const BACKOFF_BASE_MS = 500;
export const BACKOFF_MAX_MS = 10_000;

/** Pure exponential backoff: 500ms, 1s, 2s, 4s, 8s, then capped at ~10s. */
export function backoffDelay(
  attempt: number,
  baseMs: number = BACKOFF_BASE_MS,
  maxMs: number = BACKOFF_MAX_MS,
): number {
  return Math.min(maxMs, baseMs * 2 ** Math.max(0, attempt));
}

/** ws(s)://<host>/ws?token=<token> — mirrors the page's origin and scheme. */
export function wsUrl(
  loc: Pick<Location, "protocol" | "host"> = window.location,
  token: string | null = getToken(),
): string {
  const proto = loc.protocol === "https:" ? "wss:" : "ws:";
  const query = token ? `?token=${encodeURIComponent(token)}` : "";
  return `${proto}//${loc.host}/ws${query}`;
}

export type EnvelopeHandler = (envelope: Envelope) => void;

export interface AgentSocketOptions {
  /** Injectable for tests. */
  WebSocketImpl?: typeof WebSocket;
}

/**
 * Small resilient WebSocket client for the AgentGPT envelope protocol.
 *
 * - reconnects with exponential backoff (capped ~10s) until `close()` is called
 * - `kick()` reconnects immediately (used on visibilitychange → visible,
 *   because iOS standalone kills background sockets)
 * - subscribe per message `type`, or to every envelope via `onAny`
 */
export class AgentSocket {
  private ws: WebSocket | null = null;
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  private attempts = 0;
  private manualClose = false;
  private readonly typeHandlers = new Map<string, Set<EnvelopeHandler>>();
  private readonly allHandlers = new Set<EnvelopeHandler>();
  private readonly recentEnvelopes: Envelope[] = [];
  private readonly WSImpl: typeof WebSocket;

  constructor(
    private readonly url: string | (() => string),
    opts: AgentSocketOptions = {},
  ) {
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
    useWsStore.setState({
      state: this.attempts > 0 ? "reconnecting" : "connecting",
      attempts: this.attempts,
    });

    const url = typeof this.url === "function" ? this.url() : this.url;
    const ws = new this.WSImpl(url);
    this.ws = ws;

    ws.onopen = () => {
      this.attempts = 0;
      useWsStore.setState({ state: "open", attempts: 0 });
    };
    ws.onmessage = (event: MessageEvent) => {
      this.handleMessage(event.data);
    };
    ws.onclose = () => {
      this.ws = null;
      if (!this.manualClose) this.scheduleReconnect();
    };
    // onerror is intentionally unhandled: it is always followed by onclose,
    // which drives the reconnect logic.
    ws.onerror = () => {};
  }

  /** Permanently close (no reconnect). */
  close(): void {
    this.manualClose = true;
    this.clearReconnectTimer();
    this.ws?.close();
    this.ws = null;
    useWsStore.setState({ state: "closed", attempts: 0 });
  }

  /**
   * Reconnect now if the socket is dead. Called when the app returns to the
   * foreground — iOS standalone suspends/kills background sockets.
   */
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

  /** Send an envelope. Returns false when the socket isn't open. */
  send(envelope: Envelope): boolean {
    if (this.ws && this.ws.readyState === this.WSImpl.OPEN) {
      this.ws.send(JSON.stringify(envelope));
      return true;
    }
    return false;
  }

  /** Subscribe to envelopes of a given type. Returns an unsubscribe fn. */
  on(type: string, handler: EnvelopeHandler): () => void {
    let set = this.typeHandlers.get(type);
    if (!set) {
      set = new Set();
      this.typeHandlers.set(type, set);
    }
    set.add(handler);
    // A fast local model may finish between the HTTP start response and
    // React installing run-specific listeners. Replay a bounded buffer.
    for (const envelope of this.recentEnvelopes) {
      if (envelope.type === type) handler(envelope);
    }
    return () => {
      set.delete(handler);
      if (set.size === 0) this.typeHandlers.delete(type);
    };
  }

  /** Subscribe to every valid envelope. Returns an unsubscribe fn. */
  onAny(handler: EnvelopeHandler): () => void {
    this.allHandlers.add(handler);
    return () => {
      this.allHandlers.delete(handler);
    };
  }

  private handleMessage(data: unknown): void {
    if (typeof data !== "string") return;
    let parsed: unknown;
    try {
      parsed = JSON.parse(data);
    } catch {
      return; // not JSON — ignore
    }
    if (!isEnvelope(parsed)) return; // protocol mismatch / malformed — ignore
    this.recentEnvelopes.push(parsed);
    if (this.recentEnvelopes.length > 100) this.recentEnvelopes.shift();
    for (const handler of this.allHandlers) handler(parsed);
    const set = this.typeHandlers.get(parsed.type);
    if (set) for (const handler of set) handler(parsed);
  }

  private scheduleReconnect(): void {
    const delay = backoffDelay(this.attempts);
    this.attempts += 1;
    useWsStore.setState({ state: "reconnecting", attempts: this.attempts });
    this.reconnectTimer = setTimeout(() => this.connect(), delay);
  }

  private clearReconnectTimer(): void {
    if (this.reconnectTimer !== null) {
      clearTimeout(this.reconnectTimer);
      this.reconnectTimer = null;
    }
  }
}

/** App-wide singleton, URL resolved lazily (after initAuth has run). */
export const socket = new AgentSocket(() => wsUrl());

/** Connect the singleton and re-kick it whenever the app re-foregrounds. */
export function startSocket(): void {
  socket.connect();
  if (typeof document !== "undefined") {
    document.addEventListener("visibilitychange", () => {
      if (document.visibilityState === "visible") socket.kick();
    });
  }
}
