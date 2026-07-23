"""Tests for tools/browser/tool.py.

The HTTP layer is stubbed with httpx.MockTransport injected via the `client`
parameter, so no real loopback server is needed. Request shape assertions
mirror the host's InternalCommand struct in crates/server/src/browser/mod.rs
(camelCase keys: tabId, filePaths).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest

# Local helper (sibling of this test under tools/_lib/). Add to sys.path.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "_lib"))
from testloader import cleanup_tool_module, load_tool_module  # noqa: E402

_MODULE_NAME = "browser_tool_under_test"
BASE_URL = "http://127.0.0.1:8787"


@pytest.fixture()
def mod(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("AGENTGPT_INTERNAL_URL", BASE_URL)
    monkeypatch.setenv("AGENTGPT_INTERNAL_CAPABILITY_TOKEN", "test-token")
    monkeypatch.delenv("AGENTGPT_SERVER_PORT", raising=False)
    monkeypatch.delenv("AGENTGPT_SERVER_TOKEN", raising=False)
    m = load_tool_module(__file__, _MODULE_NAME)
    yield m
    cleanup_tool_module(_MODULE_NAME)


def make_client(handler: Any) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler), base_url=BASE_URL)


def ok_response(data: dict[str, Any] | None = None, summary: str = "done") -> httpx.Response:
    return httpx.Response(200, json={"ok": True, "summary": summary, "data": data or {}, "error": None})


def err_response(code: str, message: str) -> httpx.Response:
    return httpx.Response(
        200, json={"ok": False, "summary": message, "data": None, "error": {"code": code, "message": message}}
    )


class Recorder:
    """MockTransport handler that records requests and replays canned responses."""

    def __init__(self, response: httpx.Response | None = None) -> None:
        self.requests: list[httpx.Request] = []
        self.response = response or ok_response()

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.response

    @property
    def last_json(self) -> dict[str, Any]:
        return json.loads(self.requests[-1].content.decode("utf-8"))


# ---- request shape per action ----


def test_open_posts_command_with_url(mod) -> None:
    rec = Recorder()
    result = mod.run_browser_command("open", url="https://example.com", client=make_client(rec))
    assert result["ok"] is True
    req = rec.requests[0]
    assert req.method == "POST"
    assert req.url.path == "/internal/browser/command"
    assert req.headers["Authorization"] == "Bearer test-token"
    assert rec.last_json == {"action": "open", "wait": True, "url": "https://example.com"}


def test_open_without_url_sends_no_url(mod) -> None:
    rec = Recorder()
    mod.run_browser_command("open", client=make_client(rec))
    assert rec.last_json == {"action": "open", "wait": True}


def test_navigate_sends_camelcase_tab_id(mod) -> None:
    rec = Recorder()
    mod.run_browser_command(
        "navigate", url="https://example.com/apply", tab_id="tab-1", client=make_client(rec)
    )
    assert rec.last_json == {
        "action": "navigate",
        "wait": True,
        "url": "https://example.com/apply",
        "tabId": "tab-1",
    }


def test_execute_task_sends_task_and_wait(mod) -> None:
    rec = Recorder()
    mod.run_browser_command(
        "execute_task",
        url="https://example.com/jobs/123",
        task="Fill the form, stop before submission.",
        wait=True,
        client=make_client(rec),
    )
    assert rec.last_json == {
        "action": "execute_task",
        "wait": True,
        "url": "https://example.com/jobs/123",
        "task": "Fill the form, stop before submission.",
    }


def test_screenshot_and_close_browser_post_command(mod) -> None:
    for action in ("screenshot", "close_browser"):
        rec = Recorder()
        mod.run_browser_command(action, client=make_client(rec))
        assert rec.requests[0].url.path == "/internal/browser/command"
        assert rec.last_json == {"action": action, "wait": True}


def test_upload_file_sends_file_paths(mod) -> None:
    rec = Recorder()
    mod.run_browser_command(
        "upload_file", file_paths=["C:/docs/resume.pdf"], client=make_client(rec)
    )
    assert rec.last_json == {
        "action": "upload_file",
        "wait": True,
        "filePaths": ["C:/docs/resume.pdf"],
    }


def test_close_tab_sends_tab_id(mod) -> None:
    rec = Recorder()
    mod.run_browser_command("close_tab", tab_id="tab-9", client=make_client(rec))
    assert rec.last_json == {"action": "close_tab", "wait": True, "tabId": "tab-9"}


def test_status_uses_get_status_endpoint(mod) -> None:
    rec = Recorder(ok_response({"running": True, "active_tab": None}, "Browser status."))
    result = mod.run_browser_command("status", client=make_client(rec))
    req = rec.requests[0]
    assert req.method == "GET"
    assert req.url.path == "/internal/browser/status"
    assert result["ok"] is True
    assert result["data"]["running"] is True


def test_stop_task_uses_post_stop_endpoint(mod) -> None:
    rec = Recorder(ok_response({"stopped": True}, "Task stopped."))
    result = mod.run_browser_command("stop_task", client=make_client(rec))
    req = rec.requests[0]
    assert req.method == "POST"
    assert req.url.path == "/internal/browser/stop"
    assert result["ok"] is True
    assert result["data"]["stopped"] is True


# ---- local arg validation (no HTTP call may happen) ----


def test_navigate_requires_url(mod) -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        raise AssertionError("HTTP layer must not be called on validation failure")

    result = mod.run_browser_command("navigate", client=make_client(boom))
    assert result["ok"] is False
    assert result["error"]["code"] == "invalid_arguments"


def test_execute_task_requires_task(mod) -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        raise AssertionError("HTTP layer must not be called on validation failure")

    result = mod.run_browser_command("execute_task", url="https://example.com", client=make_client(boom))
    assert result["ok"] is False
    assert result["error"]["code"] == "invalid_arguments"


def test_upload_file_requires_file_paths(mod) -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        raise AssertionError("HTTP layer must not be called on validation failure")

    for bad in (None, [], ["", "  "]):
        result = mod.run_browser_command("upload_file", file_paths=bad, client=make_client(boom))
        assert result["ok"] is False
        assert result["error"]["code"] == "invalid_arguments"


def test_close_tab_requires_tab_id(mod) -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        raise AssertionError("HTTP layer must not be called on validation failure")

    result = mod.run_browser_command("close_tab", client=make_client(boom))
    assert result["ok"] is False
    assert result["error"]["code"] == "invalid_arguments"


@pytest.mark.parametrize("scheme", ["file", "chrome", "chrome-extension", "javascript", "data"])
def test_forbidden_url_schemes_rejected_locally(mod, scheme: str) -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        raise AssertionError("HTTP layer must not be called on validation failure")

    result = mod.run_browser_command("navigate", url=f"{scheme}:///etc/passwd", client=make_client(boom))
    assert result["ok"] is False
    assert result["error"]["code"] == "invalid_arguments"


# ---- host error-code passthrough ----


@pytest.mark.parametrize("code", ["BROWSER_NOT_INSTALLED", "TASK_BUSY", "TASK_CANCELLED", "PROFILE_LOCKED"])
def test_host_error_codes_pass_through(mod, code: str) -> None:
    rec = Recorder(err_response(code, "host says no"))
    result = mod.run_browser_command("open", client=make_client(rec))
    assert result["ok"] is False
    assert result["error"]["code"] == code
    assert result["error"]["message"] == "host says no"


def test_http_error_status_raises_bridge_error(mod, monkeypatch: pytest.MonkeyPatch) -> None:
    def raise_bridge(*args: Any, **kwargs: Any) -> Any:
        raise mod.BridgeClientError("BROWSER_START_FAILED", "boom", status=500)

    monkeypatch.setattr(mod._bridge, "internal_post", raise_bridge)
    # The decorated tool maps BridgeClientError into a result dict.
    result = mod.browser("open")
    assert result["ok"] is False
    assert result["error"]["code"] == "BROWSER_START_FAILED"


# ---- wait=false behavior ----


def test_wait_false_sends_wait_false_and_returns_accepted_state(mod) -> None:
    rec = Recorder(ok_response({"task_id": "t-1", "status": "accepted"}, "Task accepted."))
    result = mod.run_browser_command(
        "execute_task", task="do things", wait=False, client=make_client(rec)
    )
    assert rec.last_json["wait"] is False
    assert result["ok"] is True
    assert result["data"]["status"] == "accepted"


def test_wait_defaults_to_true(mod) -> None:
    rec = Recorder()
    mod.run_browser_command("execute_task", task="do things", client=make_client(rec))
    assert rec.last_json["wait"] is True


# ---- untrusted-content labeling (spec §11.3) ----


def test_success_result_labeled_untrusted_external(mod) -> None:
    rec = Recorder(ok_response({"final_url": "https://example.com", "result": "page text"}))
    result = mod.run_browser_command("execute_task", task="read the page", client=make_client(rec))
    assert result["ok"] is True
    assert result["data"]["content_trust"] == "untrusted_external"


def test_status_result_labeled_untrusted_external(mod) -> None:
    rec = Recorder(ok_response({"running": True, "active_tab": {"title": "Example"}}))
    result = mod.run_browser_command("status", client=make_client(rec))
    assert result["data"]["content_trust"] == "untrusted_external"


def test_error_result_not_labeled(mod) -> None:
    rec = Recorder(err_response("TASK_BUSY", "busy"))
    result = mod.run_browser_command("open", client=make_client(rec))
    assert result["ok"] is False
    assert result["data"] is None


def test_summary_capped_at_200_chars(mod) -> None:
    rec = Recorder(ok_response(summary="x" * 500))
    result = mod.run_browser_command("open", client=make_client(rec))
    assert len(result["summary"]) <= 200


# ---- transport failure handling ----


def test_logic_fn_propagates_transport_errors(mod) -> None:
    """The undecorated logic fn lets httpx transport errors propagate; the
    @tool wrapper maps them to stable error codes (tested below)."""
    def timeout_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow host", request=request)

    with pytest.raises(httpx.ReadTimeout):
        mod.run_browser_command("open", client=make_client(timeout_handler))


def test_decorated_tool_maps_timeout(mod, monkeypatch: pytest.MonkeyPatch) -> None:
    def timeout_post(*args: Any, **kwargs: Any) -> Any:
        raise httpx.ReadTimeout("slow host")

    monkeypatch.setattr(mod._bridge, "internal_post", timeout_post)
    result = mod.browser("open")
    assert result["ok"] is False
    assert result["error"]["code"] == "timeout"


def test_decorated_tool_maps_connection_error(mod, monkeypatch: pytest.MonkeyPatch) -> None:
    def down(*args: Any, **kwargs: Any) -> Any:
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(mod._bridge, "internal_post", down)
    result = mod.browser("open")
    assert result["ok"] is False
    assert result["error"]["code"] == "connection_error"


# ---- auth / base-url config ----


def test_no_token_sends_no_auth_header(mod, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AGENTGPT_INTERNAL_CAPABILITY_TOKEN")
    rec = Recorder()
    mod.run_browser_command("status", client=make_client(rec))
    assert "authorization" not in rec.requests[0].headers


def test_legacy_port_env_used_when_internal_url_absent(mod, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AGENTGPT_INTERNAL_URL")
    monkeypatch.setenv("AGENTGPT_SERVER_PORT", "9999")
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return ok_response()

    client = httpx.Client(transport=httpx.MockTransport(handler))
    mod.run_browser_command("status", client=client)
    assert seen[0].startswith("http://127.0.0.1:9999/")
