"""OpenVoice workload controller.

OpenVoice is a Python library (no built-in server, no in-process unload API).
The bridge manages a thin FastAPI worker subprocess that wraps
``openvoice/api.py``. VRAM is released by tearing down the worker process on
soft idle (respawned on the next request) — this is the only reliable way to
free VRAM since OpenVoice has no /free equivalent.

The worker lives at ``apps/bridge/openvoice_worker/``. It's spawned with:

    python -m openvoice_worker --port <port> --checkpoints <dir>

Configurable via env vars:
  AGENTGPT_OPENVOICE_PYTHON     — python executable (default: python)
  AGENTGPT_OPENVOICE_PORT       — worker port (default: 8200)
  AGENTGPT_OPENVOICE_CHECKPOINTS — path to OpenVoice checkpoints_v2
    (default: /opt/OpenVoice/checkpoints_v2)
"""

from __future__ import annotations

import asyncio
import logging
import os
import uuid
from pathlib import Path
from typing import Any

import httpx

from .base import JobResult, JobStatus, Workload, WorkloadState

logger = logging.getLogger(__name__)

DEFAULT_PORT = 8200
DEFAULT_CHECKPOINTS = "/opt/OpenVoice/checkpoints_v2"
DEFAULT_VOICES_DIR = "/var/lib/agentgpt/voices"


class OpenVoiceWorkload(Workload):
    """Manages the OpenVoice FastAPI worker subprocess."""

    workload_id: str = "openvoice"
    display_name: str = "OpenVoice"
    description: str = "Voice-cloning TTS via OpenVoice (clone any voice from a short clip)."

    def __init__(self) -> None:
        super().__init__()
        # OpenVoice models stay resident until process teardown; soft idle
        # must stop the process (no in-process unload API).
        self.soft_idle_seconds = 300.0  # 5 min
        self.hard_idle_seconds = 900.0  # 15 min
        self.port = int(os.environ.get("AGENTGPT_OPENVOICE_PORT", DEFAULT_PORT))
        self.checkpoints = os.environ.get(
            "AGENTGPT_OPENVOICE_CHECKPOINTS", DEFAULT_CHECKPOINTS
        )
        self.voices_dir = Path(os.environ.get("AGENTGPT_OPENVOICE_VOICES_DIR", DEFAULT_VOICES_DIR))
        self._proc: asyncio.subprocess.Process | None = None

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    async def start(self) -> None:
        """Spawn the OpenVoice worker subprocess."""
        if self.state in (WorkloadState.READY, WorkloadState.BUSY):
            return
        self.state = WorkloadState.STARTING
        self.error_message = None
        exe = os.environ.get("AGENTGPT_OPENVOICE_PYTHON", "python")
        # The worker module lives alongside this bridge package.
        worker_dir = Path(__file__).resolve().parent.parent.parent / "openvoice_worker"
        args = [
            "-m",
            "openvoice_worker",
            "--port",
            str(self.port),
            "--checkpoints",
            self.checkpoints,
            "--voices-dir",
            str(self.voices_dir),
        ]
        try:
            self._proc = await asyncio.create_subprocess_exec(
                exe,
                *args,
                cwd=str(worker_dir),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            self.state = WorkloadState.ERROR
            self.error_message = f"failed to spawn OpenVoice worker: {exc}"
            logger.error(self.error_message)
            return

        if not await self._wait_for_ready(timeout=120):
            self.state = WorkloadState.ERROR
            self.error_message = "OpenVoice worker did not become ready within 120s"
            logger.error(self.error_message)
            return

        self.state = WorkloadState.READY
        self.touch()
        logger.info("OpenVoice worker started on port %s", self.port)

    async def _wait_for_ready(self, timeout: float = 120) -> bool:
        deadline = asyncio.get_event_loop().time() + timeout
        async with httpx.AsyncClient() as client:
            while asyncio.get_event_loop().time() < deadline:
                try:
                    resp = await client.get(f"{self.base_url}/healthz", timeout=5)
                    if resp.status_code == 200:
                        return True
                except (httpx.HTTPError, ConnectionError):
                    pass
                await asyncio.sleep(2)
        return False

    async def stop(self) -> None:
        """Terminate the worker subprocess."""
        self.state = WorkloadState.STOPPING
        if self._proc and self._proc.returncode is None:
            self._proc.terminate()
            try:
                await asyncio.wait_for(self._proc.wait(), timeout=10)
            except TimeoutError:
                self._proc.kill()
                await self._proc.wait()
        self._proc = None
        self.state = WorkloadState.STOPPED

    async def soft_idle(self) -> None:
        """Release VRAM by stopping the worker (no in-process unload API).

        OpenVoice models stay resident until process teardown. The worker is
        respawned on the next request.
        """
        if self.state == WorkloadState.STOPPED:
            return
        logger.info("OpenVoice soft idle: stopping worker to release VRAM")
        await self.stop()

    async def submit_job(self, job: dict[str, Any]) -> JobResult:
        """Submit a TTS job to the OpenVoice worker."""
        self.ensure_ready()
        if self.state == WorkloadState.STOPPED:
            await self.start()
        self.state = WorkloadState.BUSY
        self.touch()
        job_id = str(uuid.uuid4())

        try:
            text = job.get("text", "")
            voice_id = job.get("voice_id")
            accent = job.get("accent", "en-us")
            speed = job.get("speed", 1.0)

            async with httpx.AsyncClient(timeout=60) as client:
                resp = await client.post(
                    f"{self.base_url}/synthesize",
                    json={
                        "text": text,
                        "voice_id": voice_id,
                        "accent": accent,
                        "speed": speed,
                    },
                )
                resp.raise_for_status()
                audio_bytes = resp.content
                content_type = resp.headers.get("content-type", "audio/mpeg")

            self.state = WorkloadState.READY
            return JobResult(
                job_id=job_id,
                status=JobStatus.DONE,
                outputs=[
                    {
                        "kind": "audio",
                        "mime_type": content_type,
                        "bytes": audio_bytes,
                        "asset_token": f"audio-{uuid.uuid4()}",
                    }
                ],
                summary=f"OpenVoice TTS job {job_id} completed",
            )
        except Exception as exc:
            self.state = WorkloadState.READY
            logger.exception("OpenVoice job %s failed", job_id)
            return JobResult(
                job_id=job_id,
                status=JobStatus.FAILED,
                error=str(exc),
                summary=f"OpenVoice job {job_id} failed: {exc}",
            )

    async def register_voice(
        self, name: str, clip_bytes: bytes, filename: str, mime_type: str
    ) -> dict[str, Any]:
        """Upload a reference clip to the worker for speaker embedding extraction."""
        if self.state == WorkloadState.STOPPED:
            await self.start()
        self.touch()
        async with httpx.AsyncClient(timeout=60) as client:
            files = {"clip": (filename, clip_bytes, mime_type)}
            data = {"name": name}
            resp = await client.post(
                f"{self.base_url}/register_voice",
                files=files,
                data=data,
            )
            resp.raise_for_status()
            return resp.json()
