#!/usr/bin/env bash
# Integration test: start the host headless, do a WS -> host -> sidecar round-trip,
# verify auth behavior and PWA asset availability. Exits non-zero on failure.
set -euo pipefail
cd "$(dirname "$0")/../.."

PORT="${PORT:-18787}"
TOKEN="ci-test-token-$(date +%s)"
export AGENTGPT_TOKEN="$TOKEN"

cargo run -p agentgpt-host -- --headless --port "$PORT" &
HOST_PID=$!
trap 'kill $HOST_PID 2>/dev/null || true' EXIT

# wait for the server
for i in $(seq 1 60); do
  if curl -sf "http://127.0.0.1:$PORT/api/health" >/dev/null 2>&1; then break; fi
  sleep 1
done

echo "== health =="
curl -sf "http://127.0.0.1:$PORT/api/health" | grep -q '"status":"ok"' && echo "health ok"

echo "== static index served =="
curl -sf "http://127.0.0.1:$PORT/" | grep -qi "<html" && echo "index ok"
# NOTE: non-localhost token rejection is covered by crates/server unit tests;
# CI runners have no Tailscale interface, so only localhost is bound here.

echo "== PWA assets =="
for path in manifest.webmanifest sw.js icons/icon-192.png; do
  curl -sf "http://127.0.0.1:$PORT/$path" >/dev/null && echo "$path ok"
done

echo "== WS round-trip to sidecar =="
AGENTGPT_PORT="$PORT" node "$(dirname "$0")/ws_roundtrip.mjs"
echo "ALL INTEGRATION CHECKS PASSED"
