"""OpenVoice register voice Strands tool — approval-gated voice cloning.

Uploads a reference audio clip to the OpenVoice workload on a remote backend
host, which extracts the speaker embedding and returns a voice_id. The voice_id
can then be used with openvoice_tts to synthesize speech in that voice.
"""

from __future__ import annotations

import importlib.util
import mimetypes
from pathlib import Path
from typing import Any

from strands import tool

# Load the shared `_lib/bridge_client.py` and `_lib/paths.py`.
_LIB_DIR = Path(__file__).resolve().parent.parent / "_lib"

_bridge_spec = importlib.util.spec_from_file_location(
    "agentgpt_tools_bridge_client", _LIB_DIR / "bridge_client.py"
)
assert _bridge_spec is not None and _bridge_spec.loader is not None
_bridge = importlib.util.module_from_spec(_bridge_spec)
_bridge_spec.loader.exec_module(_bridge)

_paths_spec = importlib.util.spec_from_file_location(
    "agentgpt_tools_paths", _LIB_DIR / "paths.py"
)
assert _paths_spec is not None and _paths_spec.loader is not None
_paths = importlib.util.module_from_spec(_paths_spec)
_paths_spec.loader.exec_module(_paths)

PathEscapeError = _paths.PathEscapeError

MAX_CLIP_BYTES = 20 * 1024 * 1024  # 20 MB
SUPPORTED_EXTENSIONS = {".mp3", ".wav", ".flac", ".ogg", ".m4a", ".aac"}


class RegisterVoiceError(ValueError):
    """Raised for any register_voice failure."""


async def register_voice(
    name: str,
    file_path: str,
    host: str | None = None,
) -> dict[str, Any]:
    """Upload a reference clip and return a standard-schema result dict."""
    if not name or not name.strip():
        return {
            "ok": False,
            "summary": "Voice name is required",
            "data": {},
            "error": {"code": "bad_request", "message": "name must not be empty"},
        }
    try:
        resolved = _paths.resolve_under_root(file_path)
    except PathEscapeError as exc:
        return {
            "ok": False,
            "summary": f"File path not allowed: {exc}",
            "data": {"file_path": file_path},
            "error": {"code": "path_error", "message": str(exc)},
        }
    if not resolved.exists():
        return {
            "ok": False,
            "summary": f"File not found: {file_path}",
            "data": {"file_path": file_path},
            "error": {"code": "not_found", "message": f"file not found: {file_path}"},
        }
    if not resolved.is_file():
        return {
            "ok": False,
            "summary": f"Not a file: {file_path}",
            "data": {"file_path": file_path},
            "error": {"code": "bad_request", "message": f"not a file: {file_path}"},
        }

    ext = resolved.suffix.lower()
    if ext and ext not in SUPPORTED_EXTENSIONS:
        return {
            "ok": False,
            "summary": f"Unsupported audio format: {ext}",
            "data": {"file_path": file_path},
            "error": {
                "code": "bad_request",
                "message": f"unsupported format {ext}; supported: {sorted(SUPPORTED_EXTENSIONS)}",
            },
        }

    file_size = resolved.stat().st_size
    if file_size > MAX_CLIP_BYTES:
        return {
            "ok": False,
            "summary": f"Clip too large: {file_size} bytes",
            "data": {"file_path": file_path},
            "error": {
                "code": "bad_request",
                "message": f"clip exceeds {MAX_CLIP_BYTES} bytes ({file_size} bytes)",
            },
        }

    clip_bytes = resolved.read_bytes()
    mime_type = mimetypes.guess_type(str(resolved))[0] or "audio/mpeg"
    filename = resolved.name

    # Resolve the host id.
    if host:
        try:
            host_data = await _bridge.api_get("/api/remote-hosts")
        except _bridge.BridgeClientError as exc:
            return {
                "ok": False,
                "summary": f"Could not resolve host: {exc.message}",
                "data": {},
                "error": {"code": exc.code, "message": exc.message},
            }
        hosts = host_data.get("hosts", [])
        matched = next((h for h in hosts if h["name"] == host or h["id"] == host), None)
        if not matched:
            return {
                "ok": False,
                "summary": f"Host '{host}' not found",
                "data": {},
                "error": {"code": "not_found", "message": f"remote host '{host}' not found"},
            }
        host_id = matched["id"]
    else:
        # Use the default reachable host.
        try:
            host_data = await _bridge.api_get("/api/remote-hosts")
        except _bridge.BridgeClientError as exc:
            return {
                "ok": False,
                "summary": f"Could not reach server: {exc.message}",
                "data": {},
                "error": {"code": exc.code, "message": exc.message},
            }
        hosts = host_data.get("hosts", [])
        if not hosts:
            return {
                "ok": False,
                "summary": "No remote hosts configured",
                "data": {},
                "error": {
                    "code": "no_remote_host",
                    "message": "No remote host is configured. Add one in Settings → Remote Hosts.",
                },
            }
        reachable = next((h for h in hosts if h.get("status") == "reachable"), hosts[0])
        host_id = reachable["id"]

    # Upload the clip via the desktop's passthrough endpoint.
    try:
        result = await _bridge.api_post_multipart(
            f"/api/remote-hosts/{host_id}/voices",
            fields={"name": name},
            files={"clip": (filename, clip_bytes, mime_type)},
            timeout=60.0,
        )
    except _bridge.BridgeClientError as exc:
        return {
            "ok": False,
            "summary": f"Voice registration failed: {exc.message}",
            "data": {"file_path": file_path},
            "error": {"code": exc.code, "message": exc.message},
        }

    voice = result.get("voice", {})
    voice_id = voice.get("id", "")

    return {
        "ok": True,
        "summary": f"Voice '{name}' registered (id: {voice_id[:8]}…)",
        "data": {
            "voice_id": voice_id,
            "name": name,
            "file_path": file_path,
            "host_id": host_id,
        },
        "error": None,
    }


@tool
async def openvoice_register_voice(
    name: str,
    file_path: str,
    host: str | None = None,
) -> dict[str, Any]:
    """Register a cloned voice from a reference audio clip.

    Uploads an audio file (MP3, WAV, etc.) to OpenVoice on a remote backend
    host, which extracts the speaker embedding. Returns a voice_id that can be
    used with openvoice_tts to synthesize speech in that voice. REQUIRES user
    approval before each call.

    Args:
        name: A label for the voice (e.g. "Mark", "Narrator").
        file_path: Path to the reference audio clip (relative to repo root or
            absolute under an allowed root). MP3/WAV/FLAC/OGG/M4A/AAC.
        host: Optional host name. Omit for the default/reachable host.

    Returns:
        `{ok, summary, data: {voice_id, name, file_path, host_id}, error}`.
        Use the returned voice_id with openvoice_tts.
    """

    return await register_voice(name, file_path, host=host)


# Manifest flag consumed by the runtime's approval wiring.
REQUIRES_APPROVAL = True

TOOL = openvoice_register_voice
