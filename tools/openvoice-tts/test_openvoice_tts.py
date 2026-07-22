"""Tests for tools/openvoice-tts/tool.py — validation and result handling."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "_lib"))
from testloader import cleanup_tool_module, load_tool_module  # noqa: E402

_MODULE_NAME = "openvoice_tts_tool_under_test"


@pytest.fixture()
def mod():
    m = load_tool_module(__file__, _MODULE_NAME)
    yield m
    cleanup_tool_module(_MODULE_NAME)


async def test_empty_text_rejected(mod):
    result = await mod.synthesize("")
    assert result["ok"] is False
    assert result["error"]["code"] == "bad_request"


async def test_text_too_long_rejected(mod):
    result = await mod.synthesize("x" * (mod.MAX_TEXT_LENGTH + 1))
    assert result["ok"] is False
    assert "too long" in result["error"]["message"]


async def test_successful_synthesis_returns_asset(mod, monkeypatch):
    async def fake_post(path, json=None, timeout=30.0):
        return {
            "ok": True,
            "asset_id": "audio-456",
            "asset_url": "/api/assets/audio-456",
            "host": "DGX Spark",
            "summary": "TTS completed",
        }

    monkeypatch.setattr(mod._bridge, "api_post", fake_post)

    result = await mod.synthesize("Hello world", voice_id="voice-1")
    assert result["ok"] is True
    assert result["data"]["asset_id"] == "audio-456"
    assert result["data"]["voice_id"] == "voice-1"


async def test_bridge_error_handled(mod, monkeypatch):
    def raise_error(path, json=None, timeout=30.0):
        raise mod._bridge.BridgeClientError("workload_unavailable", "openvoice not started")

    monkeypatch.setattr(mod._bridge, "api_post", raise_error)

    result = await mod.synthesize("Hello")
    assert result["ok"] is False
    assert result["error"]["code"] == "workload_unavailable"
