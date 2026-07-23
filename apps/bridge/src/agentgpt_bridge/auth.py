"""Bearer-token auth for the bridge (mirrors ADR-0003 constant-time compare).

Loopback connections are always allowed. Non-loopback connections must present
a valid ``Authorization: Bearer <token>`` header matching the bridge token.
"""

from __future__ import annotations

import hmac
import ipaddress
import os

from fastapi import Header, HTTPException, Request, status
from starlette.types import ASGIApp, Receive, Scope, Send

# Paths exempt from auth (health checks from the desktop's "test" button still
# need to work without a token if the user hasn't configured one yet — but in
# practice the token is always set. We exempt /health for reachability probes.)
EXEMPT_PATHS = frozenset({"/health", "/docs", "/openapi.json", "/redoc"})


def is_loopback(request: Request) -> bool:
    return is_loopback_host(request.client.host if request.client else None)


def is_loopback_host(host: str | None) -> bool:
    if not host:
        return False
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def token_matches(provided: str | None, expected: str) -> bool:
    if not provided or not expected:
        return False
    return hmac.compare_digest(provided, expected)


def is_authorized(path: str, client_host: str | None, authorization: str | None) -> bool:
    """Single source of truth for bearer-token authorization.

    Rules (mirrors ADR-0003):
      - no token configured -> allow all (dev mode)
      - loopback client -> allow
      - exempt paths (health/docs) -> allow
      - otherwise require ``Authorization: Bearer <token>`` (constant-time compare)
    """
    token = os.environ.get("AGENTGPT_BRIDGE_TOKEN", "")
    if not token:
        return True
    if is_loopback_host(client_host):
        return True
    # Normalize: strip trailing slash so /health/ matches /health etc.
    normalized = path.rstrip("/") or "/"
    if normalized in EXEMPT_PATHS:
        return True
    provided = None
    if authorization:
        parts = authorization.split(" ", 1)
        if len(parts) == 2 and parts[0].lower() == "bearer":
            provided = parts[1].strip()
    return token_matches(provided, token)


async def verify_auth(
    request: Request,
    authorization: str | None = Header(default=None),
) -> None:
    """FastAPI dependency: verify bearer token for non-loopback requests."""
    if not is_authorized(
        request.url.path,
        request.client.host if request.client else None,
        authorization,
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error": {"code": "unauthorized", "message": "invalid or missing token"}},
            headers={"WWW-Authenticate": "Bearer"},
        )


class BearerAuthMiddleware:
    """Pure-ASGI middleware enforcing bridge bearer auth on a path prefix.

    FastAPI dependencies do NOT apply to mounted sub-apps (e.g. the MCP
    server mounted at /mcp), so the mount is protected here instead. Uses the
    same ``is_authorized`` rules as the REST dependency — no drift.
    """

    def __init__(self, app: ASGIApp, path_prefix: str = "/mcp") -> None:
        self.app = app
        self.path_prefix = path_prefix

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not scope["path"].startswith(self.path_prefix):
            await self.app(scope, receive, send)
            return
        client = scope.get("client")
        client_host = client[0] if client else None
        authorization: str | None = None
        for key, value in scope.get("headers", []):
            if key.lower() == b"authorization":
                authorization = value.decode("latin-1")
                break
        if is_authorized(scope["path"], client_host, authorization):
            await self.app(scope, receive, send)
            return
        body = (
            b'{"detail":{"error":{"code":"unauthorized",'
            b'"message":"invalid or missing token"}}}'
        )
        await send(
            {
                "type": "http.response.start",
                "status": status.HTTP_401_UNAUTHORIZED,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                    (b"www-authenticate", b"Bearer"),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})
