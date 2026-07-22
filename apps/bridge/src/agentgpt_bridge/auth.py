"""Bearer-token auth for the bridge (mirrors ADR-0003 constant-time compare).

Loopback connections are always allowed. Non-loopback connections must present
a valid ``Authorization: Bearer <token>`` header matching the bridge token.
"""

from __future__ import annotations

import hmac
import os

from fastapi import Header, HTTPException, Request, status

# Paths exempt from auth (health checks from the desktop's "test" button still
# need to work without a token if the user hasn't configured one yet — but in
# practice the token is always set. We exempt /health for reachability probes.)
EXEMPT_PATHS = frozenset({"/health", "/docs", "/openapi.json", "/redoc"})


def is_loopback(request: Request) -> bool:
    client = request.client
    if client is None:
        return False
    host = client.host
    return host in ("127.0.0.1", "::1", "localhost", "::ffff:127.0.0.1")


def token_matches(provided: str | None, expected: str) -> bool:
    if not provided or not expected:
        return False
    return hmac.compare_digest(provided, expected)


async def verify_auth(
    request: Request,
    authorization: str | None = Header(default=None),
) -> None:
    """FastAPI dependency: verify bearer token for non-loopback requests."""
    token = os.environ.get("AGENTGPT_BRIDGE_TOKEN", "")
    # If no token is configured, allow all (dev mode). In production the token
    # is always set.
    if not token:
        return
    # Loopback always passes (mirrors auth.rs).
    if is_loopback(request):
        return
    if request.url.path in EXEMPT_PATHS:
        return
    provided = None
    if authorization:
        parts = authorization.split(" ", 1)
        if len(parts) == 2 and parts[0].lower() == "bearer":
            provided = parts[1].strip()
    if not token_matches(provided, token):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error": {"code": "unauthorized", "message": "invalid or missing token"}},
            headers={"WWW-Authenticate": "Bearer"},
        )
