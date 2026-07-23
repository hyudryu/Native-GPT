"""Tests for tools/comfyui-generate/tool.py — validation and result handling."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "_lib"))
from testloader import cleanup_tool_module, load_tool_module  # noqa: E402

_MODULE_NAME = "comfyui_generate_tool_under_test"


@pytest.fixture()
def mod():
    m = load_tool_module(__file__, _MODULE_NAME)
    yield m
    cleanup_tool_module(_MODULE_NAME)


async def test_empty_prompt_rejected(mod):
    result = await mod.generate("")
    assert result["ok"] is False
    assert result["error"]["code"] == "bad_request"


async def test_invalid_kind_rejected(mod):
    result = await mod.generate("a cat", kind="audio")
    assert result["ok"] is False
    assert "kind must be" in result["error"]["message"]


async def test_prompt_too_long_rejected(mod):
    result = await mod.generate("x" * (mod.MAX_PROMPT_LENGTH + 1))
    assert result["ok"] is False
    assert "too long" in result["error"]["message"]


async def test_successful_generation_returns_asset(mod, monkeypatch):
    async def fake_post(path, json=None, timeout=30.0):
        return {
            "ok": True,
            "asset_id": "asset-123",
            "asset_url": "/api/assets/asset-123",
            "host": "DGX Spark",
            "summary": "Generated image successfully",
        }

    monkeypatch.setattr(mod._bridge, "api_post", fake_post)

    result = await mod.generate("a sunset over the ocean", kind="image")
    assert result["ok"] is True
    assert result["data"]["asset_id"] == "asset-123"
    assert result["data"]["kind"] == "image"
    assert result["data"]["asset_url"] is not None
    assert "asset-123" in result["data"]["asset_url"]


async def test_bridge_error_handled(mod, monkeypatch):
    async def raise_error(path, json=None, timeout=30.0):
        raise mod._bridge.BridgeClientError("bridge_unreachable", "connection refused")

    monkeypatch.setattr(mod._bridge, "api_post", raise_error)

    result = await mod.generate("a cat")
    assert result["ok"] is False
    assert result["error"]["code"] == "bridge_unreachable"
