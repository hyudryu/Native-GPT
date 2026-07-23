"""Shared bridge operations used by both the REST routes and the MCP tools.

Keeping the logic here means the MCP tool layer and the REST API never drift:
both call the same helpers, which call the same ``WorkloadManager`` methods.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from .manager import WorkloadManager
from .workloads.base import JobResult, JobStatus
from .workloads.openvoice import OpenVoiceWorkload

logger = logging.getLogger(__name__)

# Trim echoed prompt/text in tool responses (mirrors the old desktop tools).
ECHO_LIMIT = 200


def workload_snapshot(manager: WorkloadManager) -> list[dict[str, Any]]:
    """Capability snapshot of all registered workloads."""
    snapshot: list[dict[str, Any]] = []
    for wl in manager.list():
        info = wl.info()
        idle = wl.idle_for()
        snapshot.append(
            {
                "id": info.id,
                "name": info.name,
                "state": info.state,
                "healthy": info.healthy,
                "description": info.description,
                # inf means "never used"; report None for JSON friendliness.
                "idle_seconds": None if idle == float("inf") else round(idle, 1),
                "soft_idle_seconds": wl.soft_idle_seconds,
                "hard_idle_seconds": wl.hard_idle_seconds,
            }
        )
    return snapshot


def job_result_to_tool_response(
    result: JobResult,
    base_url: str,
    extra_data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Convert a JobResult into the standard tool response shape.

    ``{"ok": bool, "summary": str, "data": {...}, "error": str | None}``
    with a full absolute ``asset_url`` for the first binary output.
    """
    data: dict[str, Any] = dict(extra_data or {})
    outputs = [
        {k: v for k, v in o.items() if k != "bytes"} for o in result.outputs
    ]
    if outputs:
        first = outputs[0]
        token = first.get("asset_token")
        data["asset_id"] = token
        data["asset_url"] = f"{base_url}/assets/{token}" if token else None
        data["mime_type"] = first.get("mime_type")
        data["kind"] = first.get("kind")
        data["outputs"] = outputs
    ok = result.status == JobStatus.DONE
    return {
        "ok": ok,
        "summary": result.summary,
        "data": data,
        "error": None if ok else (result.error or "job failed"),
    }


async def submit_job(
    manager: WorkloadManager, workload_id: str, job: dict[str, Any]
) -> JobResult:
    """Submit a job through the manager (same path as REST POST .../jobs)."""
    return await manager.submit_job(workload_id, job)


# ---- OpenVoice voice registry ----


def _get_openvoice(manager: WorkloadManager) -> OpenVoiceWorkload | None:
    wl = manager.get("openvoice")
    return wl if isinstance(wl, OpenVoiceWorkload) else None


async def list_voices(manager: WorkloadManager) -> dict[str, Any]:
    """List registered voices by proxying the OpenVoice worker."""
    wl = _get_openvoice(manager)
    if wl is None:
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


async def register_voice(
    manager: WorkloadManager,
    name: str,
    clip_bytes: bytes,
    filename: str,
    mime_type: str,
) -> dict[str, Any]:
    """Register a voice clip (bytes) with the OpenVoice worker.

    Returns the worker's response (includes ``voice_id`` on success or an
    ``error`` key on failure), or ``{"error": ...}`` if unavailable.
    """
    wl = _get_openvoice(manager)
    if wl is None:
        return {"error": "openvoice workload not available"}
    return await wl.register_voice(
        name=name, clip_bytes=clip_bytes, filename=filename, mime_type=mime_type
    )


async def delete_voice(manager: WorkloadManager, voice_id: str) -> dict[str, Any]:
    """Delete a voice (best-effort on the worker)."""
    wl = _get_openvoice(manager)
    if wl is not None:
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                await client.delete(f"{wl.base_url}/voices/{voice_id}")
        except Exception as exc:
            # Best-effort: the voice may already be gone on the worker.
            logger.warning("failed to delete voice %s on worker: %s", voice_id, exc)
    return {"deleted": voice_id}
