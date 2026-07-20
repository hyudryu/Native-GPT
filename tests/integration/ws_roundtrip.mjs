// WS -> host -> Python sidecar round-trip check (Node 24 built-in WebSocket).
const port = process.env.AGENTGPT_PORT ?? "18787";
const token = process.env.AGENTGPT_TOKEN ?? "";

const ws = new WebSocket(`ws://127.0.0.1:${port}/ws?token=${token}`);
const req = {
  protocol: "1.0",
  type: "runtime.health",
  request_id: `req-ci-${Date.now()}`,
  timestamp: new Date().toISOString(),
  payload: {},
};

const timer = setTimeout(() => {
  console.error("TIMEOUT waiting for sidecar response");
  process.exit(1);
}, 90_000);

ws.onopen = () => ws.send(JSON.stringify(req));
ws.onmessage = (e) => {
  clearTimeout(timer);
  const msg = JSON.parse(e.data);
  if (msg.type !== "runtime.health.ok" || msg.request_id !== req.request_id) {
    console.error("unexpected response:", e.data);
    process.exit(1);
  }
  if (!(msg.payload.rss_bytes > 0)) {
    console.error("rss_bytes missing:", e.data);
    process.exit(1);
  }
  console.log("round-trip ok:", e.data);
  ws.close();
  process.exit(0);
};
ws.onerror = (err) => {
  clearTimeout(timer);
  console.error("ws error:", err.message ?? err);
  process.exit(1);
};
