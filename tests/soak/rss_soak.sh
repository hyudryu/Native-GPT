#!/usr/bin/env bash
# RSS soak harness (skeleton, Phase 0 thresholds provisional).
# Drives repeated sidecar round-trips through the host and asserts bounded RSS growth
# for both processes. Intended for the nightly leak CI job, not every PR.
#
# Usage: bash tests/soak/rss_soak.sh [cycles] [max_host_growth_mb] [max_sidecar_growth_mb]
set -euo pipefail
cd "$(dirname "$0")/../.."

CYCLES="${1:-50}"
MAX_HOST_GROWTH_MB="${2:-30}"
MAX_SIDECAR_GROWTH_MB="${3:-50}"
PORT="${PORT:-18788}"
TOKEN="soak-token-$(date +%s)"
export AGENTGPT_TOKEN="$TOKEN"

cargo run -p agentgpt-host -- --headless --port "$PORT" &
HOST_PID=$!
trap 'kill $HOST_PID 2>/dev/null || true' EXIT

for i in $(seq 1 60); do
  if curl -sf "http://127.0.0.1:$PORT/api/health" >/dev/null 2>&1; then break; fi
  sleep 1
done

health() { curl -sf "http://127.0.0.1:$PORT/api/health"; }
rss_of() { health | node -e 'let d="";process.stdin.on("data",c=>d+=c).on("end",()=>{const j=JSON.parse(d);console.log([j.host_rss_bytes, j.sidecar_rss_bytes ?? 0].join(" "))})'; }

echo "warmup: 5 cycles"
for i in $(seq 1 5); do AGENTGPT_PORT="$PORT" node tests/integration/ws_roundtrip.mjs >/dev/null; done

read -r host0 sidecar0 <<< "$(rss_of)"
echo "baseline: host=$((host0/1048576))MB sidecar=$((sidecar0/1048576))MB"

for i in $(seq 1 "$CYCLES"); do
  AGENTGPT_PORT="$PORT" node tests/integration/ws_roundtrip.mjs >/dev/null
  if (( i % 10 == 0 )); then
    read -r h s <<< "$(rss_of)"
    echo "cycle $i: host=$((h/1048576))MB sidecar=$((s/1048576))MB"
  fi
done

read -r host1 sidecar1 <<< "$(rss_of)"
host_growth=$(( (host1 - host0) / 1048576 ))
sidecar_growth=$(( (sidecar1 - sidecar0) / 1048576 ))
echo "growth after $CYCLES cycles: host=${host_growth}MB sidecar=${sidecar_growth}MB"

fail=0
(( host_growth > MAX_HOST_GROWTH_MB )) && { echo "FAIL: host RSS grew ${host_growth}MB > ${MAX_HOST_GROWTH_MB}MB"; fail=1; }
(( sidecar_growth > MAX_SIDECAR_GROWTH_MB )) && { echo "FAIL: sidecar RSS grew ${sidecar_growth}MB > ${MAX_SIDECAR_GROWTH_MB}MB"; fail=1; }
(( fail == 0 )) && echo "SOAK PASSED"
exit $fail
