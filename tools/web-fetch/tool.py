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

import importlib.util
from pathlib import Path
from typing import Any, Literal

import httpx
from bs4 import BeautifulSoup
from strands import tool

# The SSRF guard is shared with tools/web-http via `tools/_lib/web_safety.py`.
# Loaded by file path (the runtime imports each tool.py as a standalone
# module, so package imports across folders are unavailable). Names are
# re-exported so existing callers/tests keep working unchanged.
_LIB_PATH = Path(__file__).resolve().parent.parent / "_lib" / "web_safety.py"
_spec = importlib.util.spec_from_file_location("agentgpt_tools_web_safety", _LIB_PATH)
assert _spec is not None and _spec.loader is not None
_web_safety = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_web_safety)

SsrfError = _web_safety.SsrfError
assert_safe_url = _web_safety.assert_safe_url
is_forbidden_ip = _web_safety.is_forbidden_ip
_host_ips = _web_safety.host_ips
_default_resolver = _web_safety.default_resolver

USER_AGENT = "AgentGPT/1.0 (web-fetch tool)"
TIMEOUT_SECONDS = 15.0
MAX_BYTES = 2 * 1024 * 1024  # 2 MB cap on response bodies

ExtractMode = Literal["markdown", "text", "raw"]


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
