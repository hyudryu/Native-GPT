"""Thin FastAPI worker wrapping the OpenVoice Python library.

OpenVoice has no built-in HTTP server. This worker exposes:
  - GET  /healthz
  - POST /synthesize      — TTS with optional cloned voice
  - POST /register_voice  — extract speaker embedding from a reference clip
  - GET  /voices          — list registered voices
  - DELETE /voices/{id}   — remove a voice

The bridge manages this worker as a subprocess (start on demand, stop to
release VRAM). Models load lazily on first request.
"""

from __future__ import annotations

import logging
import uuid
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile, status
from fastapi.responses import Response

logger = logging.getLogger(__name__)

app = FastAPI(title="OpenVoice Worker", version="0.1.0")

# Lazily-initialized OpenVoice objects (loaded on first use).
_tts = None
_converter = None
_checkpoints_dir: Path | None = None
_voices_dir: Path | None = None
_voices: dict[str, dict] = {}  # voice_id -> {name, se_path, ref_clip, created_at}


def _init_paths(checkpoints: str, voices_dir: str) -> None:
    global _checkpoints_dir, _voices_dir
    _checkpoints_dir = Path(checkpoints)
    _voices_dir = Path(voices_dir)
    _voices_dir.mkdir(parents=True, exist_ok=True)


def _ensure_loaded() -> None:
    """Lazily import and initialize OpenVoice models."""
    global _tts, _converter
    if _tts is not None and _converter is not None:
        return
    if _checkpoints_dir is None or _voices_dir is None:
        raise RuntimeError("worker not configured: call _init_paths first")
    try:
        from openvoice.api import BaseSpeakerTTS, ToneColorConverter
    except ImportError as exc:
        raise RuntimeError(
            f"OpenVoice is not installed: {exc}. Install openvoice in the worker environment."
        ) from exc

    import torch

    device = "cuda" if torch.cuda.is_available() else "cpu"
    v2_config = _checkpoints_dir / "v2" / "config.json"
    tts_config = v2_config if v2_config.exists() else _checkpoints_dir / "config.json"
    _tts = BaseSpeakerTTS(str(tts_config), device=device)
    converter_config = _checkpoints_dir / "v2" / "config.json"
    _converter = ToneColorConverter(str(converter_config), device=device)

    # Load checkpoints.
    v2_ckpt = _checkpoints_dir / "v2" / "model.safetensors"
    ckpt = v2_ckpt if v2_ckpt.exists() else _checkpoints_dir / "checkpoint.pth"
    _converter.load_ckpt(str(ckpt))
    logger.info("OpenVoice models loaded on %s", device)


@app.get("/healthz")
async def healthz() -> dict:
    return {"ok": True}


@app.post("/synthesize")
async def synthesize(
    text: str = Form(...),
    voice_id: str | None = Form(default=None),
    accent: str = Form(default="en-us"),
    speed: float = Form(default=1.0),
) -> Response:
    """Synthesize speech. If voice_id is provided, clone that voice."""
    _ensure_loaded()
    import io as _io

    import soundfile as sf

    # Generate base TTS audio.
    base_name = f"tts_{uuid.uuid4()}.wav"
    output_path = (_voices_dir / base_name) if _voices_dir else Path(base_name)
    # OpenVoice v2 uses accent internally; base TTS language stays English.
    _tts.tts(text, str(output_path), language="English", speed=speed)

    # If a voice_id is provided, apply tone color conversion.
    if voice_id and voice_id in _voices:
        se_path = Path(_voices[voice_id]["se_path"])
        if se_path.exists():
            converted_path = output_path.with_suffix(".conv.wav")
            if _voices_dir:
                src_se = _voices_dir / "tmp" / "default_se.pth"
            else:
                src_se = Path("default_se.pth")
            # The source speaker embedding is typically the default/base speaker.
            _converter.convert(
                audio_src_path=str(output_path),
                src_se=str(src_se),
                tgt_se=str(se_path),
                output_path=str(converted_path),
            )
            output_path = converted_path

    audio_bytes, sr = sf.read(str(output_path))
    # Encode to WAV bytes.
    buffer = _io.BytesIO()
    sf.write(buffer, audio_bytes, sr, format="WAV")
    wav_bytes = buffer.getvalue()

    # Cleanup temp files.
    try:
        output_path.unlink(missing_ok=True)
    except Exception:
        pass

    return Response(content=wav_bytes, media_type="audio/wav")


@app.post("/register_voice")
async def register_voice(
    name: str = Form(...),  # noqa: B008
    clip: UploadFile = File(...),  # noqa: B008
) -> dict:
    """Register a voice by extracting its speaker embedding from a reference clip."""
    _ensure_loaded()
    clip_bytes = await clip.read()
    if not clip_bytes:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="empty clip")

    voice_id = str(uuid.uuid4())
    ref_suffix = Path(clip.filename or ".mp3").suffix
    if _voices_dir:
        ref_path = _voices_dir / f"ref_{voice_id}{ref_suffix}"
        se_path = _voices_dir / f"se_{voice_id}.pth"
    else:
        ref_path = Path(f"ref_{voice_id}")
        se_path = Path(f"se_{voice_id}.pth")
    ref_path.write_bytes(clip_bytes)

    _converter.extract_se([str(ref_path)], str(se_path))

    _voices[voice_id] = {
        "name": name,
        "se_path": str(se_path),
        "ref_clip": str(ref_path),
        "created_at": str(uuid.uuid1()),  # rough timestamp
    }
    return {"voice_id": voice_id, "name": name}


@app.get("/voices")
async def list_voices() -> dict:
    return {
        "voices": [
            {"voice_id": vid, "name": v["name"]}
            for vid, v in _voices.items()
        ]
    }


@app.delete("/voices/{voice_id}")
async def delete_voice(voice_id: str) -> dict:
    if voice_id not in _voices:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"voice {voice_id} not found")
    voice = _voices.pop(voice_id)
    for key in ("se_path", "ref_clip"):
        try:
            Path(voice[key]).unlink(missing_ok=True)
        except Exception:
            pass
    return {"deleted": voice_id}
