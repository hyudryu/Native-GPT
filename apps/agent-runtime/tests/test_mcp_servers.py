"""Tests for MCP server config loading and Strands MCPClient wiring.

Covers the desktop-side half of the bridge-MCP design: loading
``mcp_servers.json`` (written by the Rust host from its remote_hosts table),
tolerating missing/broken config, honoring ``tls_verify: false``, and the
"one unreachable host must not break chat" guarantee.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import socket
import sys
from typing import Any

import httpx
import pytest

from agentgpt_runtime.mcp_servers import (
    _build_client,
    _build_transport,
    config_path,
    load_mcp_clients,
    load_mcp_servers_config,
)


@pytest.fixture(autouse=True)
def _unload_strands_after_test() -> Any:
    """Keep the sidecar's startup-import invariant intact.

    test_protocol.py asserts ``strands`` is absent from sys.modules (the
    sidecar imports it lazily to keep startup fast). These tests exercise the
    real SDK in-process, so scrub it from sys.modules afterwards.
    """
    yield
    for name in [m for m in sys.modules if m == "strands" or m.startswith("strands.")]:
        sys.modules.pop(name, None)


def _write_config(tmp_path: Any, servers: dict[str, Any], *, nested: bool = True) -> Any:
    doc = {"mcpServers": servers} if nested else servers
    path = tmp_path / "mcp_servers.json"
    path.write_text(json.dumps(doc), encoding="utf-8")
    return path


def _unused_port() -> int:
    """A port that is (almost certainly) not listening, for refusal tests."""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


# ---- config file reading ----


def test_config_path_from_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    monkeypatch.setenv("AGENTGPT_MCP_SERVERS", str(tmp_path / "mcp_servers.json"))
    assert config_path() == tmp_path / "mcp_servers.json"


def test_config_path_unset_or_blank(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AGENTGPT_MCP_SERVERS", raising=False)
    assert config_path() is None
    monkeypatch.setenv("AGENTGPT_MCP_SERVERS", "   ")
    assert config_path() is None


def test_load_config_tolerates_missing_file(tmp_path: Any) -> None:
    assert load_mcp_servers_config(tmp_path / "nope.json") == {}


def test_load_config_tolerates_malformed_json(tmp_path: Any) -> None:
    path = tmp_path / "mcp_servers.json"
    path.write_text("{not json", encoding="utf-8")
    assert load_mcp_servers_config(path) == {}


def test_load_config_reads_nested_and_flat_shapes(tmp_path: Any) -> None:
    nested = _write_config(tmp_path, {"a": {"url": "https://a/mcp"}})
    assert "a" in load_mcp_servers_config(nested)
    flat = _write_config(tmp_path, {"b": {"url": "https://b/mcp"}}, nested=False)
    assert "b" in load_mcp_servers_config(flat)


# ---- client construction ----


class _FakeMCPClient:
    """Stands in for strands' MCPClient; records constructor args."""

    def __init__(self, transport: Any, **kwargs: Any) -> None:
        self.transport = transport
        self.kwargs = kwargs


def test_load_mcp_clients_builds_one_client_per_enabled_server(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    monkeypatch.setattr("strands.tools.mcp.MCPClient", _FakeMCPClient)
    path = _write_config(
        tmp_path,
        {
            "bridge-a": {
                "url": "https://a:8443/mcp",
                "transport": "streamable-http",
                "headers": {"Authorization": "Bearer ta"},
            },
            "bridge-disabled": {"url": "https://b:8443/mcp", "disabled": True},
            "bridge-no-url": {"transport": "streamable-http"},
            "bridge-bad-shape": "not-an-object",
        },
    )
    clients = load_mcp_clients(path)
    assert len(clients) == 1
    client = clients[0]
    assert isinstance(client, _FakeMCPClient)
    # One unreachable host must not break chat: failed connects yield no tools.
    assert client.kwargs["continue_on_error"] is True
    assert client.kwargs["startup_timeout"] > 0
    assert callable(client.transport)


def test_load_mcp_clients_missing_file_returns_empty(tmp_path: Any) -> None:
    assert load_mcp_clients(tmp_path / "missing.json") == []


def test_load_mcp_clients_no_env_returns_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AGENTGPT_MCP_SERVERS", raising=False)
    assert load_mcp_clients() == []


def test_load_mcp_clients_uses_env_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    monkeypatch.setattr("strands.tools.mcp.MCPClient", _FakeMCPClient)
    path = _write_config(tmp_path, {"bridge": {"url": "https://gx10:8443/mcp"}})
    monkeypatch.setenv("AGENTGPT_MCP_SERVERS", str(path))
    assert len(load_mcp_clients()) == 1


# ---- transport: headers + tls_verify ----


def test_transport_passes_headers_and_honors_tls_verify(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorded: dict[str, Any] = {}

    class FakeAsyncClient:
        def __init__(self, **kwargs: Any) -> None:
            recorded["client_kwargs"] = kwargs

        async def aclose(self) -> None:
            recorded["closed"] = True

    @contextlib.asynccontextmanager
    async def fake_streamable_http_client(url: str, http_client: Any = None, **_: Any) -> Any:
        recorded["url"] = url
        recorded["http_client"] = http_client
        yield ("read", "write", "get_session_id")

    monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)
    monkeypatch.setattr(
        "mcp.client.streamable_http.streamable_http_client", fake_streamable_http_client
    )

    transport = _build_transport(
        "https://gx10:8443/mcp", {"Authorization": "Bearer t"}, tls_verify=False
    )

    async def run() -> None:
        async with transport():
            pass

    asyncio.run(run())
    assert recorded["url"] == "https://gx10:8443/mcp"
    assert recorded["client_kwargs"]["headers"] == {"Authorization": "Bearer t"}
    assert recorded["client_kwargs"]["verify"] is False
    assert isinstance(recorded["http_client"], FakeAsyncClient)
    # mcp does not close caller-provided clients; our wrapper must.
    assert recorded["closed"] is True


def test_transport_defaults_to_tls_verification(monkeypatch: pytest.MonkeyPatch) -> None:
    recorded: dict[str, Any] = {}

    class FakeAsyncClient:
        def __init__(self, **kwargs: Any) -> None:
            recorded["client_kwargs"] = kwargs

        async def aclose(self) -> None:
            pass

    @contextlib.asynccontextmanager
    async def fake_streamable_http_client(url: str, http_client: Any = None, **_: Any) -> Any:
        yield ("read", "write", "get_session_id")

    monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)
    monkeypatch.setattr(
        "mcp.client.streamable_http.streamable_http_client", fake_streamable_http_client
    )

    transport = _build_transport("http://bridge:8443/mcp", {}, tls_verify=True)

    async def run() -> None:
        async with transport():
            pass

    asyncio.run(run())
    assert recorded["client_kwargs"]["verify"] is True


# ---- integration with the real Strands MCPClient / registry ----


def test_unreachable_server_yields_no_tools() -> None:
    """The core robustness guarantee, against the real Strands MCPClient.

    A dead host raises on connect; with continue_on_error=True the client
    swallows it and contributes zero tools instead of breaking the run.
    """
    client = _build_client("dead", {"url": f"http://127.0.0.1:{_unused_port()}/mcp"})
    assert asyncio.run(client.load_tools()) == []
    assert client.connection_failed is True
    client.stop(None, None, None)


def test_mcp_tools_appear_in_agent_tool_list() -> None:
    """A ToolProvider in the tools list registers its tools in the registry.

    This is the path chat.py relies on when it passes MCP clients into the
    Strands Agent alongside local manifest tools.
    """
    from unittest.mock import AsyncMock, Mock  # noqa: PLC0415

    from strands import tool  # noqa: PLC0415
    from strands.tools.mcp import MCPClient  # noqa: PLC0415
    from strands.tools.registry import ToolRegistry  # noqa: PLC0415

    @tool
    def comfyui_generate(prompt: str) -> str:
        """Generate an image from a text prompt.

        Args:
            prompt: What to draw.
        """
        return prompt

    provider = Mock(spec=MCPClient)
    provider.load_tools = AsyncMock(return_value=[comfyui_generate])

    registry = ToolRegistry()
    names = registry.process_tools([provider])
    assert "comfyui_generate" in names
    assert "comfyui_generate" in registry.registry

    # chat.py calls registry.cleanup() at the end of a run to stop providers.
    registry.cleanup()
    provider.remove_consumer.assert_called()
