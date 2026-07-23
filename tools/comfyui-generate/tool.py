"""ComfyUI generate Strands tool — approval-gated image/video generation.

Submits a generation prompt to the ComfyUI workload on a remote backend host,
waits for the result, and returns the generated asset. The desktop server acts
as the authenticated client of the bridge; this tool calls the desktop server's
REST API which proxies to the bridge.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

from strands import tool

# Load the shared `_lib/bridge_client.py` for calling the desktop server.
_LIB_PATH = Path(__file__).resolve().parent.parent / "_lib" / "bridge_client.py"
_spec = importlib.util.spec_from_file_location("agentgpt_tools_bridge_client", _LIB_PATH)
assert _spec is not None and _spec.loader is not None
_bridge = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_bridge)

MAX_PROMPT_LENGTH = 8000


async def generate(
    prompt: str,
    kind: str = "image",
    model: str | None = None,
    size: str | None = None,
    host: str | None = None,
) -> dict[str, Any]:
    """Submit a generation request and return a standard-schema result dict."""
    if not prompt or not prompt.strip():
        return {
            "ok": False,
            "summary": "Prompt is required",
            "data": {},
            "error": {"code": "bad_request", "message": "prompt must not be empty"},
        }
    if len(prompt) > MAX_PROMPT_LENGTH:
        return {
            "ok": False,
            "summary": f"Prompt exceeds {MAX_PROMPT_LENGTH} characters",
            "data": {},
            "error": {"code": "bad_request", "message": f"prompt too long ({len(prompt)} chars)"},
        }
    if kind not in ("image", "video"):
        return {
            "ok": False,
            "summary": f"Unsupported output kind: {kind}",
            "data": {},
            "error": {"code": "bad_request", "message": "kind must be 'image' or 'video'"},
        }

    body: dict[str, Any] = {"prompt": prompt, "kind": kind}
    if model:
        body["model"] = model
    if size:
        body["size"] = size
    if host:
        body["host"] = host

    try:
        result = await _bridge.api_post("/api/remote-hosts/generate", json=body, timeout=180.0)
    except _bridge.BridgeClientError as exc:
        return {
            "ok": False,
            "summary": f"Generation failed: {exc.message}",
            "data": {"prompt": prompt[:200]},
            "error": {"code": exc.code, "message": exc.message},
        }

    asset_id = result.get("asset_id", "")
    asset_url = result.get("asset_url", "")
    base = _bridge.server_base_url()

    return {
        "ok": True,
        "summary": result.get("summary", f"Generated {kind} successfully"),
        "data": {
            "prompt": prompt[:200],
            "kind": kind,
            "asset_id": asset_id,
            "asset_url": f"{base}{asset_url}" if asset_url else None,
            "host": result.get("host"),
        },
        "error": None,
    }


@tool
async def comfyui_generate(
    prompt: str,
    kind: str = "image",
    model: str | None = None,
    size: str | None = None,
    host: str | None = None,
) -> dict[str, Any]:
    """Generate an image or video using ComfyUI on a remote backend host.

    Submits a text prompt to ComfyUI and returns the generated asset. REQUIRES
    user approval before each call. The remote host must be configured and
    healthy — use remote_host_status first if unsure.

    Args:
        prompt: A description of what to generate (e.g. "a serene mountain
            landscape at sunset, oil painting style").
        kind: Output type: "image" (default) or "video".
        model: Optional model checkpoint name (e.g. "sdxl_base.safetensors").
            Omit to use the host's default.
        size: Optional resolution (e.g. "1024x1024"). Omit for default.
        host: Optional host name to use. Omit for the default/reachable host.

    Returns:
        `{ok, summary, data: {prompt, kind, asset_id, asset_url, host}, error}`.
        The asset_url points to the generated image/video for inline display.
    """

    return await generate(prompt, kind=kind, model=model, size=size, host=host)


# Manifest flag consumed by the runtime's approval wiring.
REQUIRES_APPROVAL = True

TOOL = comfyui_generate
