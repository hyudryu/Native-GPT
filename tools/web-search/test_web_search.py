"""Tests for tools/web-search/tool.py.

Uses an injected fake client so no network access is needed.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "_lib"))
from testloader import cleanup_tool_module, load_tool_module  # noqa: E402

_MODULE_NAME = "web_search_tool_under_test"


@pytest.fixture()
def mod():
    m = load_tool_module(__file__, _MODULE_NAME)
    yield m
    cleanup_tool_module(_MODULE_NAME)


class _FakeClient:
    def __init__(self, results: list[dict[str, Any]]) -> None:
        self._results = results
        self.calls: list[tuple[str, int]] = []
        self.closed = False

    def text(self, query: str, max_results: int = 5) -> list[dict[str, Any]]:
        self.calls.append((query, max_results))
        return self._results[:max_results]

    def close(self) -> None:
        self.closed = True


class _RaisingClient:
    def text(self, query: str, max_results: int = 5) -> list[dict[str, Any]]:
        raise ConnectionError("network down")


def test_normalize_renames_keys(mod) -> None:
    raw = {"title": "T", "href": "https://x", "body": "snippet text"}
    assert mod.normalize_result(raw) == {"title": "T", "url": "https://x", "snippet": "snippet text"}


def test_normalize_falls_back_for_url_key(mod) -> None:
    raw = {"title": "T", "url": "https://y", "snippet": "alt"}
    assert mod.normalize_result(raw) == {"title": "T", "url": "https://y", "snippet": "alt"}


def test_search_returns_normalized_results(mod) -> None:
    fake = _FakeClient(
        [
            {"title": "Result One", "href": "https://one.example", "body": "First snippet"},
            {"title": "Result Two", "href": "https://two.example", "body": "Second snippet"},
        ]
    )
    result = mod.search("hello world", max_results=5, client=fake)
    assert result["ok"] is True
    assert result["summary"] == "2 results for 'hello world'"
    assert len(result["data"]["results"]) == 2
    assert result["data"]["results"][0] == {
        "title": "Result One",
        "url": "https://one.example",
        "snippet": "First snippet",
    }
    assert fake.calls == [("hello world", 5)]


def test_search_caps_max_results(mod) -> None:
    fake = _FakeClient([{"title": f"R{i}", "href": f"https://{i}", "body": ""} for i in range(50)])
    result = mod.search("x", max_results=9999, client=fake)
    assert len(result["data"]["results"]) <= mod.MAX_RESULTS_CAP
    assert fake.calls[0][1] == mod.MAX_RESULTS_CAP


def test_search_filters_non_dict_entries(mod) -> None:
    fake = _FakeClient([{"title": "ok", "href": "https://ok", "body": ""}, "garbage", None, 42])  # type: ignore[list-item]
    result = mod.search("x", client=fake)
    assert len(result["data"]["results"]) == 1


def test_search_empty_results_returned_as_success(mod) -> None:
    fake = _FakeClient([])
    result = mod.search("obscure query", client=fake)
    assert result["ok"] is True
    assert result["data"]["results"] == []
    assert "0 results" in result["summary"]


def test_search_rejects_empty_query(mod) -> None:
    with pytest.raises(mod.SearchError):
        mod.search("", client=_FakeClient([]))
    with pytest.raises(mod.SearchError):
        mod.search("   ", client=_FakeClient([]))


def test_search_rejects_invalid_max_results(mod) -> None:
    with pytest.raises(mod.SearchError):
        mod.search("x", max_results=0, client=_FakeClient([]))
    with pytest.raises(mod.SearchError):
        mod.search("x", max_results="not-a-number", client=_FakeClient([]))


def test_search_rejects_unexpected_response_shape(mod) -> None:
    class _BadClient:
        def text(self, query: str, max_results: int = 5) -> dict[str, Any]:
            return {"not": "a list"}

    with pytest.raises(mod.SearchError):
        mod.search("x", client=_BadClient())


def test_search_propagates_backend_errors(mod) -> None:
    with pytest.raises(ConnectionError):
        mod.search("x", client=_RaisingClient())


def test_tool_wrapper_converts_backend_error_to_dict(mod) -> None:
    # Empty query surfaces a SearchError converted to a result dict.
    result = mod.web_search("")  # type: ignore[misc]
    assert result["ok"] is False
    assert result["error"]["code"] == "search_error"


def test_closes_injected_client(mod) -> None:
    fake = _FakeClient([])
    mod.search("x", client=fake)
    assert fake.closed is False  # we only close clients we created


def test_summary_pluralization(mod) -> None:
    fake = _FakeClient([{"title": "only", "href": "u", "body": "b"}])
    result = mod.search("x", client=fake)
    assert result["summary"] == "1 result for 'x'"
