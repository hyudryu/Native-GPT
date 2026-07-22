"""Tests for tools/web-fetch/tool.py.

Uses httpx's MockTransport so no real network calls are made. SSRF guard is
exercised via an injected resolver that returns canned IPs.
"""

from __future__ import annotations

import ipaddress
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest

# Local helper (sibling of this test under tools/_lib/). Add to sys.path.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "_lib"))
from testloader import cleanup_tool_module, load_tool_module  # noqa: E402

_TOOL_DIR = Path(__file__).resolve().parent
_MODULE_NAME = "web_fetch_tool_under_test"


@pytest.fixture()
def mod():
    m = load_tool_module(__file__, _MODULE_NAME)
    yield m
    cleanup_tool_module(_MODULE_NAME)


def _make_client(handler: Any) -> httpx.Client:
    """httpx.Client backed by a MockTransport for offline tests.

    Includes the same User-Agent the real tool sets, so tests that assert on
    the outbound header don't need to set it themselves.
    """
    return httpx.Client(
        transport=httpx.MockTransport(handler),
        headers={"User-Agent": "AgentGPT/1.0 (web-fetch tool)"},
    )


def test_markdown_extract_strips_script_and_keeps_text(mod) -> None:
    html = (
        "<!doctype html><html><head><title>Example Page</title>"
        "<script>alert('xss')</script></head>"
        "<body><h1>Hello</h1><p>This is <a href='/x'>a link</a>.</p></body></html>"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["user-agent"].startswith("AgentGPT")
        return httpx.Response(200, text=html, headers={"content-type": "text/html"})

    result = mod.fetch(
        "https://example.com/page",
        "markdown",
        client=_make_client(handler),
        resolver=lambda host: ["93.184.216.34"],
    )
    assert result["ok"] is True
    assert result["data"]["status"] == 200
    assert result["data"]["title"] == "Example Page"
    content = result["data"]["content"]
    assert "alert" not in content  # script stripped
    assert "Hello" in content
    assert "a link" in content
    assert "https://example.com/x" in content or "/x" in content


def test_text_extract_returns_plain_text(mod) -> None:
    html = "<html><body><h1>Title</h1><p>Para.</p></body></html>"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=html, headers={"content-type": "text/html"})

    result = mod.fetch(
        "https://example.com/",
        "text",
        client=_make_client(handler),
        resolver=lambda host: ["93.184.216.34"],
    )
    assert "Title" in result["data"]["content"]
    assert "Para" in result["data"]["content"]
    # No markdown artefacts in text mode.
    assert "#" not in result["data"]["content"]


def test_raw_extract_returns_original_html(mod) -> None:
    html = "<html><body><h1>Raw</h1></body></html>"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=html, headers={"content-type": "text/html"})

    result = mod.fetch(
        "https://example.com/",
        "raw",
        client=_make_client(handler),
        resolver=lambda host: ["93.184.216.34"],
    )
    assert "<html>" in result["data"]["content"]


def test_non_html_content_returned_as_text(mod) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text='{"json": true}', headers={"content-type": "application/json"})

    result = mod.fetch(
        "https://api.example.com/data",
        "markdown",
        client=_make_client(handler),
        resolver=lambda host: ["93.184.216.34"],
    )
    assert result["data"]["content"] == '{"json": true}'


def test_http_error_status_returned_as_error_result(mod) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="Not Found")

    result = mod.fetch(
        "https://example.com/missing",
        "markdown",
        client=_make_client(handler),
        resolver=lambda host: ["93.184.216.34"],
    )
    assert result["ok"] is False
    assert result["error"]["code"] == "http_error"
    assert result["data"]["status"] == 404


def test_truncation_when_body_exceeds_max_bytes(mod, monkeypatch: pytest.MonkeyPatch) -> None:
    # Lower the cap for the test.
    monkeypatch.setattr(mod, "MAX_BYTES", 1024)
    big = "A" * 5000

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=big, headers={"content-type": "text/plain"})

    result = mod.fetch(
        "https://example.com/big",
        "raw",
        client=_make_client(handler),
        resolver=lambda host: ["93.184.216.34"],
    )
    assert result["ok"] is True
    assert result["data"]["truncated"] is True
    assert "[truncated" in result["data"]["content"]


def test_ssrf_rejects_loopback_literal_ip(mod) -> None:
    with pytest.raises(mod.SsrfError):
        mod.assert_safe_url("http://127.0.0.1/admin")


def test_ssrf_rejects_link_local_literal_ip(mod) -> None:
    with pytest.raises(mod.SsrfError):
        mod.assert_safe_url("http://169.254.169.254/latest/meta-data/")


def test_ssrf_rejects_private_literal_ip(mod) -> None:
    with pytest.raises(mod.SsrfError):
        mod.assert_safe_url("http://10.0.0.1/")


def test_ssrf_rejects_loopback_hostname_via_resolver(mod) -> None:
    # The hostname looks public but resolves to a loopback IP.
    with pytest.raises(mod.SsrfError):
        mod.assert_safe_url("https://internal.local/", resolver=lambda host: ["127.0.0.1"])


def test_ssrf_rejects_unresolvable_host(mod) -> None:
    with pytest.raises(mod.SsrfError):
        mod.assert_safe_url("https://nonexistent.invalid/", resolver=lambda host: [])


def test_ssrf_rejects_non_http_scheme(mod) -> None:
    with pytest.raises(mod.SsrfError):
        mod.assert_safe_url("file:///etc/passwd")
    with pytest.raises(mod.SsrfError):
        mod.assert_safe_url("ftp://example.com/file")


def test_ssrf_allows_public_hostname(mod) -> None:
    assert mod.assert_safe_url("https://example.com/", resolver=lambda host: ["93.184.216.34"]) == "https://example.com/"


def test_ssrf_allows_public_literal_ip(mod) -> None:
    assert mod.assert_safe_url("https://93.184.216.34/") == "https://93.184.216.34/"


def test_is_forbidden_ip_matches_knowledge_rs_rules(mod) -> None:
    assert mod.is_forbidden_ip(ipaddress.ip_address("127.0.0.1"))
    assert mod.is_forbidden_ip(ipaddress.ip_address("10.0.0.1"))
    assert mod.is_forbidden_ip(ipaddress.ip_address("192.168.1.1"))
    assert mod.is_forbidden_ip(ipaddress.ip_address("169.254.169.254"))
    assert mod.is_forbidden_ip(ipaddress.ip_address("::1"))
    assert not mod.is_forbidden_ip(ipaddress.ip_address("93.184.216.34"))
    assert not mod.is_forbidden_ip(ipaddress.ip_address("1.1.1.1"))


def test_fetch_raises_ssrf_for_loopback(mod) -> None:
    """fetch() lets SsrfError propagate; the @tool wrapper converts it."""
    with pytest.raises(mod.SsrfError):
        mod.fetch(
            "http://127.0.0.1/admin",
            "markdown",
            client=_make_client(lambda req: httpx.Response(200, text="")),
            resolver=lambda host: ["127.0.0.1"],
        )


def test_tool_wrapper_returns_dict_on_ssrf(mod) -> None:
    """The @tool-decorated `web_fetch` converts SsrfError to a result dict."""
    result = mod.web_fetch("http://127.0.0.1/admin")  # type: ignore[misc]
    assert result["ok"] is False
    assert result["error"]["code"] == "ssrf_blocked"
    assert "127.0.0.1" in result["error"]["message"]
