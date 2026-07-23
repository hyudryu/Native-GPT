"""ComfyUI workload controller.

Manages a ComfyUI server (`python main.py`) as a subprocess and talks to its
HTTP/WebSocket API. VRAM is released via POST /free on soft idle — no restart
needed. ComfyUI auto-loads models per workflow on first use.

Requires ComfyUI to be installed at the path configured by
AGENTGPT_COMFYUI_PATH (env var), defaulting to /opt/ComfyUI.
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

DEFAULT_COMFYUI_PATH = "/opt/ComfyUI"
DEFAULT_COMFYUI_PORT = 8188


class ComfyUIWorkload(Workload):
    """Manages a ComfyUI server subprocess."""

    workload_id: str = "comfyui"
    display_name: str = "ComfyUI"
    description: str = "Image and video generation via ComfyUI node workflows."

    def __init__(self) -> None:
        super().__init__()
        self.comfyui_path = os.environ.get("AGENTGPT_COMFYUI_PATH", DEFAULT_COMFYUI_PATH)
        self.port = int(os.environ.get("AGENTGPT_COMFYUI_PORT", DEFAULT_COMFYUI_PORT))
        self._proc: asyncio.subprocess.Process | None = None
        self._client_id: str = ""

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    async def start(self) -> None:
        """Spawn the ComfyUI server subprocess."""
        if self.state in (WorkloadState.READY, WorkloadState.BUSY):
            return
        self.state = WorkloadState.STARTING
        self.error_message = None
        exe = os.environ.get("AGENTGPT_COMFYUI_PYTHON", "python")
        args = [
            "main.py",
            "--listen",
            "127.0.0.1",
            "--port",
            str(self.port),
        ]
        try:
            self._proc = await asyncio.create_subprocess_exec(
                exe,
                *args,
                cwd=self.comfyui_path,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            self.state = WorkloadState.ERROR
            self.error_message = (
                f"failed to spawn ComfyUI at {exe}: {exc}. "
                f"Set AGENTGPT_COMFYUI_PYTHON and AGENTGPT_COMFYUI_PATH."
            )
            logger.error(self.error_message)
            return

        # Wait for the server to respond to /system_stats.
        self._client_id = str(uuid.uuid4())
        if not await self._wait_for_ready(timeout=60):
            self.state = WorkloadState.ERROR
            self.error_message = "ComfyUI did not become ready within 60s"
            logger.error(self.error_message)
            # Clean up the spawned process to avoid a zombie.
            await self.stop()
            return

        self.state = WorkloadState.READY
        self.touch()
        logger.info("ComfyUI started on port %s", self.port)

    async def _wait_for_ready(self, timeout: float = 60) -> bool:
        """Poll /system_stats until the server responds or timeout."""
        deadline = asyncio.get_event_loop().time() + timeout
        async with httpx.AsyncClient() as client:
            while asyncio.get_event_loop().time() < deadline:
                try:
                    resp = await client.get(f"{self.base_url}/system_stats", timeout=5)
                    if resp.status_code == 200:
                        self.last_health = 0.0
                        return True
                except (httpx.HTTPError, ConnectionError):
                    pass
                await asyncio.sleep(1)
        return False

    async def stop(self) -> None:
        """Terminate the ComfyUI subprocess."""
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
        """Release VRAM via POST /free — no restart needed."""
        if self.state not in (WorkloadState.READY, WorkloadState.BUSY):
            return
        try:
            async with httpx.AsyncClient() as client:
                await client.post(
                    f"{self.base_url}/free",
                    json={"unload_models": True, "free_memory": True},
                    timeout=30,
                )
            logger.info("ComfyUI VRAM released via /free")
        except httpx.HTTPError as exc:
            logger.warning("failed to free ComfyUI VRAM: %s", exc)

    async def submit_job(self, job: dict[str, Any]) -> JobResult:
        """Submit a generation job to ComfyUI.

        Supports two modes:
          - {"kind": "workflow", "workflow": <graph-json>} — raw ComfyUI graph.
          - {"kind": "generate", "prompt": "...", "model": "...", "size": ...} —
            the bridge builds a minimal txt2img graph.
        """
        self.ensure_ready()
        self.state = WorkloadState.BUSY
        self.touch()
        job_id = str(uuid.uuid4())

        try:
            workflow = self._resolve_workflow(job)
            outputs = await self._execute_workflow(workflow)
            self.state = WorkloadState.READY
            return JobResult(
                job_id=job_id,
                status=JobStatus.DONE,
                outputs=outputs,
                summary=f"ComfyUI job {job_id} completed",
            )
        except Exception as exc:
            self.state = WorkloadState.READY
            logger.exception("ComfyUI job %s failed", job_id)
            return JobResult(
                job_id=job_id,
                status=JobStatus.FAILED,
                error=str(exc),
                summary=f"ComfyUI job {job_id} failed: {exc}",
            )

    def _resolve_workflow(self, job: dict[str, Any]) -> dict[str, Any]:
        """Return the ComfyUI prompt graph for this job."""
        if job.get("workflow"):
            return job["workflow"]
        # Build a minimal txt2img graph from generate params.
        prompt_text = job.get("prompt", "")
        model = job.get("model", "sdxl_base.safetensors")
        size = job.get("size", "1024x1024")
        return self._txt2img_graph(prompt_text, model, size)

    def _txt2img_graph(self, prompt: str, model: str, size: str) -> dict[str, Any]:
        """Build a minimal ComfyUI txt2img workflow graph."""
        w, h = (int(x) for x in size.split("x")) if "x" in size else (1024, 1024)
        return {
            "3": {
                "class_type": "KSampler",
                "inputs": {
                    "seed": 0,
                    "steps": 20,
                    "cfg": 8,
                    "sampler_name": "euler",
                    "scheduler": "normal",
                    "denoise": 1,
                    "model": ["4", 0],
                    "positive": ["6", 0],
                    "negative": ["7", 0],
                    "latent_image": ["5", 0],
                },
            },
            "4": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": model}},
            "5": {
                "class_type": "EmptyLatentImage",
                "inputs": {"width": w, "height": h, "batch_size": 1},
            },
            "6": {
                "class_type": "CLIPTextEncode",
                "inputs": {"text": prompt, "clip": ["4", 1]},
            },
            "7": {
                "class_type": "CLIPTextEncode",
                "inputs": {"text": "", "clip": ["4", 1]},
            },
            "8": {
                "class_type": "VAEDecode",
                "inputs": {"samples": ["3", 0], "vae": ["4", 2]},
            },
            "9": {
                "class_type": "SaveImage",
                "inputs": {"images": ["8", 0]},
            },
        }

    async def _execute_workflow(self, workflow: dict[str, Any]) -> list[dict[str, Any]]:
        """POST /prompt, poll /history, fetch outputs via /view."""
        async with httpx.AsyncClient(timeout=120) as client:
            # Submit the prompt.
            resp = await client.post(
                f"{self.base_url}/prompt",
                json={"prompt": workflow, "client_id": self._client_id},
            )
            resp.raise_for_status()
            result = resp.json()
            prompt_id = result["prompt_id"]

            # Poll /history until the prompt completes.
            outputs_meta = await self._poll_history(client, prompt_id)

            # Fetch each output's bytes.
            outputs: list[dict[str, Any]] = []
            for _node_id, node_output in outputs_meta.items():
                for image_info in node_output.get("images", node_output.get("gifs", [])):
                    asset_token = str(uuid.uuid4())
                    img_bytes = await self._fetch_view(
                        client,
                        image_info.get("filename", ""),
                        image_info.get("subfolder", ""),
                        image_info.get("type", "output"),
                    )
                    kind = "video" if "gifs" in node_output else "image"
                    ext = Path(image_info.get("filename", "x.png")).suffix.lstrip(".") or "png"
                    mime = "video/mp4" if kind == "video" else f"image/{ext}"
                    outputs.append(
                        {
                            "kind": kind,
                            "mime_type": mime,
                            "bytes": img_bytes,
                            "asset_token": asset_token,
                        }
                    )
            return outputs

    async def _poll_history(
        self, client: httpx.AsyncClient, prompt_id: str, timeout: float = 120
    ) -> dict[str, Any]:
        """Poll GET /history/{prompt_id} until outputs are available."""
        deadline = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < deadline:
            resp = await client.get(f"{self.base_url}/history/{prompt_id}")
            resp.raise_for_status()
            history = resp.json()
            if prompt_id in history:
                return history[prompt_id].get("outputs", {})
            await asyncio.sleep(1)
        raise TimeoutError(f"ComfyUI prompt {prompt_id} did not complete within {timeout}s")

    async def _fetch_view(
        self,
        client: httpx.AsyncClient,
        filename: str,
        subfolder: str,
        kind: str,
    ) -> bytes:
        """Fetch output bytes via GET /view."""
        resp = await client.get(
            f"{self.base_url}/view",
            params={"filename": filename, "subfolder": subfolder, "type": kind},
            timeout=60,
        )
        resp.raise_for_status()
        return resp.content
