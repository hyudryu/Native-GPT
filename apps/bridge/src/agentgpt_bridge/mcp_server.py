"""MCP tool layer for the bridge.

Exposes the bridge's workload capabilities as MCP tools over streamable-http,
mounted into the same FastAPI app at ``/mcp`` (see ``app.py``). Every tool is
a thin wrapper over the shared helpers in ``ops.py`` — the same code paths
the REST routes use.

Tool return convention (mirrors the old desktop tools):

    {"ok": bool, "summary": str, "data": {...}, "error": str | None}

Generation tools return a full absolute ``data.asset_url`` pointing at the
bridge's ``/assets/{token}`` REST endpoint, which serves the bytes.
"""

from __future__ import annotations

import asyncio
import logging
import mimetypes
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import Context, FastMCP

from . import ops
from .config import BridgeConfig
from .manager import WorkloadManager

logger = logging.getLogger(__name__)

MCP_SERVER_NAME = "agentgpt-bridge"
MCP_MOUNT_PATH = "/mcp"

MAX_PROMPT_LENGTH = 8000
MAX_TEXT_LENGTH = 5000
VALID_KINDS = ("image", "video")

EXPECTED_TOOLS = (
    "list_workloads",
    "start_workload",
    "stop_workload",
    "comfyui_generate",
    "openvoice_tts",
    "openvoice_register_voice",
    "list_voices",
    "delete_voice",
)


def _error(summary: str, message: str, data: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"ok": False, "summary": summary, "data": data or {}, "error": message}


def _base_url_from_context(ctx: Context[Any, Any, Any] | None, config: BridgeConfig) -> str:
    """Derive the public base URL for asset links.

    Prefers the incoming request's Host/X-Forwarded-* headers (correct when
    the bridge sits behind a reverse proxy or Tailscale); falls back to the
    configured host/port. Never hard-coded.
    """
    request = None
    if ctx is not None:
        try:
            request = ctx.request_context.request
        except Exception:  # pragma: no cover - no active request context
            request = None
    if request is not None:
        headers = request.headers
        proto = headers.get("x-forwarded-proto") or request.url.scheme or "http"
        host = headers.get("x-forwarded-host") or headers.get("host")
        if host:
            return f"{proto}://{host}"
    host = config.host
    if host in ("0.0.0.0", "::", ""):
        host = "127.0.0.1"
    return f"http://{host}:{config.port}"


def create_mcp_server(manager: WorkloadManager, config: BridgeConfig) -> FastMCP:
    """Build the FastMCP server wrapping the given workload manager.

    The server is created with ``streamable_http_path="/"`` because it is
    mounted into the FastAPI app at ``MCP_MOUNT_PATH`` — the mount strips the
    prefix, so the inner route must live at the sub-app root.
    """
    mcp = FastMCP(
        MCP_SERVER_NAME,
        instructions=(
            "AgentGPT remote GPU bridge: manage workloads (ComfyUI image/video "
            "generation, OpenVoice voice-cloning TTS) and fetch generated media "
            "via returned asset URLs."
        ),
        streamable_http_path="/",
    )

    @mcp.tool()
    async def list_workloads() -> dict[str, Any]:
        """Capability snapshot of the bridge: workload ids, state, health, idle timers.

        Returns:
            {ok, summary, data: {workloads: [...]}, error}
        """
        workloads = ops.workload_snapshot(manager)
        return {
            "ok": True,
            "summary": f"{len(workloads)} workload(s) registered",
            "data": {"workloads": workloads},
            "error": None,
        }

    @mcp.tool()
    async def start_workload(workload_id: str) -> dict[str, Any]:
        """Start a workload (spins up its GPU process on demand).

        Args:
            workload_id: e.g. "comfyui" or "openvoice" (see list_workloads).
        """
        try:
            wl = await manager.ensure_started(workload_id)
        except KeyError:
            return _error(
                f"Unknown workload: {workload_id}", f"workload {workload_id} not found"
            )
        return {
            "ok": wl.state.value in ("ready", "busy"),
            "summary": f"workload {workload_id} is {wl.state.value}",
            "data": {"workload_id": workload_id, "state": wl.state.value},
            "error": wl.error_message,
        }

    @mcp.tool()
    async def stop_workload(workload_id: str) -> dict[str, Any]:
        """Stop a workload (kills its GPU process, frees all VRAM).

        Args:
            workload_id: e.g. "comfyui" or "openvoice" (see list_workloads).
        """
        try:
            await manager.stop(workload_id)
        except KeyError:
            return _error(
                f"Unknown workload: {workload_id}", f"workload {workload_id} not found"
            )
        return {
            "ok": True,
            "summary": f"workload {workload_id} stopped",
            "data": {"workload_id": workload_id, "state": "stopped"},
            "error": None,
        }

    @mcp.tool()
    async def comfyui_generate(
        prompt: str,
        kind: str = "image",
        model: str | None = None,
        size: str | None = None,
        ctx: Context[Any, Any, Any] | None = None,
    ) -> dict[str, Any]:
        """Generate an image or video using ComfyUI on this bridge host.

        Args:
            prompt: Description of what to generate (e.g. "a serene mountain
                landscape at sunset, oil painting style").
            kind: Output type: "image" (default) or "video".
            model: Optional model checkpoint name (e.g. "sdxl_base.safetensors").
                Omit to use the host's default.
            size: Optional resolution (e.g. "1024x1024"). Omit for default.

        Returns:
            {ok, summary, data: {prompt, kind, asset_id, asset_url, mime_type}, error}.
            asset_url is a full URL to the generated asset on this bridge.
        """
        if not prompt or not prompt.strip():
            return _error("Prompt is required", "prompt must not be empty")
        if len(prompt) > MAX_PROMPT_LENGTH:
            return _error(
                f"Prompt exceeds {MAX_PROMPT_LENGTH} characters",
                f"prompt too long ({len(prompt)} chars)",
            )
        if kind not in VALID_KINDS:
            return _error(
                f"Unsupported output kind: {kind}", "kind must be 'image' or 'video'"
            )
        # Same job body shape as the old REST proxy (bridge.rs comfyui_job_body).
        job: dict[str, Any] = {
            "kind": "generate",
            "prompt": prompt,
            "output_kind": kind,
            "model": model,
            "size": size,
        }
        try:
            result = await ops.submit_job(manager, "comfyui", job)
        except KeyError:
            return _error(
                "ComfyUI workload not available", "workload comfyui not found"
            )
        except RuntimeError as exc:
            return _error(f"Generation failed: {exc}", str(exc))
        base_url = _base_url_from_context(ctx, config)
        return ops.job_result_to_tool_response(
            result, base_url, {"prompt": prompt[: ops.ECHO_LIMIT], "kind": kind}
        )

    @mcp.tool()
    async def openvoice_tts(
        text: str,
        voice_id: str | None = None,
        accent: str | None = None,
        speed: float = 1.0,
        ctx: Context[Any, Any, Any] | None = None,
    ) -> dict[str, Any]:
        """Synthesize speech from text using OpenVoice on this bridge host.

        If a voice_id is provided, the output is in that cloned voice (see
        openvoice_register_voice / list_voices); otherwise the default base
        speaker is used.

        Args:
            text: The text to convert to speech (must not be empty).
            voice_id: Optional cloned-voice identifier. Omit for the default voice.
            accent: Optional accent for v2 (e.g. "en-us", "en-br", "es", "fr",
                "jp", "kr", "zh"). Omit for default.
            speed: Speech speed multiplier (clamped to 0.1-5.0, default 1.0).

        Returns:
            {ok, summary, data: {text, voice_id, asset_id, asset_url, mime_type}, error}.
            asset_url is a full URL to the generated audio on this bridge.
        """
        if not text or not text.strip():
            return _error("Text is required", "text must not be empty")
        if len(text) > MAX_TEXT_LENGTH:
            return _error(
                f"Text exceeds {MAX_TEXT_LENGTH} characters",
                f"text too long ({len(text)} chars)",
            )
        # Clamp speed to the safe range, matching the workload's behavior.
        speed = max(0.1, min(float(speed), 5.0))
        job: dict[str, Any] = {
            "text": text,
            "voice_id": voice_id,
            "accent": accent,
            "speed": speed,
        }
        try:
            result = await ops.submit_job(manager, "openvoice", job)
        except KeyError:
            return _error(
                "OpenVoice workload not available", "workload openvoice not found"
            )
        except RuntimeError as exc:
            return _error(f"TTS failed: {exc}", str(exc))
        base_url = _base_url_from_context(ctx, config)
        return ops.job_result_to_tool_response(
            result,
            base_url,
            {"text": text[: ops.ECHO_LIMIT], "voice_id": voice_id, "kind": "audio"},
        )

    @mcp.tool()
    async def openvoice_register_voice(name: str, file_path: str) -> dict[str, Any]:
        """Register a cloned voice from a reference audio clip.

        IMPORTANT: ``file_path`` is a path ON THE BRIDGE HOST (the machine
        running this MCP server), not on the caller's machine. Upload the clip
        to the bridge host first, then pass its absolute path here.

        Args:
            name: Human-friendly voice name.
            file_path: Absolute path to the audio clip on the bridge host
                (e.g. /tmp/ref.mp3).

        Returns:
            {ok, summary, data: {voice_id, name}, error}. Use the voice_id
            with openvoice_tts.
        """
        if not name or not name.strip():
            return _error("Name is required", "name must not be empty")
        path = Path(file_path)
        if not path.is_file():
            return _error(
                f"Clip not found on bridge host: {file_path}",
                f"no such file: {file_path}",
            )
        clip_bytes = await asyncio.to_thread(path.read_bytes)
        mime_type = mimetypes.guess_type(path.name)[0] or "audio/mpeg"
        result = await ops.register_voice(
            manager,
            name=name,
            clip_bytes=clip_bytes,
            filename=path.name,
            mime_type=mime_type,
        )
        if "error" in result:
            return _error(f"Voice registration failed: {result['error']}", result["error"])
        voice_id = result.get("voice_id")
        return {
            "ok": True,
            "summary": f"Voice '{name}' registered (voice_id={voice_id})",
            "data": {"voice_id": voice_id, "name": name},
            "error": None,
        }

    @mcp.tool()
    async def list_voices() -> dict[str, Any]:
        """List registered OpenVoice cloned voices on this bridge host.

        Returns:
            {ok, summary, data: {voices: [...]}, error}
        """
        result = await ops.list_voices(manager)
        voices = result.get("voices", [])
        warning = result.get("warning")
        return {
            "ok": warning is None,
            "summary": warning or f"{len(voices)} voice(s) registered",
            "data": {"voices": voices},
            "error": warning,
        }

    @mcp.tool()
    async def delete_voice(voice_id: str) -> dict[str, Any]:
        """Delete a registered OpenVoice cloned voice.

        Args:
            voice_id: The voice identifier returned by openvoice_register_voice.
        """
        result = await ops.delete_voice(manager, voice_id)
        return {
            "ok": True,
            "summary": f"Voice {voice_id} deleted",
            "data": result,
            "error": None,
        }

    return mcp
