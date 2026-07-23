"""Shared SSRF / unsafe-URL guard for HTTP tools.

Factored out of `tools/web-fetch/tool.py` so both `web-fetch` and `web-http`
enforce one policy: only http/https URLs, and no host that is (or resolves
to) a private, loopback, link-local, multicast, or unspecified IP — including
the cloud metadata endpoint 169.254.169.254. Mirrors `unsafe_ip` in
`crates/server/src/knowledge.rs`.

Loaded by file path from `tools/<id>/tool.py` (no package context when the
runtime imports tool modules standalone).

`allow_private` is a TEST-ONLY seam: local test servers bind 127.0.0.1, which
the guard correctly rejects in production. `@tool`-exposed functions never
expose this parameter; only internal helpers take it, defaulting to False.
"""

from __future__ import annotations

import ipaddress
import socket
from typing import Any
from urllib.parse import urlparse


class SsrfError(ValueError):
    """Raised when a URL host resolves to a forbidden IP."""


def default_resolver(host: str) -> list[str]:
    """Resolve `host` via getaddrinfo; returns [] when DNS fails."""
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return []
    # Each entry: (family, type, proto, canonname, sockaddr). sockaddr is
    # (ip, port) for IPv4 or (ip, port, flowinfo, scope_id) for IPv6.
    return [info[4][0] for info in infos]


def host_ips(
    host: str, resolver: Any = None
) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    """Resolve `host` to IP addresses using `resolver(host)` (list of str IPs).

    `resolver` is injectable so tests can stub DNS. Default uses
    `socket.getaddrinfo`.
    """
    if resolver is None:
        resolver = default_resolver
    infos = resolver(host)
    ips: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
    for raw in infos:
        try:
            ips.append(ipaddress.ip_address(raw))
        except ValueError:
            continue
    return ips


def is_forbidden_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """True if the IP is private, loopback, link-local, multicast, or unspecified.

    Mirrors `unsafe_ip` in crates/server/src/knowledge.rs.
    """
    if isinstance(ip, ipaddress.IPv4Address):
        return (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_multicast
            or ip.is_unspecified
            or str(ip) == "169.254.169.254"  # cloud metadata endpoint
        )
    # IPv6
    return (
        ip.is_loopback
        or ip.is_unspecified
        or ip.is_multicast
        or (ip.segments()[0] & 0xFE00) == 0xFC00  # unique local fc00::/7
        or (ip.segments()[0] & 0xFFC0) == 0xFE80  # link-local fe80::/10
    )


def assert_safe_url(url: str, resolver: Any = None, allow_private: bool = False) -> str:
    """Validate `url` and reject any host resolving to a forbidden IP.

    Returns the URL unchanged if it passes. Raises SsrfError otherwise.

    `allow_private` skips the forbidden-IP rejection — a test-only seam for
    loopback test servers; production callers must leave it False.
    """
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise SsrfError(f"only http/https URLs are allowed (got {parsed.scheme!r})")
    host = parsed.hostname
    if not host:
        raise SsrfError("URL has no host")
    # Literal IPs in the URL (e.g. http://127.0.0.1/) are checked directly.
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        if is_forbidden_ip(literal) and not allow_private:
            raise SsrfError(f"IP address {host} is in a forbidden range")
        return url
    ips = host_ips(host, resolver=resolver)
    if not ips and not allow_private:
        # Could not resolve — reject up front rather than letting the HTTP
        # client discover a forbidden address on its own.
        raise SsrfError(f"could not resolve host {host}")
    if not allow_private:
        for ip in ips:
            if is_forbidden_ip(ip):
                raise SsrfError(f"host {host} resolves to forbidden IP {ip}")
    return url
