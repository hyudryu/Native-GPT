"""Tests for tools/remote-host-status/tool.py."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "_lib"))
from testloader import cleanup_tool_module, load_tool_module  # noqa: E402

_MODULE_NAME = "remote_host_status_tool_under_test"


@pytest.fixture()
def mod():
    m = load_tool_module(__file__, _MODULE_NAME)
    yield m
    cleanup_tool_module(_MODULE_NAME)


async def test_no_hosts_returns_capability_unavailable(mod, monkeypatch):
    """When no hosts are configured, capabilities are disabled with guidance."""
    # Stub the bridge client to return an empty host list.
    async def fake_get(path, timeout=30.0):
        return {"hosts": []}

    monkeypatch.setattr(mod._bridge, "api_get", fake_get)

    result = await mod.check_status()
    assert result["ok"] is True
    assert result["data"]["hosts"] == []
    caps = result["data"]["capabilities"]
    assert caps["comfyui_generation"]["enabled"] is False
    assert caps["comfyui_generation"]["status"] == "no_remote_host"
    assert caps["openvoice_tts"]["enabled"] is False


async def test_lists_hosts_and_capabilities(mod, monkeypatch):
    """When a healthy host exists, capabilities are enabled."""

    async def fake_get(path, timeout=30.0):
        return {
            "hosts": [
                {
                    "id": "host-1",
                    "name": "DGX Spark",
                    "base_url": "http://dgx.local:8443",
                    "status": "reachable",
                    "has_token": True,
                    "workloads": {
                        "comfyui": {"state": "ready", "healthy": True},
                        "openvoice": {"state": "stopped", "healthy": False},
                    },
                }
            ]
        }

    monkeypatch.setattr(mod._bridge, "api_get", fake_get)

    result = await mod.check_status()
    assert result["ok"] is True
    assert len(result["data"]["hosts"]) == 1
    caps = result["data"]["capabilities"]
    assert caps["comfyui_generation"]["enabled"] is True
    assert caps["openvoice_tts"]["enabled"] is False


async def test_server_error_returns_error(mod, monkeypatch):
    """When the server is unreachable, return an error result."""

    def raise_error(path, timeout=30.0):
        raise mod._bridge.BridgeClientError("http_503", "server unavailable", 503)

    monkeypatch.setattr(mod._bridge, "api_get", raise_error)

    result = await mod.check_status()
    assert result["ok"] is False
    assert result["error"]["code"] == "http_503"
