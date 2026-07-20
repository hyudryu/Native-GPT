import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { PROTOCOL_VERSION, type Envelope } from "@agentgpt/protocol-types";
import {
  AgentSocket,
  backoffDelay,
  isEnvelope,
  wsUrl,
  useWsStore,
  BACKOFF_MAX_MS,
} from "./ws";

class MockWebSocket {
  static readonly CONNECTING = 0;
  static readonly OPEN = 1;
  static readonly CLOSING = 2;
  static readonly CLOSED = 3;
  static instances: MockWebSocket[] = [];

  readonly CONNECTING = 0;
  readonly OPEN = 1;
  readonly CLOSING = 2;
  readonly CLOSED = 3;

  readyState = 0;
  sent: string[] = [];
  onopen: (() => void) | null = null;
  onclose: (() => void) | null = null;
  onerror: (() => void) | null = null;
  onmessage: ((ev: { data: unknown }) => void) | null = null;

  constructor(public readonly url: string) {
    MockWebSocket.instances.push(this);
  }
  send(data: string) {
    this.sent.push(data);
  }
  close() {
    this.readyState = 3;
    this.onclose?.();
  }
  // test helpers
  emitOpen() {
    this.readyState = 1;
    this.onopen?.();
  }
  emitMessage(data: unknown) {
    this.onmessage?.({ data });
  }
  emitClose() {
    this.readyState = 3;
    this.onclose?.();
  }
}

const WSImpl = MockWebSocket as unknown as typeof WebSocket;

function makeEnvelope(overrides: Partial<Envelope> = {}): Envelope {
  return {
    protocol: PROTOCOL_VERSION,
    type: "runtime.health.ok",
    request_id: "req-1",
    timestamp: new Date().toISOString(),
    payload: { status: "ok" },
    ...overrides,
  };
}

beforeEach(() => {
  vi.useFakeTimers();
  MockWebSocket.instances = [];
  useWsStore.setState({ state: "idle", attempts: 0 });
});

afterEach(() => {
  vi.useRealTimers();
});

describe("isEnvelope", () => {
  it("accepts a valid envelope", () => {
    expect(isEnvelope(makeEnvelope())).toBe(true);
  });
  it("rejects wrong protocol versions", () => {
    expect(isEnvelope(makeEnvelope({ protocol: "2.0" as never }))).toBe(false);
  });
  it("rejects non-objects and missing fields", () => {
    expect(isEnvelope(null)).toBe(false);
    expect(isEnvelope("runtime.hello")).toBe(false);
    expect(isEnvelope({ protocol: "1.0", type: "a.b" })).toBe(false);
    expect(isEnvelope({ ...makeEnvelope(), payload: null })).toBe(false);
  });
});

describe("backoffDelay", () => {
  it("grows exponentially", () => {
    expect(backoffDelay(0)).toBe(500);
    expect(backoffDelay(1)).toBe(1000);
    expect(backoffDelay(2)).toBe(2000);
    expect(backoffDelay(3)).toBe(4000);
    expect(backoffDelay(4)).toBe(8000);
  });
  it("is capped at ~10s", () => {
    expect(backoffDelay(5)).toBe(BACKOFF_MAX_MS);
    expect(backoffDelay(20)).toBe(BACKOFF_MAX_MS);
  });
  it("treats negative attempts as zero", () => {
    expect(backoffDelay(-3)).toBe(500);
  });
});

describe("wsUrl", () => {
  it("uses ws:// for http pages and appends the token", () => {
    expect(
      wsUrl({ protocol: "http:", host: "100.64.1.2:8787" }, "abc"),
    ).toBe("ws://100.64.1.2:8787/ws?token=abc");
  });
  it("uses wss:// for https pages", () => {
    expect(wsUrl({ protocol: "https:", host: "example.com" }, "abc")).toBe(
      "wss://example.com/ws?token=abc",
    );
  });
  it("omits the query string without a token", () => {
    expect(wsUrl({ protocol: "http:", host: "127.0.0.1:8787" }, null)).toBe(
      "ws://127.0.0.1:8787/ws",
    );
  });
  it("encodes the token", () => {
    expect(wsUrl({ protocol: "http:", host: "h" }, "a b?")).toBe(
      "ws://h/ws?token=a%20b%3F",
    );
  });
});

describe("AgentSocket", () => {
  it("connects and reports state via the store", () => {
    const s = new AgentSocket("ws://test/ws", { WebSocketImpl: WSImpl });
    s.connect();
    expect(useWsStore.getState().state).toBe("connecting");
    MockWebSocket.instances[0]!.emitOpen();
    expect(useWsStore.getState().state).toBe("open");
  });

  it("dispatches valid envelopes to type and wildcard subscribers", () => {
    const s = new AgentSocket("ws://test/ws", { WebSocketImpl: WSImpl });
    s.connect();
    const ws = MockWebSocket.instances[0]!;
    ws.emitOpen();

    const seen: string[] = [];
    const off = s.on("runtime.health.ok", (e) => seen.push(`typed:${e.request_id}`));
    s.onAny((e) => seen.push(`any:${e.type}`));

    ws.emitMessage(JSON.stringify(makeEnvelope()));
    expect(seen).toEqual(["any:runtime.health.ok", "typed:req-1"]);

    off();
    ws.emitMessage(JSON.stringify(makeEnvelope({ request_id: "req-2" })));
    expect(seen).toEqual(["any:runtime.health.ok", "typed:req-1", "any:runtime.health.ok"]);
  });

  it("replays recent events to late type subscribers", () => {
    const s = new AgentSocket("ws://test/ws", { WebSocketImpl: WSImpl });
    s.connect();
    const ws = MockWebSocket.instances[0]!;
    ws.emitOpen();
    ws.emitMessage(
      JSON.stringify(
        makeEnvelope({
          type: "run.completed",
          request_id: "fast-run",
          payload: { run_id: "run-1" },
        }),
      ),
    );

    const seen: string[] = [];
    s.on("run.completed", (event) => seen.push(event.request_id));
    expect(seen).toEqual(["fast-run"]);
  });

  it("ignores malformed JSON, wrong-protocol, and binary frames", () => {
    const s = new AgentSocket("ws://test/ws", { WebSocketImpl: WSImpl });
    s.connect();
    const ws = MockWebSocket.instances[0]!;
    ws.emitOpen();

    const seen: unknown[] = [];
    s.onAny((e) => seen.push(e));
    ws.emitMessage("not json{");
    ws.emitMessage(JSON.stringify({ protocol: "2.0", type: "a.b" }));
    ws.emitMessage(new ArrayBuffer(4));
    expect(seen).toEqual([]);
  });

  it("send() serializes envelopes only while open", () => {
    const s = new AgentSocket("ws://test/ws", { WebSocketImpl: WSImpl });
    s.connect();
    const ws = MockWebSocket.instances[0]!;
    expect(s.send(makeEnvelope())).toBe(false);
    ws.emitOpen();
    expect(s.send(makeEnvelope())).toBe(true);
    expect(JSON.parse(ws.sent[0]!)).toMatchObject({ protocol: "1.0" });
  });

  it("reconnects with backoff after an unexpected close", () => {
    const s = new AgentSocket("ws://test/ws", { WebSocketImpl: WSImpl });
    s.connect();
    const ws = MockWebSocket.instances[0]!;
    ws.emitOpen();
    ws.emitClose();

    expect(useWsStore.getState().state).toBe("reconnecting");
    expect(MockWebSocket.instances).toHaveLength(1);

    vi.advanceTimersByTime(499);
    expect(MockWebSocket.instances).toHaveLength(1);
    vi.advanceTimersByTime(1);
    expect(MockWebSocket.instances).toHaveLength(2);

    // second failure → next backoff step (1000ms)
    MockWebSocket.instances[1]!.emitClose();
    vi.advanceTimersByTime(1000);
    expect(MockWebSocket.instances).toHaveLength(3);
  });

  it("does not reconnect after close()", () => {
    const s = new AgentSocket("ws://test/ws", { WebSocketImpl: WSImpl });
    s.connect();
    MockWebSocket.instances[0]!.emitOpen();
    s.close();
    vi.advanceTimersByTime(60_000);
    expect(MockWebSocket.instances).toHaveLength(1);
    expect(useWsStore.getState().state).toBe("closed");
  });

  it("kick() reconnects immediately when the socket is dead", () => {
    const s = new AgentSocket("ws://test/ws", { WebSocketImpl: WSImpl });
    s.connect();
    const ws = MockWebSocket.instances[0]!;
    ws.emitOpen();
    ws.emitClose(); // schedules a 500ms reconnect
    s.kick(); // foregrounded: reconnect now, not in 500ms
    expect(MockWebSocket.instances).toHaveLength(2);
    expect(useWsStore.getState().attempts).toBe(0);
  });

  it("kick() is a no-op while the socket is open", () => {
    const s = new AgentSocket("ws://test/ws", { WebSocketImpl: WSImpl });
    s.connect();
    MockWebSocket.instances[0]!.emitOpen();
    s.kick();
    expect(MockWebSocket.instances).toHaveLength(1);
  });
});
