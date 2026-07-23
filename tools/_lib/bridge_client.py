"""Shared HTTP client for remote-host tools.

Tools call the desktop server's REST API (which proxies to the bridge) rather
than talking to the bridge directly. The desktop server's port and token are
exposed via env vars by the host at startup:

  AGENTGPT_SERVER_PORT  — the desktop server's localhost port
  AGENTGPT_SERVER_TOKEN — the desktop server's bearer token

All requests go through localhost, so auth is technically exempt, but the
token is sent anyway for correctness in non-loopback deployments.
"""

from __future__ import annotations

import os
from typing import Any

import httpx

DEFAULT_TIMEOUT = 30.0


class BridgeClientError(Exception):
    """Raised when a call to the desktop server fails."""

    def __init__(self, code: str, message: str, status: int = 0) -> None:
        self.code = code
        self.message = message
        self.status = status
        super().__init__(f"{code}: {message}")


def _server_url() -> str:
    port = os.environ.get("AGENTGPT_SERVER_PORT", "8787")
    return f"http://127.0.0.1:{port}"


def _auth_headers() -> dict[str, str]:
    token = os.environ.get("AGENTGPT_SERVER_TOKEN", "")
    if token:
        return {"Authorization": f"Bearer {token}"}
    return {}


async def api_get(path: str, timeout: float = DEFAULT_TIMEOUT) -> Any:
    """GET a JSON endpoint on the desktop server."""
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.get(
            f"{_server_url()}{path}", headers=_auth_headers(), timeout=timeout
        )
    return _handle(resp)


async def api_post(path: str, json: Any = None, timeout: float = DEFAULT_TIMEOUT) -> Any:
    """POST JSON to the desktop server."""
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(
            f"{_server_url()}{path}", json=json, headers=_auth_headers(), timeout=timeout
        )
    return _handle(resp)


async def api_post_multipart(
    path: str, fields: dict[str, Any], files: dict[str, tuple[str, bytes, str]],
    timeout: float = DEFAULT_TIMEOUT,
) -> Any:
    """POST multipart form data to the desktop server."""
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(
            f"{_server_url()}{path}",
            data=fields,
            files=files,
            headers=_auth_headers(),
            timeout=timeout,
        )
    return _handle(resp)


def _handle(resp: httpx.Response) -> Any:
    if resp.status_code == 204:
        return None
    try:
        body = resp.json()
    except (ValueError, httpx.DecodingError):
        if not resp.status_code < 400:
            raise BridgeClientError(
                f"http_{resp.status_code}",
                resp.text[:500] if resp.text else f"status {resp.status_code}",
                resp.status_code,
            ) from None
        return None
    if not resp.status_code < 400:
        err = body.get("error", {})
        raise BridgeClientError(
            err.get("code", f"http_{resp.status_code}"),
            err.get("message", f"status {resp.status_code}"),
            resp.status_code,
        )
    return body


def server_base_url() -> str:
    """The desktop server's base URL (for constructing asset URLs)."""
    return _server_url()
