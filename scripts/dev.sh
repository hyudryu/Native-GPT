#!/usr/bin/env bash
# Dev runner: builds the UI, then starts the host (headless) which lazy-spawns the sidecar.
# Usage:
#   ./scripts/dev.sh            # build UI once, run host headless on a random port
#   ./scripts/dev.sh --port 8787
#   ./scripts/dev.sh --ui-dev   # additionally start vite dev server (hot reload) on :5173
set -euo pipefail
cd "$(dirname "$0")/.."

PORT="${PORT:-0}"
UI_DEV=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --port) PORT="$2"; shift 2 ;;
    --host) shift 2 ;; # accepted for launcher compatibility; host always binds loopback
    --ui-dev) UI_DEV=1; shift ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

# Ensure deps are present
if [[ ! -d node_modules ]]; then pnpm install; fi
if [[ ! -d apps/agent-runtime/.venv ]]; then uv sync --directory apps/agent-runtime; fi

# Build UI so the host can serve it. Rebuild every launch so source changes
# are picked up (the host serves dist/ statically).
echo ">> building UI"
pnpm --filter @agentgpt/ui build

if [[ "$UI_DEV" == "1" ]]; then
  echo ">> starting vite dev server (proxy -> 127.0.0.1:8787)"
  (cd apps/ui && VITE_DEV_BACKEND=http://127.0.0.1:8787 pnpm dev) &
  VITE_PID=$!
  trap 'kill $VITE_PID 2>/dev/null || true' EXIT
  PORT=8787
fi

echo ">> starting host (headless, port=$PORT)"
exec cargo run -p agentgpt-host -- --headless --port "$PORT"
