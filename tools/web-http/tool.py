"""Web & HTTP Strands tools — SSRF-guarded search and general HTTP requests.

Multi-tool folder: `TOOL` is a list of Strands tools. The SSRF guard is
shared with `tools/web-fetch` via `tools/_lib/web_safety.py` (loaded by file
path): http/https only; hosts that are or resolve to private, loopback,
link-local, multicast, or unspecified IPs — including 169.254.169.254 — are
rejected before any socket is opened, and EVERY redirect hop is re-validated
(manual redirect following, never the client's implicit follow).

`web_find` accepts either an http(s) URL (fetched, HTML stripped to text) or
a knowledge source id (loaded from `knowledge_sources` / `knowledge_chunks`
via `tools/_lib/db.py`).

Planned-but-not-in-this-stage: auth profiles (named credential bundles).
http_request sends only the headers the caller passes; Authorization/Cookie
values are redacted from echoed request data.

TEST-ONLY SEAM: the internal `_request` / `_fetch_text` helpers take an
`allow_private` kwarg (default False) so tests can hit a loopback
http.server. The @tool wrappers never expose it.
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urljoin, urlparse, urlunparse

import httpx
from strands import tool

# Load shared `_lib` helpers by file path (no package context when the
# runtime imports this file standalone).
_LIB_DIR = Path(__file__).resolve().parent.parent / "_lib"


def _load_lib(filename: str, module_name: str) -> Any:
    spec = importlib.util.spec_from_file_location(module_name, _LIB_DIR / filename)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_web_safety = _load_lib("web_safety.py", "agentgpt_tools_web_safety")
_db = _load_lib("db.py", "agentgpt_tools_db")

SsrfError = _web_safety.SsrfError

USER_AGENT = "AgentGPT/1.0 (web-http tool)"
DEFAULT_TIMEOUT_SECONDS = 30
MAX_TIMEOUT_SECONDS = 120
DEFAULT_MAX_RESPONSE_BYTES = 1024 * 1024  # 1 MB
HARD_MAX_RESPONSE_BYTES = 10 * 1024 * 1024  # absolute ceiling
MAX_REDIRECTS = 5
FETCH_MAX_BYTES = 2 * 1024 * 1024  # web_find page cap (matches web-fetch)

ALLOWED_METHODS = {"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"}
REDIRECT_STATUSES = {301, 302, 303, 307, 308}
SENSITIVE_REQUEST_HEADERS = {"authorization", "proxy-authorization", "cookie"}
FILTERED_RESPONSE_HEADERS = {"set-cookie"}

_URL_RE = re.compile(r"^https?://", re.IGNORECASE)


class HttpToolError(ValueError):
    """Any web-http failure; `code` becomes the result's error code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _result(summary: str, data: dict[str, Any]) -> dict[str, Any]:
    return {"ok": True, "summary": summary, "data": data, "error": None}


def _failure(
    code: str, summary: str, message: str, data: dict[str, Any] | None = None
) -> dict[str, Any]:
    return {
        "ok": False,
        "summary": summary,
        "data": data or {},
        "error": {"code": code, "message": message},
    }


def _reject_credentials_in_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.username is not None or parsed.password is not None:
        raise HttpToolError(
            "credentials_in_url",
            "URLs with embedded credentials (user:pass@host) are refused; "
            "pass auth via the headers argument instead",
        )


def _merge_query(url: str, query: dict[str, Any] | None) -> str:
    if not query:
        return url
    if not isinstance(query, dict):
        raise HttpToolError("invalid_query", "query must be an object of key/value pairs")
    parsed = urlparse(url)
    pairs = []
    for key, value in query.items():
        if isinstance(value, (list, tuple)):
            pairs.extend((key, str(v)) for v in value)
        else:
            pairs.append((key, str(value)))
    extra = urlencode(pairs)
    combined = f"{parsed.query}&{extra}" if parsed.query else extra
    return urlunparse(parsed._replace(query=combined))


def _redact_request_headers(headers: dict[str, str]) -> dict[str, str]:
    return {
        k: ("***" if k.lower() in SENSITIVE_REQUEST_HEADERS else v) for k, v in headers.items()
    }


def _filter_response_headers(headers: httpx.Headers) -> dict[str, str]:
    return {k: v for k, v in headers.items() if k.lower() not in FILTERED_RESPONSE_HEADERS}


def _read_capped(response: httpx.Response, cap: int) -> tuple[bytes, bool]:
    """Read a streamed body, stopping at `cap` bytes."""
    chunks: list[bytes] = []
    size = 0
    truncated = False
    for chunk in response.iter_bytes(chunk_size=16384):
        size += len(chunk)
        chunks.append(chunk)
        if size >= cap:
            truncated = True
            break
    return b"".join(chunks), truncated


def _decode_body(body: bytes, content_type: str) -> str:
    match = re.search(r"charset=([\w\-]+)", content_type or "", re.IGNORECASE)
    encodings = [match.group(1)] if match else []
    encodings += ["utf-8", "latin-1"]
    for encoding in encodings:
        try:
            return body.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return body.decode("utf-8", errors="replace")


def _request(
    method: str,
    url: str,
    *,
    query: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    body: str | None = None,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    follow_redirects: bool = True,
    maximum_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
    client: httpx.Client | None = None,
    resolver: Any = None,
    allow_private: bool = False,
) -> dict[str, Any]:
    """Execute the HTTP request with manual, re-validated redirect following.

    `client`, `resolver`, and `allow_private` are injectable/test-only seams;
    the @tool wrapper never exposes them.
    """
    verb = str(method).strip().upper()
    if verb not in ALLOWED_METHODS:
        raise HttpToolError(
            "invalid_method",
            f"method must be one of {sorted(ALLOWED_METHODS)} (got {method!r})",
        )
    if not isinstance(url, str) or not url.strip():
        raise HttpToolError("invalid_url", "url must be a non-empty string")
    target = _merge_query(url.strip(), query)
    _reject_credentials_in_url(target)

    try:
        timeout = int(timeout_seconds)
    except (TypeError, ValueError):
        raise HttpToolError("invalid_timeout", "timeout_seconds must be an integer") from None
    if timeout < 1 or timeout > MAX_TIMEOUT_SECONDS:
        raise HttpToolError("invalid_timeout", f"timeout_seconds must be 1..{MAX_TIMEOUT_SECONDS}")
    try:
        cap = int(maximum_response_bytes)
    except (TypeError, ValueError):
        raise HttpToolError("invalid_cap", "maximum_response_bytes must be an integer") from None
    cap = max(1024, min(cap, HARD_MAX_RESPONSE_BYTES))

    request_headers = {"User-Agent": USER_AGENT}
    if headers:
        if not isinstance(headers, dict):
            raise HttpToolError("invalid_headers", "headers must be an object of string pairs")
        request_headers.update({str(k): str(v) for k, v in headers.items()})

    owns_client = client is None
    if client is None:
        client = httpx.Client(timeout=float(timeout), follow_redirects=False)

    redirect_chain: list[dict[str, Any]] = []
    current_url = target
    current_method = verb
    current_body = body
    try:
        for _ in range(MAX_REDIRECTS + 1):
            _web_safety.assert_safe_url(current_url, resolver=resolver, allow_private=allow_private)
            request_kwargs: dict[str, Any] = {"headers": request_headers}
            if current_body is not None and current_method not in {"GET", "HEAD"}:
                request_kwargs["content"] = current_body.encode("utf-8")
            with client.stream(current_method, current_url, **request_kwargs) as response:
                status = response.status_code
                if (
                    follow_redirects
                    and status in REDIRECT_STATUSES
                    and response.headers.get("location")
                ):
                    location = urljoin(current_url, response.headers["location"])
                    _reject_credentials_in_url(location)
                    redirect_chain.append({"from": current_url, "to": location, "status": status})
                    if status == 303 or (status in {301, 302} and current_method == "POST"):
                        current_method = "GET"
                        current_body = None
                    current_url = location
                    continue
                body_bytes, truncated = _read_capped(response, cap)
                content_type = response.headers.get("content-type", "")
                text = _decode_body(body_bytes, content_type)
                return _result(
                    f"{verb} {urlparse(current_url).netloc or current_url} -> {status}",
                    {
                        "status": status,
                        "headers": _filter_response_headers(response.headers),
                        "body": text,
                        "bytes": len(body_bytes),
                        "truncated": truncated,
                        "final_url": current_url,
                        "redirect_chain": redirect_chain,
                        "request": {
                            "method": verb,
                            "url": target,
                            "headers": _redact_request_headers(request_headers),
                        },
                    },
                )
        return _failure(
            "too_many_redirects",
            "redirect limit exceeded",
            f"exceeded {MAX_REDIRECTS} redirects",
            {"redirect_chain": redirect_chain, "final_url": current_url},
        )
    finally:
        if owns_client:
            client.close()


def _fetch_text(
    url: str,
    *,
    client: httpx.Client | None = None,
    resolver: Any = None,
    allow_private: bool = False,
) -> str:
    """Fetch a URL and return readable text (HTML tags stripped via bs4)."""
    owns_client = client is None
    if client is None:
        client = httpx.Client(timeout=15.0, follow_redirects=False)
    try:
        current_url = url
        for _ in range(MAX_REDIRECTS + 1):
            _web_safety.assert_safe_url(current_url, resolver=resolver, allow_private=allow_private)
            with client.stream(
                "GET", current_url, headers={"User-Agent": USER_AGENT}
            ) as response:
                if (
                    response.status_code in REDIRECT_STATUSES
                    and response.headers.get("location")
                ):
                    current_url = urljoin(current_url, response.headers["location"])
                    _reject_credentials_in_url(current_url)
                    continue
                if response.status_code >= 400:
                    raise HttpToolError("http_error", f"HTTP {response.status_code} for {url}")
                body, _ = _read_capped(response, FETCH_MAX_BYTES)
                content_type = response.headers.get("content-type", "")
                text = _decode_body(body, content_type)
                break
        else:
            raise HttpToolError("too_many_redirects", f"exceeded {MAX_REDIRECTS} redirects")
    finally:
        if owns_client:
            client.close()

    if "html" in content_type.lower() or text.lstrip().lower().startswith(("<!doctype", "<html")):
        try:
            from bs4 import BeautifulSoup

            soup = BeautifulSoup(text, "lxml")
            for tag in soup(["script", "style", "noscript"]):
                tag.decompose()
            return soup.get_text(separator="\n", strip=True)
        except ImportError:
            pass  # fall through to raw text
    return text


def _compile_pattern(pattern: str, case_sensitive: bool) -> re.Pattern[str]:
    if not isinstance(pattern, str) or not pattern:
        raise HttpToolError("invalid_pattern", "pattern must be a non-empty string")
    flags = 0 if case_sensitive else re.IGNORECASE
    try:
        return re.compile(pattern, flags)
    except re.error:
        # Not valid regex — treat as a literal string search.
        return re.compile(re.escape(pattern), flags)


def _search_text(
    text: str, compiled: re.Pattern[str], context_chars: int, maximum_matches: int
) -> list[dict[str, Any]]:
    matches = []
    for match in compiled.finditer(text):
        start = max(0, match.start() - context_chars)
        end = min(len(text), match.end() + context_chars)
        matches.append(
            {
                "index": match.start(),
                "match": match.group(0),
                "context": text[start:end],
            }
        )
        if len(matches) >= maximum_matches:
            break
    return matches


def _load_source_text(source_id: str) -> tuple[str, str]:
    """Return (title, text) for a knowledge source from the app database."""
    try:
        conn = _db.connect()
    except FileNotFoundError as exc:
        raise HttpToolError("db_unavailable", str(exc)) from None
    try:
        row = conn.execute(
            "SELECT id, title, content FROM knowledge_sources WHERE id = ?", (source_id,)
        ).fetchone()
        if row is None:
            raise HttpToolError("not_found", f"knowledge source not found: {source_id}")
        text = row["content"] or ""
        if not text:
            chunks = conn.execute(
                "SELECT content FROM knowledge_chunks WHERE source_id = ? ORDER BY position",
                (source_id,),
            ).fetchall()
            text = "\n".join(chunk["content"] for chunk in chunks)
        return row["title"], text
    finally:
        conn.close()


def _web_find(
    url_or_source_id: str,
    pattern: str,
    case_sensitive: bool = False,
    context_chars: int = 120,
    maximum_matches: int = 20,
    *,
    client: httpx.Client | None = None,
    resolver: Any = None,
    allow_private: bool = False,
) -> dict[str, Any]:
    """Implementation of web_find. client/resolver/allow_private are
    injectable test-only seams; the @tool wrapper never exposes them."""
    if not isinstance(url_or_source_id, str) or not url_or_source_id.strip():
        raise HttpToolError("invalid_source", "url_or_source_id must be a non-empty string")
    try:
        context = max(0, min(500, int(context_chars)))
        maximum = max(1, min(100, int(maximum_matches)))
    except (TypeError, ValueError):
        raise HttpToolError("invalid_argument", "context_chars/maximum_matches must be integers") from None
    compiled = _compile_pattern(pattern, bool(case_sensitive))

    source = url_or_source_id.strip()
    if _URL_RE.match(source):
        kind = "url"
        text = _fetch_text(source, client=client, resolver=resolver, allow_private=allow_private)
        label: str | None = source
    else:
        kind = "knowledge"
        title, text = _load_source_text(source)
        label = title

    matches = _search_text(text, compiled, context, maximum)
    return _result(
        f"{len(matches)} match(es) in {label}",
        {
            "source": source,
            "kind": kind,
            "title": label,
            "matches": matches,
            "match_count": len(matches),
            "truncated": len(matches) >= maximum,
        },
    )


# ── tools ───────────────────────────────────────────────────────────────────


@tool
def web_find(
    url_or_source_id: str,
    pattern: str,
    case_sensitive: bool = False,
    context_chars: int = 120,
    maximum_matches: int = 20,
) -> dict[str, Any]:
    """Search a web page or knowledge source for a pattern, with context.

    If `url_or_source_id` starts with http:// or https:// it is fetched
    (SSRF-guarded; HTML stripped to text). Otherwise it is treated as a
    knowledge source id and searched in the app database.

    Args:
        url_or_source_id: An http(s) URL or a knowledge source id.
        pattern: Regex (preferred) — invalid regex falls back to a literal
            string search.
        case_sensitive: Default false (case-insensitive).
        context_chars: Characters of surrounding context per match (0-500).
        maximum_matches: Cap on returned matches (1-100, default 20).

    Returns:
        `{ok, summary, data: {source, kind: "url"|"knowledge", matches:
        [{index, match, context}], match_count, truncated}, error}`.
    """

    try:
        return _web_find(
            url_or_source_id, pattern, case_sensitive, context_chars, maximum_matches
        )
    except HttpToolError as exc:
        return _failure(exc.code, "web_find failed", str(exc), {"source": url_or_source_id})
    except SsrfError as exc:
        return _failure("ssrf_blocked", "URL rejected by SSRF guard", str(exc), {"source": url_or_source_id})
    except httpx.TimeoutException:
        return _failure("timeout", "request timed out", "fetch exceeded its timeout", {"source": url_or_source_id})
    except httpx.HTTPError as exc:
        return _failure("connection_error", "request failed", str(exc), {"source": url_or_source_id})


@tool
def http_request(
    method: str,
    url: str,
    query: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    body: str | None = None,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    follow_redirects: bool = True,
    maximum_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
) -> dict[str, Any]:
    """Make an outbound HTTP request with a strict SSRF guard.

    Every redirect hop is re-validated (private/loopback/metadata IPs are
    always rejected). URLs with embedded credentials are refused. Response
    bodies are capped (default 1 MB, max 10 MB). Auth profiles are a planned
    later stage — pass any auth headers explicitly; they are redacted in the
    echoed request data.

    Args:
        method: GET | HEAD | POST | PUT | PATCH | DELETE | OPTIONS.
        url: http(s) URL. `user:pass@host` URLs are rejected.
        query: Optional query parameters merged into the URL.
        headers: Optional request headers.
        body: Optional request body (text; sent for non-GET/HEAD).
        timeout_seconds: 1-120, default 30.
        follow_redirects: Follow 3xx redirects (each hop re-validated).
        maximum_response_bytes: Response cap (1 KB - 10 MB, default 1 MB).

    Returns:
        `{ok, summary, data: {status, headers, body, bytes, truncated,
        final_url, redirect_chain, request: {method, url, headers(redacted)}},
        error}`. HTTP 4xx/5xx still return ok=True (the request succeeded);
        check `status`.
    """

    try:
        return _request(
            method,
            url,
            query=query,
            headers=headers,
            body=body,
            timeout_seconds=timeout_seconds,
            follow_redirects=follow_redirects,
            maximum_response_bytes=maximum_response_bytes,
        )
    except HttpToolError as exc:
        return _failure(exc.code, "request rejected", str(exc), {"url": url})
    except SsrfError as exc:
        return _failure("ssrf_blocked", "URL rejected by SSRF guard", str(exc), {"url": url})
    except httpx.TimeoutException:
        return _failure("timeout", "request timed out", f"exceeded {timeout_seconds}s", {"url": url})
    except httpx.HTTPError as exc:
        return _failure("connection_error", "request failed", str(exc), {"url": url})


TOOL = [web_find, http_request]
