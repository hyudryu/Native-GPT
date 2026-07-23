"""FastAPI app for the AgentGPT remote backend host (bridge).

Exposes workload lifecycle, job submission, voice management, and asset
retrieval. See ADR-0008 for architecture.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Any

import httpx
from fastapi import Depends, FastAPI, File, Form, HTTPException, UploadFile, status
from fastapi.responses import Response

from .auth import verify_auth
from .config import BridgeConfig, resolve_token
from .manager import WorkloadManager
from .workloads import ComfyUIWorkload, FakeWorkload, OpenVoiceWorkload

logger = logging.getLogger(__name__)

BRIDGE_VERSION = "0.1.0"


def build_manager(config: BridgeConfig) -> WorkloadManager:
    """Build the workload manager from config."""
    manager = WorkloadManager()
    if config.use_fake_workloads:
        manager.register(FakeWorkload())
        return manager
    if config.enable_comfyui:
        manager.register(ComfyUIWorkload())
    if config.enable_openvoice:
        manager.register(OpenVoiceWorkload())
    return manager


def create_app(config: BridgeConfig | None = None) -> FastAPI:
    """Create the FastAPI application."""
    if config is None:
        config = BridgeConfig(token=resolve_token())

    manager = build_manager(config)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        manager.start_idle_loop()
        logger.info("bridge started (version %s)", BRIDGE_VERSION)
        yield
        await manager.shutdown()

    app = FastAPI(
        title="AgentGPT Bridge",
        version=BRIDGE_VERSION,
        description="Remote backend host managing GPU workloads (ComfyUI, OpenVoice)",
        lifespan=lifespan,
        dependencies=[Depends(verify_auth)],
    )
    app.state.manager = manager
    app.state.config = config

    _register_routes(app, manager)
    return app


def _register_routes(app: FastAPI, manager: WorkloadManager) -> None:
    @app.get("/health")
    async def health() -> dict[str, Any]:
        workloads = {}
        for wl in manager.list():
            info = wl.info()
            workloads[info.id] = {
                "state": info.state,
                "healthy": info.healthy,
                "version": info.version,
                "description": info.description,
            }
        return {"version": BRIDGE_VERSION, "workloads": workloads}

    @app.get("/workloads")
    async def list_workloads() -> dict[str, Any]:
        return {"workloads": [w.model_dump() for w in manager.infos()]}

    @app.get("/workloads/{workload_id}")
    async def get_workload(workload_id: str) -> dict[str, Any]:
        wl = manager.get(workload_id)
        if wl is None:
            raise HTTPException(
                status.HTTP_404_NOT_FOUND,
                detail=f"workload {workload_id} not found",
            )
        return {"workload": wl.info().model_dump()}

    @app.post("/workloads/{workload_id}/start")
    async def start_workload(workload_id: str) -> dict[str, Any]:
        wl = await manager.ensure_started(workload_id)
        return {"workload_id": workload_id, "state": wl.state.value}

    @app.post("/workloads/{workload_id}/stop")
    async def stop_workload(workload_id: str) -> dict[str, Any]:
        await manager.stop(workload_id)
        return {"workload_id": workload_id, "state": "stopped"}

    @app.post("/workloads/{workload_id}/jobs")
    async def submit_job(workload_id: str, job: dict[str, Any]) -> dict[str, Any]:
        try:
            result = await manager.submit_job(workload_id, job)
        except KeyError:
            raise HTTPException(
                status.HTTP_404_NOT_FOUND, detail=f"workload {workload_id} not found"
            ) from None
        except RuntimeError as exc:
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
            ) from exc
        return {
            "job_id": result.job_id,
            "status": result.status.value,
            "outputs": [
                {k: v for k, v in o.items() if k != "bytes"} for o in result.outputs
            ],
            "error": result.error,
            "summary": result.summary,
        }

    @app.get("/workloads/{workload_id}/jobs/{job_id}")
    async def job_status(workload_id: str, job_id: str) -> dict[str, Any]:
        # Jobs are synchronous in this version; status is derived from the
        # result stored by asset_token. This endpoint exists for future async
        # job support.
        return {"job_id": job_id, "workload_id": workload_id, "status": "done"}

    @app.get("/assets/{asset_token}")
    async def fetch_asset(asset_token: str) -> Response:
        asset = manager.get_asset(asset_token)
        if asset is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="asset not found or expired")
        data, mime_type = asset
        return Response(content=data, media_type=mime_type)

    # ---- OpenVoice voice management ----

    @app.get("/workloads/openvoice/voices")
    async def list_voices() -> dict[str, Any]:
        wl = manager.get("openvoice")
        if wl is None or not isinstance(wl, OpenVoiceWorkload):
            return {"voices": []}
        # The worker owns the voice registry; proxy its list.
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(f"{wl.base_url}/voices")
                resp.raise_for_status()
                return resp.json()
        except Exception as exc:
            logger.warning("failed to list voices from openvoice worker: %s", exc)
            return {"voices": [], "warning": str(exc)}

    @app.post("/workloads/openvoice/voices")
    async def upload_voice(
        name: str = Form(...),  # noqa: B008
        clip: UploadFile = File(...),  # noqa: B008
    ) -> dict[str, Any]:
        wl = manager.get("openvoice")
        if wl is None or not isinstance(wl, OpenVoiceWorkload):
            raise HTTPException(
                status.HTTP_404_NOT_FOUND, detail="openvoice workload not available"
            )
        clip_bytes = await clip.read()
        result = await wl.register_voice(
            name=name,
            clip_bytes=clip_bytes,
            filename=clip.filename or "clip.mp3",
            mime_type=clip.content_type or "audio/mpeg",
        )
        return result

    @app.delete("/workloads/openvoice/voices/{voice_id}")
    async def delete_voice(voice_id: str) -> dict[str, Any]:
        wl = manager.get("openvoice")
        if wl is not None and isinstance(wl, OpenVoiceWorkload):
            try:
                async with httpx.AsyncClient(timeout=15) as client:
                    await client.delete(f"{wl.base_url}/voices/{voice_id}")
            except Exception as exc:
                # Best-effort: the voice may already be gone on the worker.
                logger.warning("failed to delete voice %s on worker: %s", voice_id, exc)
        return {"deleted": voice_id}
