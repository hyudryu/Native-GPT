"""Loopback HTTP client for the Rust host's internal APIs.

The Rust host exposes a loopback-only internal API (browser spec §9.1) that
Python tools use to reach trusted host services such as the Browser manager.
Connection settings come from env vars the host injects when it spawns the
Python sidecar:

  AGENTGPT_INTERNAL_URL              — e.g. http://127.0.0.1:8787 (preferred)
  AGENTGPT_INTERNAL_CAPABILITY_TOKEN — per-process bearer token (preferred)

For backward compatibility with the older sidecar contract, this pair is
also honored as a fallback:

  AGENTGPT_SERVER_PORT  — localhost port (default 8787)
  AGENTGPT_SERVER_TOKEN — bearer token

All traffic stays on loopback; the token is sent anyway so the host can
enforce per-process capability auth. Never log the token or response bodies
that may contain credentials.
"""

from __future__ import annotations

import os
from typing import Any

import httpx

DEFAULT_TIMEOUT = 30.0
DEFAULT_PORT = "8787"


class BridgeClientError(Exception):
    """Raised when the host answers an internal-API call with an HTTP error.

    `code` carries the host's error code when the body has one, otherwise
    `http_<status>`. Transport failures (timeout, connect) are NOT wrapped —
    callers catch `httpx.TimeoutException` / `httpx.HTTPError` directly.
    """

    def __init__(self, code: str, message: str, status: int = 0) -> None:
        self.code = code
        self.message = message
        self.status = status
        super().__init__(f"{code}: {message}")


def internal_base_url() -> str:
    """Base URL of the loopback internal API (no trailing slash)."""
    url = os.environ.get("AGENTGPT_INTERNAL_URL", "").strip()
    if url:
        return url.rstrip("/")
    port = os.environ.get("AGENTGPT_SERVER_PORT", DEFAULT_PORT)
    return f"http://127.0.0.1:{port}"


def internal_headers() -> dict[str, str]:
    """Auth headers for internal calls. Empty when no token is configured."""
    token = os.environ.get("AGENTGPT_INTERNAL_CAPABILITY_TOKEN", "").strip()
    if not token:
        token = os.environ.get("AGENTGPT_SERVER_TOKEN", "").strip()
    if token:
        return {"Authorization": f"Bearer {token}"}
    return {}


def internal_get(
    path: str,
    *,
    timeout: float = DEFAULT_TIMEOUT,
    client: httpx.Client | None = None,
) -> Any:
    """GET a JSON endpoint on the host's internal API.

    `client` is injectable for tests (e.g. an httpx.MockTransport-backed
    client). Returns the parsed JSON body.
    """
    owns_client = client is None
    if client is None:
        client = httpx.Client(timeout=timeout)
    try:
        resp = client.get(
            f"{internal_base_url()}{path}", headers=internal_headers(), timeout=timeout
        )
    finally:
        if owns_client:
            client.close()
    return _handle(resp)


def internal_post(
    path: str,
    payload: Any = None,
    *,
    timeout: float = DEFAULT_TIMEOUT,
    client: httpx.Client | None = None,
) -> Any:
    """POST JSON to the host's internal API. `client` is injectable for tests."""
    owns_client = client is None
    if client is None:
        client = httpx.Client(timeout=timeout)
    try:
        resp = client.post(
            f"{internal_base_url()}{path}",
            json=payload,
            headers=internal_headers(),
            timeout=timeout,
        )
    finally:
        if owns_client:
            client.close()
    return _handle(resp)


def _handle(resp: httpx.Response) -> Any:
    if resp.status_code == 204:
        return None
    # An empty body is a valid "no content" success (e.g. an endpoint that
    # legitimately returns nothing). A non-empty, non-JSON 2xx body (HTML error
    # page, misconfigured proxy) is an upstream problem that would otherwise
    # silently become None — surface it so callers see the failure.
    if 200 <= resp.status_code < 300 and not resp.content.strip():
        return None
    try:
        body = resp.json()
    except (ValueError, httpx.DecodingError):
        if resp.status_code >= 400:
            raise BridgeClientError(
                f"http_{resp.status_code}",
                resp.text[:500] if resp.text else f"status {resp.status_code}",
                resp.status_code,
            ) from None
        raise BridgeClientError(
            f"http_{resp.status_code}",
            resp.text[:500] if resp.text else f"status {resp.status_code}",
            resp.status_code,
        ) from None
    if resp.status_code >= 400:
        err = body.get("error") if isinstance(body, dict) else None
        if isinstance(err, dict):
            raise BridgeClientError(
                str(err.get("code") or f"http_{resp.status_code}"),
                str(err.get("message") or f"status {resp.status_code}"),
                resp.status_code,
            )
        raise BridgeClientError(
            f"http_{resp.status_code}", f"status {resp.status_code}", resp.status_code
        )
    return body
