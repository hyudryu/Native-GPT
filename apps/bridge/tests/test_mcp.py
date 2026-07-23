"""Tests for the bridge MCP tool layer mounted at /mcp.

Covers: tool listing, bearer auth on /mcp (via ASGI middleware), generation
happy paths through FakeWorkloads (aliased as comfyui/openvoice), asset_url
base-URL derivation, and the list_workloads snapshot.
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import AsyncIterator, Iterator
from typing import Any

import httpx
import pytest
import uvicorn
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from agentgpt_bridge.app import create_app
from agentgpt_bridge.config import BridgeConfig
from agentgpt_bridge.mcp_server import EXPECTED_TOOLS
from agentgpt_bridge.workloads.fake import FakeWorkload

REMOTE_CLIENT = ("10.20.30.40", 5555)  # non-loopback
LOOPBACK_CLIENT = ("127.0.0.1", 5555)


def _build_app(token: str = "test-token"):
    """App with fake workloads aliased to the real workload ids."""
    app = create_app(BridgeConfig(token=token, use_fake_workloads=True))
    manager = app.state.manager
    for alias in ("comfyui", "openvoice"):
        wl = FakeWorkload()
        wl.workload_id = alias
        manager.register(wl)
    return app


@pytest.fixture()
def live_server() -> Iterator[str]:
    """Run the app under uvicorn on an ephemeral port (lifespan runs)."""
    app = _build_app()
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(200):
        if server.started:
            break
        time.sleep(0.02)
    port = server.servers[0].sockets[0].getsockname()[1]
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=5)


async def _mcp_session(base: str) -> AsyncIterator[ClientSession]:
    async with streamable_http_client(f"{base}/mcp") as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            yield session


def _structured(result: Any) -> dict[str, Any]:
    """Extract the tool's dict result (structuredContent, else JSON text)."""
    if getattr(result, "structuredContent", None):
        return result.structuredContent
    return json.loads(result.content[0].text)


# ---- MCP endpoint mounting & tool listing ----


async def test_mcp_lists_expected_tools(live_server: str) -> None:
    async for session in _mcp_session(live_server):
        tools = await session.list_tools()
        names = {t.name for t in tools.tools}
        assert set(EXPECTED_TOOLS) <= names


async def test_health_reports_mcp_mounted(live_server: str) -> None:
    async with httpx.AsyncClient() as client:
        resp = await client.get(f"{live_server}/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["mcp"] is True
    assert data["mcp_path"] == "/mcp"
    # Backward-compatible: original fields still present.
    assert "version" in data and "workloads" in data


# ---- list_workloads snapshot ----


async def test_list_workloads_snapshot(live_server: str) -> None:
    async for session in _mcp_session(live_server):
        result = await session.call_tool("list_workloads", {})
        payload = _structured(result)
        assert payload["ok"] is True
        assert payload["error"] is None
        ids = {w["id"] for w in payload["data"]["workloads"]}
        assert {"fake", "comfyui", "openvoice"} <= ids
        for w in payload["data"]["workloads"]:
            assert w["state"] == "stopped"
            assert w["healthy"] is False
            assert "soft_idle_seconds" in w and "hard_idle_seconds" in w


# ---- lifecycle tools ----


async def test_start_and_stop_workload(live_server: str) -> None:
    async for session in _mcp_session(live_server):
        result = _structured(
            await session.call_tool("start_workload", {"workload_id": "comfyui"})
        )
        assert result["ok"] is True
        assert result["data"]["state"] == "ready"

        result = _structured(
            await session.call_tool("stop_workload", {"workload_id": "comfyui"})
        )
        assert result["ok"] is True
        assert result["data"]["state"] == "stopped"

        result = _structured(
            await session.call_tool("start_workload", {"workload_id": "nope"})
        )
        assert result["ok"] is False
        assert "not found" in result["error"]


# ---- generation happy paths (FakeWorkload) ----


async def test_comfyui_generate_returns_absolute_asset_url(live_server: str) -> None:
    async for session in _mcp_session(live_server):
        payload = _structured(
            await session.call_tool(
                "comfyui_generate",
                {"prompt": "a cat", "kind": "image", "size": "512x512"},
            )
        )
        assert payload["ok"] is True
        assert payload["error"] is None
        data = payload["data"]
        assert data["kind"] == "image"
        assert data["mime_type"] == "image/png"
        assert data["asset_url"].startswith(f"{live_server}/assets/")
        # The asset is actually fetchable at the returned URL.
        async with httpx.AsyncClient() as client:
            resp = await client.get(data["asset_url"])
        assert resp.status_code == 200
        assert resp.content == b"FAKE_IMAGE_DATA"


async def test_comfyui_generate_validates_input(live_server: str) -> None:
    async for session in _mcp_session(live_server):
        payload = _structured(
            await session.call_tool("comfyui_generate", {"prompt": "   "})
        )
        assert payload["ok"] is False
        assert "prompt" in payload["error"]

        payload = _structured(
            await session.call_tool(
                "comfyui_generate", {"prompt": "a cat", "kind": "hologram"}
            )
        )
        assert payload["ok"] is False
        assert "kind" in payload["error"]


async def test_openvoice_tts_happy_path(live_server: str) -> None:
    async for session in _mcp_session(live_server):
        payload = _structured(
            await session.call_tool(
                "openvoice_tts", {"text": "hello world", "speed": 99.0}
            )
        )
        assert payload["ok"] is True
        data = payload["data"]
        assert data["kind"] == "audio"
        assert data["mime_type"] == "audio/mpeg"
        assert data["asset_url"].startswith(f"{live_server}/assets/")


async def test_openvoice_tts_rejects_empty_text(live_server: str) -> None:
    async for session in _mcp_session(live_server):
        payload = _structured(await session.call_tool("openvoice_tts", {"text": "  "}))
        assert payload["ok"] is False
        assert "text" in payload["error"]


async def test_voice_tools_without_openvoice_workload(live_server: str) -> None:
    """list_voices/delete_voice work even when no real worker is running."""
    async for session in _mcp_session(live_server):
        payload = _structured(await session.call_tool("list_voices", {}))
        # Fake openvoice has no worker to proxy; either empty list or warning.
        assert "voices" in payload["data"]

        payload = _structured(
            await session.call_tool("delete_voice", {"voice_id": "v-123"})
        )
        assert payload["ok"] is True
        assert payload["data"]["deleted"] == "v-123"


# ---- bearer auth on /mcp (ASGI middleware) ----


def _post_mcp(
    app: Any, client: tuple[str, int], authorization: str | None = None
) -> httpx.Response:
    """POST a (deliberately bare) request to /mcp from a given client address.

    Authorized requests will fail *inside* the MCP handler (non-401), which
    is fine — these tests only assert auth gating.
    """
    from starlette.testclient import TestClient

    headers = {"authorization": authorization} if authorization else {}
    with TestClient(app, client=client, raise_server_exceptions=False) as c:
        return c.post("/mcp", json={"jsonrpc": "2.0", "id": 1}, headers=headers)


def test_mcp_auth_rejects_missing_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENTGPT_BRIDGE_TOKEN", "secret-token")
    app = _build_app()
    resp = _post_mcp(app, REMOTE_CLIENT)
    assert resp.status_code == 401
    assert resp.headers["www-authenticate"] == "Bearer"


def test_mcp_auth_rejects_wrong_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENTGPT_BRIDGE_TOKEN", "secret-token")
    app = _build_app()
    resp = _post_mcp(app, REMOTE_CLIENT, authorization="Bearer wrong")
    assert resp.status_code == 401


def test_mcp_auth_accepts_correct_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENTGPT_BRIDGE_TOKEN", "secret-token")
    app = _build_app()
    resp = _post_mcp(app, REMOTE_CLIENT, authorization="Bearer secret-token")
    assert resp.status_code != 401  # passed auth; MCP handler responds


def test_mcp_auth_loopback_exempt(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENTGPT_BRIDGE_TOKEN", "secret-token")
    app = _build_app()
    resp = _post_mcp(app, LOOPBACK_CLIENT)  # no token at all
    assert resp.status_code != 401


def test_mcp_auth_allow_all_when_no_token_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("AGENTGPT_BRIDGE_TOKEN", raising=False)
    app = _build_app()
    resp = _post_mcp(app, REMOTE_CLIENT)
    assert resp.status_code != 401


def test_rest_auth_still_enforced(monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression: the refactored verify_auth still guards REST routes."""
    from starlette.testclient import TestClient

    monkeypatch.setenv("AGENTGPT_BRIDGE_TOKEN", "secret-token")
    app = _build_app()
    with TestClient(app, client=REMOTE_CLIENT, raise_server_exceptions=False) as c:
        assert c.get("/workloads").status_code == 401
        assert c.get("/health").status_code == 200  # exempt path
        assert (
            c.get("/workloads", headers={"authorization": "Bearer secret-token"})
            .status_code
            == 200
        )
