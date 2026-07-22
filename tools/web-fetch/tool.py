"""Web fetch Strands tool.

Fetches a URL via httpx with a strict SSRF guard (private/loopback/link-local
addresses are rejected before any request is made) and returns the body in
one of three extract modes:

- "markdown" (default): HTML -> readable markdown-ish text via bs4
- "text":           HTML -> plain text (visible text only, tags stripped)
- "raw":            Raw HTML body as-is

Capped at 2 MB to bound memory use; non-200 responses return an error result
rather than raising.
"""

from __future__ import annotations

import ipaddress
import socket
from typing import Any, Literal
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup
from strands import tool

USER_AGENT = "AgentGPT/1.0 (web-fetch tool)"
TIMEOUT_SECONDS = 15.0
MAX_BYTES = 2 * 1024 * 1024  # 2 MB cap on response bodies

ExtractMode = Literal["markdown", "text", "raw"]


class SsrfError(ValueError):
    """Raised when a URL host resolves to a forbidden IP."""


def _host_ips(host: str, resolver: Any = None) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    """Resolve `host` to IP addresses using `resolver(host)` (returns list of str IPs).

    `resolver` is injectable so tests can stub DNS. Default uses `socket.getaddrinfo`.
    """
    if resolver is None:
        resolver = _default_resolver
    infos = resolver(host)
    ips: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
    for raw in infos:
        try:
            ips.append(ipaddress.ip_address(raw))
        except ValueError:
            continue
    return ips


def _default_resolver(host: str) -> list[str]:
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return []
    # Each entry: (family, type, proto, canonname, sockaddr). sockaddr is
    # (ip, port) for IPv4 or (ip, port, flowinfo, scope_id) for IPv6.
    return [info[4][0] for info in infos]


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


def assert_safe_url(url: str, resolver: Any = None) -> str:
    """Validate `url` and reject any host resolving to a forbidden IP.

    Returns the URL unchanged if it passes. Raises SsrfError otherwise.
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
        if is_forbidden_ip(literal):
            raise SsrfError(f"IP address {host} is in a forbidden range")
        return url
    ips = _host_ips(host, resolver=resolver)
    if not ips:
        # Could not resolve — let httpx fail rather than silently allowing it.
        # If the host later resolves to something forbidden, httpx will be the
        # one making the request, which is what we're trying to avoid; so we
        # reject up front.
        raise SsrfError(f"could not resolve host {host}")
    for ip in ips:
        if is_forbidden_ip(ip):
            raise SsrfError(f"host {host} resolves to forbidden IP {ip}")
    return url


def _html_to_markdown(html: str, base_url: str) -> str:
    """Convert HTML to a readable markdown-ish string via bs4.

    Lightweight (not a full HTML→markdown converter): keeps headings as `#`,
    links as `[text](href)`, code in backticks, lists with bullets, and
    drops script/style/nav/aside/footer elements.
    """
    soup = BeautifulSoup(html, "lxml")
    # Strip obviously-non-content elements.
    for tag in soup(["script", "style", "noscript", "nav", "aside", "footer", "header"]):
        tag.decompose()

    lines: list[str] = []

    def emit(text: str) -> None:
        text = text.replace("\r", "").strip()
        if text:
            lines.append(text)

    for element in soup.descendants:
        name = getattr(element, "name", None)
        if name is None:
            # NavigableString — emit only if it's not whitespace-only inside a block.
            parent_name = getattr(element.parent, "name", None)
            text = str(element)
            if parent_name in {"script", "style", "noscript"}:
                continue
            if text.strip():
                emit(text)
            continue
        if name in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            level = int(name[1])
            emit(f"\n{'#' * level} {element.get_text(strip=True)}\n")
        elif name == "a":
            href = element.get("href")
            text = element.get_text(strip=True)
            if href and text:
                emit(f"[{text}]({href})")
        elif name in {"li"}:
            emit(f"- {element.get_text(strip=True)}")
        elif name == "pre":
            code = element.get_text()
            emit(f"\n```\n{code.strip()}\n```\n")
        elif name == "code":
            # Inline code (not inside <pre>, which we already handled above).
            if element.parent is not None and element.parent.name != "pre":
                emit(f"`{element.get_text()}`")
        elif name == "p":
            emit(element.get_text(strip=True))
            lines.append("")

    # Collapse 3+ blank lines to 2.
    cleaned: list[str] = []
    blanks = 0
    for line in lines:
        if line:
            cleaned.append(line)
            blanks = 0
        else:
            blanks += 1
            if blanks <= 1:
                cleaned.append("")
    return "\n".join(cleaned).strip()


def _html_to_text(html: str) -> str:
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    return soup.get_text(separator="\n", strip=True)


def fetch(
    url: str,
    extract_mode: ExtractMode = "markdown",
    *,
    client: httpx.Client | None = None,
    resolver: Any = None,
) -> dict[str, Any]:
    """Fetch `url` and return a standard-schema result dict.

    `client` and `resolver` are injectable for tests. Real callers go through
    the `@tool`-decorated `web_fetch` below.
    """
    assert_safe_url(url, resolver=resolver)
    owns_client = client is None
    if client is None:
        client = httpx.Client(
            timeout=TIMEOUT_SECONDS,
            follow_redirects=True,
            headers={"User-Agent": USER_AGENT},
        )
    try:
        # Stream so we can stop reading past MAX_BYTES.
        with client.stream("GET", url) as response:
            status = response.status_code
            chunks: list[bytes] = []
            size = 0
            truncated = False
            for chunk in response.iter_bytes(chunk_size=8192):
                size += len(chunk)
                chunks.append(chunk)
                if size >= MAX_BYTES:
                    truncated = True
                    break
            body = b"".join(chunks)
            content_type = response.headers.get("content-type", "")
    finally:
        if owns_client:
            client.close()

    if status >= 400:
        return {
            "ok": False,
            "summary": f"HTTP {status}",
            "data": {"url": url, "status": status, "truncated": truncated},
            "error": {"code": "http_error", "message": f"HTTP {status}"},
        }

    raw_text: str
    try:
        raw_text = body.decode("utf-8", errors="replace")
    except UnicodeDecodeError:
        raw_text = body.decode("latin-1", errors="replace")

    is_html = "html" in content_type.lower() or raw_text.lstrip().lower().startswith("<!doctype") or raw_text.lstrip().startswith("<html")
    title: str | None = None
    if is_html:
        try:
            title_soup = BeautifulSoup(raw_text, "lxml")
            if title_soup.title and title_soup.title.string:
                title = title_soup.title.string.strip()
        except Exception:  # noqa: BLE001
            pass

    if extract_mode == "raw":
        content = raw_text
    elif extract_mode == "text":
        content = _html_to_text(raw_text) if is_html else raw_text
    else:  # "markdown"
        content = _html_to_markdown(raw_text, url) if is_html else raw_text

    if truncated:
        content += "\n\n[truncated: response exceeded 2 MB]"

    return {
        "ok": True,
        "summary": (title or url)[:200],
        "data": {
            "url": url,
            "status": status,
            "content_type": content_type,
            "title": title,
            "content": content,
            "truncated": truncated,
        },
        "error": None,
    }


@tool
def web_fetch(url: str, extract_mode: ExtractMode = "markdown") -> dict[str, Any]:
    """Fetch a URL and return its content as markdown, plain text, or raw HTML.

    Use this to read web pages, documentation, or API responses. Blocks
    private/loopback IPs (SSRF guard) and caps responses at 2 MB.

    Args:
        url: An http:// or https:// URL.
        extract_mode: "markdown" (default, readable HTML→markdown), "text"
            (plain visible text), or "raw" (original HTML).

    Returns:
        A standard tool result: `{ok, summary, data: {url, status, content_type,
        title?, content, truncated}, error}`.
    """

    try:
        return fetch(url, extract_mode)
    except SsrfError as exc:
        return {
            "ok": False,
            "summary": "URL rejected by SSRF guard",
            "data": {"url": url},
            "error": {"code": "ssrf_blocked", "message": str(exc)},
        }
    except httpx.TimeoutException:
        return {
            "ok": False,
            "summary": "Request timed out",
            "data": {"url": url},
            "error": {"code": "timeout", "message": f"request exceeded {TIMEOUT_SECONDS:.0f}s"},
        }
    except httpx.HTTPError as exc:
        return {
            "ok": False,
            "summary": "Request failed",
            "data": {"url": url},
            "error": {"code": "connection_error", "message": str(exc)},
        }


TOOL = web_fetch
