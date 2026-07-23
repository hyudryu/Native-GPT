"""OpenVoice TTS Strands tool — approval-gated speech synthesis.

Synthesizes speech from text using OpenVoice on a remote backend host. If a
voice_id is provided, the output is in that cloned voice; otherwise the default
base speaker is used. The desktop server proxies to the bridge.
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

MAX_TEXT_LENGTH = 5000


async def synthesize(
    text: str,
    voice_id: str | None = None,
    accent: str | None = None,
    speed: float = 1.0,
    host: str | None = None,
) -> dict[str, Any]:
    """Submit a TTS request and return a standard-schema result dict."""
    if not text or not text.strip():
        return {
            "ok": False,
            "summary": "Text is required",
            "data": {},
            "error": {"code": "bad_request", "message": "text must not be empty"},
        }
    if len(text) > MAX_TEXT_LENGTH:
        return {
            "ok": False,
            "summary": f"Text exceeds {MAX_TEXT_LENGTH} characters",
            "data": {},
            "error": {"code": "bad_request", "message": f"text too long ({len(text)} chars)"},
        }

    body: dict[str, Any] = {"text": text, "speed": speed}
    if voice_id:
        body["voice_id"] = voice_id
    if accent:
        body["accent"] = accent
    if host:
        body["host"] = host

    try:
        result = await _bridge.api_post("/api/remote-hosts/tts", json=body, timeout=60.0)
    except _bridge.BridgeClientError as exc:
        return {
            "ok": False,
            "summary": f"TTS failed: {exc.message}",
            "data": {"text": text[:200]},
            "error": {"code": exc.code, "message": exc.message},
        }

    asset_id = result.get("asset_id", "")
    asset_url = result.get("asset_url", "")
    base = _bridge.server_base_url()

    return {
        "ok": True,
        "summary": result.get("summary", "Speech synthesized successfully"),
        "data": {
            "text": text[:200],
            "voice_id": voice_id,
            "asset_id": asset_id,
            "asset_url": f"{base}{asset_url}" if asset_url else None,
            "host": result.get("host"),
        },
        "error": None,
    }


@tool
async def openvoice_tts(
    text: str,
    voice_id: str | None = None,
    accent: str | None = None,
    speed: float = 1.0,
    host: str | None = None,
) -> dict[str, Any]:
    """Synthesize speech from text using OpenVoice on a remote backend host.

    Converts text to speech. If a voice_id is provided, the speech is in that
    cloned voice (use openvoice_register_voice to create one from an audio
    clip). Without a voice_id, uses the default base speaker. REQUIRES user
    approval before each call.

    Args:
        text: The text to convert to speech.
        voice_id: Optional cloned-voice identifier. Omit for the default voice.
        accent: Optional accent for v2 (e.g. "en-us", "en-br", "es", "fr",
            "jp", "kr", "zh"). Omit for default.
        speed: Speech speed multiplier (0.5–2.0, default 1.0).
        host: Optional host name. Omit for the default/reachable host.

    Returns:
        `{ok, summary, data: {text, voice_id, asset_id, asset_url, host}, error}`.
        The asset_url points to the generated audio for inline playback.
    """

    return await synthesize(text, voice_id=voice_id, accent=accent, speed=speed, host=host)


# Manifest flag consumed by the runtime's approval wiring.
REQUIRES_APPROVAL = True

TOOL = openvoice_tts
