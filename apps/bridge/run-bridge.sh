#!/usr/bin/env bash
# ============================================================
#  AgentGPT Bridge — one-click launcher (Linux / Ubuntu / DGX)
#
#  Runs the remote backend host service that manages GPU workloads
#  (ComfyUI for image/video, OpenVoice for voice-cloning TTS).
#  The AgentGPT desktop app connects to this as a "remote host".
#
#  Usage:
#    ./run-bridge.sh                    start on 0.0.0.0:8443
#    ./run-bridge.sh --fake             use fake workloads (no GPU needed)
#    ./run-bridge.sh --port 9000        custom port
#    ./run-bridge.sh --host 127.0.0.1   localhost only
#    ./run-bridge.sh --help             full options
#
#  Environment (set before running or via a .env file):
#    AGENTGPT_BRIDGE_TOKEN        bearer token (auto-generated if unset)
#    AGENTGPT_COMFYUI_PATH        path to ComfyUI checkout (default /opt/ComfyUI)
#    AGENTGPT_COMFYUI_PYTHON      python for ComfyUI subprocess (default python)
#    AGENTGPT_COMFYUI_PORT        ComfyUI internal port (default 8188)
#    AGENTGPT_OPENVOICE_CHECKPOINTS  OpenVoice v2 checkpoints (default /opt/OpenVoice/checkpoints_v2)
#    AGENTGPT_OPENVOICE_PORT      OpenVoice worker port (default 8200)
#    AGENTGPT_OPENVOICE_VOICES_DIR  where voice embeddings are stored (default /var/lib/agentgpt/voices)
#
#  First run: it installs dependencies via `uv sync`, then starts the server.
# ============================================================
set -euo pipefail
cd "$(dirname "$0")"

# ---- defaults ------------------------------------------------
PORT="${AGENTGPT_BRIDGE_PORT:-8443}"
HOST="${AGENTGPT_BRIDGE_HOST:-0.0.0.0}"
EXTRA_ARGS=""

# ---- parse args ----------------------------------------------
while [[ $# -gt 0 ]]; do
    case "$1" in
        --port)   PORT="$2"; shift 2 ;;
        --host)   HOST="$2"; shift 2 ;;
        --fake)   EXTRA_ARGS="$EXTRA_ARGS --fake"; shift ;;
        --help|-h)
            sed -n '3,28p' "$0"
            exit 0 ;;
        *)        EXTRA_ARGS="$EXTRA_ARGS $1"; shift ;;
    esac
done

# ---- prerequisite check: uv ----------------------------------
if ! command -v uv &>/dev/null; then
    echo "ERROR: 'uv' was not found in PATH."
    echo "Install it:  curl -LsSf https://astral.sh/uv/install.sh | sh"
    echo "Then re-run this script."
    exit 1
fi

# ---- first-run dependency install ----------------------------
if [[ ! -d ".venv" ]]; then
    echo "Installing bridge dependencies (first run)..."
    uv sync
    echo ""
fi

# ---- token handling ------------------------------------------
# If no token is set, generate a random one and print it so the operator
# can paste it into the desktop app's Remote Host settings.
if [[ -z "${AGENTGPT_BRIDGE_TOKEN:-}" ]]; then
    GENERATED_TOKEN="$(python3 -c "import secrets; print(secrets.token_hex(32))" 2>/dev/null || uv run python -c "import secrets; print(secrets.token_hex(32))")"
    export AGENTGPT_BRIDGE_TOKEN="$GENERATED_TOKEN"
    echo "============================================================"
    echo "  Generated bridge token (copy into desktop Remote Host settings):"
    echo ""
    echo "    $GENERATED_TOKEN"
    echo ""
    echo "  Set AGENTGPT_BRIDGE_TOKEN to use a fixed token."
    echo "============================================================"
    echo ""
fi

# ---- launch --------------------------------------------------
echo "  AgentGPT Bridge"
echo "  Listening:   http://$HOST:$PORT"
echo "  Health:      http://$HOST:$PORT/health"
if [[ "$EXTRA_ARGS" == *"--fake"* ]]; then
    echo "  Workloads:   FAKE (no GPU, for testing)"
else
    echo "  Workloads:   ComfyUI + OpenVoice (on-demand, GPU)"
fi
echo ""

exec uv run python -m agentgpt_bridge --host "$HOST" --port "$PORT" $EXTRA_ARGS
