"""Load bridge MCP server configs into Strands ``MCPClient`` instances.

The desktop host generates ``app-data/mcp_servers.json`` from its
``remote_hosts`` table (one entry per host with a keychain token) and points
the sidecar at it via the ``AGENTGPT_MCP_SERVERS`` env var. Each entry is a
streamable-http MCP server (the bridge's ``/mcp`` endpoint) with a bearer
``Authorization`` header and an optional non-standard ``tls_verify: false``
flag for self-signed bridge certificates.

Design: ``docs/superpowers/specs/2026-07-22-bridge-mcp-server-design.md``.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

ENV_VAR = "AGENTGPT_MCP_SERVERS"

# MCP tool calls proxy GPU jobs (ComfyUI video, OpenVoice TTS) that can run
# for minutes server-side, so the read timeout must be generous. Connect is
# short so an unreachable host fails fast (and, with continue_on_error, just
# yields no tools from that server).
HTTP_TIMEOUT_SECONDS = 600.0
CONNECT_TIMEOUT_SECONDS = 15.0
STARTUP_TIMEOUT_SECONDS = 25


def config_path() -> Path | None:
    """Config file path from the environment; None when unset/empty."""
    raw = os.environ.get(ENV_VAR, "").strip()
    return Path(raw) if raw else None


def load_mcp_servers_config(path: Path) -> dict[str, Any]:
    """Read the ``mcpServers`` mapping; missing/malformed file maps to ``{}``.

    Tolerates both the nested ``{"mcpServers": {...}}`` shape (what the
    desktop host writes) and a flat name→config mapping (what Strands'
    ``MCPClient.load_servers`` also accepts).
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("mcp servers config unreadable (%s): %s", path, exc)
        return {}
    if not isinstance(data, dict):
        logger.warning("mcp servers config (%s) is not an object; ignoring", path)
        return {}
    servers = data.get("mcpServers", data)
    if not isinstance(servers, dict):
        logger.warning("mcp servers config (%s) has no mcpServers object; ignoring", path)
        return {}
    return servers


def _build_transport(url: str, headers: dict[str, str], tls_verify: bool) -> Callable[[], Any]:
    """Build a streamable-http transport callable for Strands ``MCPClient``.

    The callable is invoked (and its async context manager entered/exited)
    inside the MCPClient's background event loop, so the httpx client is
    created and closed on that loop. mcp 1.28.1's ``streamable_http_client``
    accepts a pre-configured ``http_client`` and does NOT close it, so we wrap
    it to close the client on exit — this is also how ``tls_verify: false``
    (self-signed bridge certs) is honored.
    """

    def transport() -> Any:
        import contextlib  # noqa: PLC0415

        import httpx  # noqa: PLC0415
        from mcp.client.streamable_http import streamable_http_client  # noqa: PLC0415

        @contextlib.asynccontextmanager
        async def connect() -> Any:
            client = httpx.AsyncClient(
                headers=headers or None,
                verify=tls_verify,
                timeout=httpx.Timeout(HTTP_TIMEOUT_SECONDS, connect=CONNECT_TIMEOUT_SECONDS),
            )
            try:
                async with streamable_http_client(url=url, http_client=client) as streams:
                    yield streams
            finally:
                await client.aclose()

        return connect()

    return transport


def _build_client(name: str, server: dict[str, Any]) -> Any:
    """Build one Strands ``MCPClient`` from a config entry (may raise)."""
    from strands.tools.mcp import MCPClient  # noqa: PLC0415

    url = server.get("url")
    if not isinstance(url, str) or not url.strip():
        raise ValueError(f"mcp server '{name}' is missing a url")
    transport_kind = server.get("transport", "streamable-http")
    if transport_kind != "streamable-http":
        raise ValueError(f"mcp server '{name}': unsupported transport {transport_kind!r}")
    raw_headers = server.get("headers")
    headers = (
        {str(k): str(v) for k, v in raw_headers.items()} if isinstance(raw_headers, dict) else {}
    )
    tls_verify = server.get("tls_verify", True) is not False
    return MCPClient(
        _build_transport(url.strip(), headers, tls_verify),
        startup_timeout=STARTUP_TIMEOUT_SECONDS,
        # Design spec: one unreachable/dead host must not break chat. With
        # continue_on_error a failed connect yields zero tools from that
        # server instead of raising during Agent construction.
        continue_on_error=True,
    )


def load_mcp_clients(path: Path | None = None) -> list[Any]:
    """Build one Strands ``MCPClient`` per configured MCP server.

    Returns [] when no config path is set or the file is missing. Individual
    broken entries are skipped (logged) so one bad server never blocks the
    rest — the remaining servers and local tools still load.
    """
    path = path or config_path()
    if path is None:
        return []
    clients: list[Any] = []
    for name, server in load_mcp_servers_config(path).items():
        if not isinstance(server, dict):
            logger.warning("mcp server '%s': entry is not an object, skipping", name)
            continue
        if server.get("disabled"):
            continue
        try:
            clients.append(_build_client(name, server))
        except Exception as exc:  # noqa: BLE001 - config boundary must not break chat
            logger.warning("mcp server '%s' skipped: %s", name, exc)
    if clients:
        logger.info("loaded %d MCP server client(s) from %s", len(clients), path)
    return clients
