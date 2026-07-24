"""Tests for tools/web-http/tool.py.

Success paths run against a local http.server on 127.0.0.1 via the internal
`allow_private` test seam (the SSRF guard correctly rejects loopback in
production; the @tool wrappers never expose the seam). SSRF rejection paths
need no network at all — literal forbidden IPs fail before DNS, and
hostname cases use an injected resolver returning canned IPs.
"""

from __future__ import annotations

import json
import sqlite3
import sys
import threading
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "_lib"))
from testdb import create_test_db  # noqa: E402
from testloader import cleanup_tool_module, load_tool_module  # noqa: E402

_MODULE_NAME = "web_http_tool_under_test"

PAGE_HTML = (
    "<html><head><title>t</title><style>body{color:red}</style></head>"
    "<body><p>The quick brown fox jumps over the lazy dog.</p>"
    "<script>var x = 'fox';</script></body></html>"
)


class _Handler(BaseHTTPRequestHandler):
    def _send(self, status: int, body: bytes, content_type: str = "text/html") -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        if self.path.startswith("/redirect-loop"):
            self.send_response(302)
            self.send_header("Location", "/redirect-loop")
            self.send_header("Content-Length", "0")
            self.end_headers()
        elif self.path.startswith("/redirect"):
            self.send_response(302)
            self.send_header("Location", "/page")
            self.send_header("Content-Length", "0")
            self.end_headers()
        elif self.path.startswith("/echo"):
            length = int(self.headers.get("Content-Length") or 0)
            payload = self.rfile.read(length) if length else b""
            self._send(
                200,
                json.dumps(
                    {"path": self.path, "auth": self.headers.get("Authorization"), "body": payload.decode()}
                ).encode(),
                "application/json",
            )
        elif self.path.startswith("/big"):
            self._send(200, b"A" * (512 * 1024), "text/plain")
        else:
            self._send(200, PAGE_HTML.encode())

    do_POST = do_GET  # noqa: N815

    def log_message(self, *args: object) -> None:  # silence test output
        pass


@pytest.fixture(scope="module")
def server():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    thread.join(timeout=5)


@pytest.fixture()
def mod(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    create_test_db(tmp_path)
    monkeypatch.setenv("AGENTGPT_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AGENTGPT_REPO_ROOT", str(tmp_path))
    m = load_tool_module(__file__, _MODULE_NAME)
    yield m
    cleanup_tool_module(_MODULE_NAME)


# ── http_request success paths (local server) ───────────────────────────────


def test_http_request_get(mod, server: str) -> None:
    result = mod._request("GET", f"{server}/page", allow_private=True)
    assert result["ok"] is True
    assert result["data"]["status"] == 200
    assert "quick brown fox" in result["data"]["body"]
    assert result["data"]["final_url"].endswith("/page")


def test_http_request_query_and_headers_redacted(mod, server: str) -> None:
    result = mod._request(
        "POST",
        f"{server}/echo",
        query={"a": "1", "b": "two"},
        headers={"Authorization": "Bearer secret-token", "X-Custom": "ok"},
        body="payload",
        allow_private=True,
    )
    assert result["ok"] is True
    echoed = json.loads(result["data"]["body"])
    assert echoed["auth"] == "Bearer secret-token"  # reached the server
    assert echoed["body"] == "payload"
    assert "a=1" in echoed["path"] and "b=two" in echoed["path"]
    # ...but the echoed request data redacts the credential.
    assert result["data"]["request"]["headers"]["Authorization"] == "***"
    assert result["data"]["request"]["headers"]["X-Custom"] == "ok"


def test_http_request_follows_redirects_and_records_chain(mod, server: str) -> None:
    result = mod._request("GET", f"{server}/redirect", allow_private=True)
    assert result["ok"] is True
    assert result["data"]["status"] == 200
    assert len(result["data"]["redirect_chain"]) == 1
    assert result["data"]["redirect_chain"][0]["status"] == 302
    no_follow = mod._request("GET", f"{server}/redirect", follow_redirects=False, allow_private=True)
    assert no_follow["data"]["status"] == 302


def test_http_request_redirect_loop_capped(mod, server: str) -> None:
    result = mod._request("GET", f"{server}/redirect-loop", allow_private=True)
    assert result["ok"] is False
    assert result["error"]["code"] == "too_many_redirects"


def test_http_request_size_cap(mod, server: str) -> None:
    result = mod._request(
        "GET", f"{server}/big", maximum_response_bytes=64 * 1024, allow_private=True
    )
    assert result["ok"] is True
    assert result["data"]["truncated"] is True
    assert len(result["data"]["body"]) <= 64 * 1024 + 16384


# ── validation / SSRF (no network needed) ───────────────────────────────────


def test_rejects_invalid_method(mod) -> None:
    result = mod.http_request("TRACE", "http://example.com/")
    assert result["ok"] is False
    assert result["error"]["code"] == "invalid_method"


def test_rejects_credentials_in_url(mod) -> None:
    result = mod.http_request("GET", "http://user:pass@example.com/")
    assert result["ok"] is False
    assert result["error"]["code"] == "credentials_in_url"


@pytest.mark.parametrize(
    "url",
    [
        "http://169.254.169.254/latest/meta-data/",
        "http://10.0.0.1/",
        "http://192.168.1.1/",
        "http://127.0.0.1:8080/",
    ],
)
def test_ssrf_rejects_literal_forbidden_ips(mod, url: str) -> None:
    result = mod.http_request("GET", url)
    assert result["ok"] is False
    assert result["error"]["code"] == "ssrf_blocked"


def test_ssrf_rejects_metadata_hostname_via_resolver(mod) -> None:
    with pytest.raises(mod.SsrfError):
        mod._request(
            "GET",
            "http://metadata.google.internal/",
            resolver=lambda host: ["169.254.169.254"],
        )


def test_ssrf_rejects_redirect_to_private(mod, server: str) -> None:
    # The redirect hop is re-validated: /redirect-private would point at a
    # private IP; simulate by validating the join target directly.
    with pytest.raises(mod.SsrfError):
        mod._web_safety.assert_safe_url("http://169.254.169.254/")


def test_rejects_non_http_scheme(mod) -> None:
    result = mod.http_request("GET", "file:///etc/passwd")
    assert result["ok"] is False
    assert result["error"]["code"] == "ssrf_blocked"


# ── web_find ────────────────────────────────────────────────────────────────


def test_web_find_url(mod, server: str) -> None:
    result = mod._web_find(f"{server}/page", "brown fox", allow_private=True)
    assert result["ok"] is True
    assert result["data"]["kind"] == "url"
    assert result["data"]["match_count"] == 1
    assert "brown fox" in result["data"]["matches"][0]["context"]
    # Script content must be stripped (bs4 path), style too.
    assert "var x" not in str(result["data"]["matches"])


def test_web_find_regex_and_case(mod, server: str) -> None:
    result = mod._web_find(f"{server}/page", r"FOX", allow_private=True)
    assert result["ok"] is True
    assert result["data"]["match_count"] == 1  # case-insensitive default
    sensitive = mod._web_find(f"{server}/page", r"FOX", case_sensitive=True, allow_private=True)
    assert sensitive["data"]["match_count"] == 0


def test_web_find_invalid_regex_falls_back_to_literal(mod, server: str) -> None:
    result = mod._web_find(f"{server}/page", "dog.", allow_private=True)
    assert result["ok"] is True
    # "dog." as literal text exists; as regex it would also match, but the
    # fallback path is exercised by a pattern that is invalid regex:
    result2 = mod._web_find(f"{server}/page", "lazy dog (", allow_private=True)
    assert result2["ok"] is True
    assert result2["data"]["match_count"] == 0  # literal "(", no such text


def test_web_find_knowledge_source(mod, tmp_path: Path) -> None:
    conn = sqlite3.connect(str(tmp_path / "agentgpt.sqlite3"))
    now = datetime.now(UTC).isoformat()
    conn.execute(
        "INSERT INTO knowledge_sources (id, title, source_type, content, chunk_count,"
        " created_at, updated_at) VALUES (?, ?, 'paste', ?, 0, ?, ?)",
        ("ksrc-test", "Spec notes", "alpha beta gamma beta", now, now),
    )
    conn.commit()
    conn.close()
    result = mod.web_find("ksrc-test", "beta")
    assert result["ok"] is True
    assert result["data"]["kind"] == "knowledge"
    assert result["data"]["match_count"] == 2
    assert result["data"]["title"] == "Spec notes"


def test_web_find_knowledge_source_missing(mod) -> None:
    result = mod.web_find("ksrc-nope", "x")
    assert result["ok"] is False
    assert result["error"]["code"] == "not_found"


def test_web_find_ssrf_blocked(mod) -> None:
    result = mod.web_find("http://169.254.169.254/", "x")
    assert result["ok"] is False
    assert result["error"]["code"] == "ssrf_blocked"
