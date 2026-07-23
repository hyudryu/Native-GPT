"""Tests for the workload lifecycle and manager."""

from __future__ import annotations

import pytest

from agentgpt_bridge.manager import WorkloadManager
from agentgpt_bridge.workloads import FakeWorkload, WorkloadState


@pytest.fixture()
def manager() -> WorkloadManager:
    mgr = WorkloadManager()
    mgr.register(FakeWorkload())
    return mgr


async def test_fake_workload_starts_and_stops() -> None:
    wl = FakeWorkload()
    assert wl.state == WorkloadState.STOPPED
    await wl.start()
    assert wl.state == WorkloadState.READY
    assert wl.start_call_count == 1
    await wl.stop()
    assert wl.state == WorkloadState.STOPPED
    assert wl.stop_call_count == 1


async def test_fake_workload_submits_job(manager: WorkloadManager) -> None:
    result = await manager.submit_job("fake", {"prompt": "a cat", "output_kind": "image"})
    assert result.status.value == "done"
    assert len(result.outputs) == 1
    assert result.outputs[0]["kind"] == "image"
    assert result.outputs[0]["mime_type"] == "image/png"
    assert "bytes" in result.outputs[0]
    assert "asset_token" in result.outputs[0]


async def test_job_stores_asset_for_retrieval(manager: WorkloadManager) -> None:
    result = await manager.submit_job("fake", {"prompt": "a dog", "output_kind": "image"})
    token = result.outputs[0]["asset_token"]
    asset = manager.get_asset(token)
    assert asset is not None
    data, mime = asset
    assert data == b"FAKE_IMAGE_DATA"
    assert mime == "image/png"


async def test_unknown_workload_raises(manager: WorkloadManager) -> None:
    with pytest.raises(KeyError, match="unknown workload"):
        await manager.submit_job("nonexistent", {})


async def test_ensure_started_starts_stopped_workload(manager: WorkloadManager) -> None:
    wl = manager.get("fake")
    assert wl is not None
    assert wl.state == WorkloadState.STOPPED
    await manager.ensure_started("fake")
    assert wl.state == WorkloadState.READY


async def test_tts_job_produces_audio_output(manager: WorkloadManager) -> None:
    result = await manager.submit_job(
        "fake", {"text": "hello world", "voice_id": None, "accent": "en-us", "speed": 1.0}
    )
    assert result.status.value == "done"
    assert len(result.outputs) == 1
    assert result.outputs[0]["kind"] == "audio"
    assert result.outputs[0]["mime_type"] == "audio/mpeg"


async def test_soft_idle_releases_vram(manager: WorkloadManager) -> None:
    wl = manager.get("fake")
    assert wl is not None
    wl.soft_idle_seconds = 0.0  # immediately idle
    await manager.ensure_started("fake")
    assert wl.state == WorkloadState.READY
    await wl.soft_idle()
    assert wl.soft_idle_call_count == 1
