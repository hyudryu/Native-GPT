"""Web search Strands tool backed by DuckDuckGo (`ddgs` package, no API key).

Returns a standard tool result with a list of `{title, url, snippet}` entries.
Falls back to an error result if the search backend is unreachable or the
library is broken — never hard-fails the agent runtime.
"""

from __future__ import annotations

from typing import Any

from strands import tool

MAX_RESULTS_CAP = 20  # hard ceiling on `max_results` to bound payload size
DEFAULT_MAX_RESULTS = 5


class SearchError(RuntimeError):
    """Raised when the search backend fails."""


def normalize_result(raw: dict[str, Any]) -> dict[str, str]:
    """Normalize a ddgs TextResult dict to our wire shape.

    ddgs uses keys `title`, `href`, `body`. We rename to `title`, `url`, `snippet`.
    """
    return {
        "title": str(raw.get("title", "")).strip(),
        "url": str(raw.get("href", raw.get("url", ""))).strip(),
        "snippet": str(raw.get("body", raw.get("snippet", raw.get("description", "")))).strip(),
    }


def search(query: str, max_results: int = DEFAULT_MAX_RESULTS, *, client: Any = None) -> dict[str, Any]:
    """Perform a DuckDuckGo text search and return a standard-schema result dict.

    `client` is injectable for tests; if provided, must expose a `.text(query,
    max_results=N)` method returning a list of dicts (mirroring `ddgs.DDGS`).
    """
    if not isinstance(query, str) or not query.strip():
        raise SearchError("query must be a non-empty string")
    try:
        max_results_int = int(max_results)
    except (TypeError, ValueError) as exc:
        raise SearchError(f"max_results must be an integer (got {max_results!r})") from exc
    if max_results_int < 1:
        raise SearchError("max_results must be >= 1")
    max_results_int = min(max_results_int, MAX_RESULTS_CAP)

    owns_client = client is None
    if client is None:
        # Imported lazily so the runtime doesn't pay the import cost unless
        # the agent actually invokes this tool.
        from ddgs import DDGS  # noqa: PLC0415

        client = DDGS()
    try:
        raw_results = client.text(query, max_results=max_results_int)
    finally:
        # ddgs.DDGS doesn't expose a close() in 9.x, but its underlying HTTP
        # transport cleans up via GC. If a custom client with close() was
        # injected, call it.
        if owns_client:
            close = getattr(client, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:  # noqa: BLE001, S110
                    pass

    if not isinstance(raw_results, list):
        raise SearchError(f"unexpected search response shape: {type(raw_results).__name__}")

    results = [normalize_result(item) for item in raw_results if isinstance(item, dict)]
    summary = f"{len(results)} result{'s' if len(results) != 1 else ''} for {query!r}"
    return {
        "ok": True,
        "summary": summary,
        "data": {"query": query, "results": results},
        "error": None,
    }


@tool
def web_search(query: str, max_results: int = DEFAULT_MAX_RESULTS) -> dict[str, Any]:
    """Search the web and return titles, URLs, and snippets.

    Uses DuckDuckGo so no API key is required. Use this when you need current
    information or to discover sources before fetching them with web_fetch.

    Args:
        query: A natural-language or keyword search query.
        max_results: Maximum number of results to return (1-20, default 5).

    Returns:
        `{ok, summary, data: {query, results: [{title, url, snippet}]}, error}`.
    """

    try:
        return search(query, max_results)
    except SearchError as exc:
        return {
            "ok": False,
            "summary": "Search rejected",
            "data": {"query": query},
            "error": {"code": "search_error", "message": str(exc)},
        }
    except Exception as exc:  # noqa: BLE001 — network/backend failure must not kill the run
        return {
            "ok": False,
            "summary": "Search backend unavailable",
            "data": {"query": query},
            "error": {"code": "search_backend_error", "message": str(exc)},
        }


TOOL = web_search
