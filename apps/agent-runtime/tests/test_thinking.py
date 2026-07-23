"""Thinking off/high profiles, retry ladder, unsupported cache (spec §§1-2)."""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

from agentgpt_runtime.chat import ChatRuns
from agentgpt_runtime.protocol import RunStartPayload
from agentgpt_runtime.thinking import (
    THINKING_HIGH_PROFILES,
    THINKING_OFF_FALLBACK,
    THINKING_OFF_PROFILES,
    ThinkingParamsCache,
    is_thinking_param_error,
    match_profile_family,
    next_thinking_attempt,
    resolve_thinking_params,
    thinking_attempt_ladder,
)


def _payload(mode: str, base_url: str, model_id: str, **model_kw: Any) -> RunStartPayload:
    return RunStartPayload.model_validate(
        {
            "run_id": "run-1",
            "conversation_id": "conv-1",
            "message_id": "msg-1",
            "prompt": "hi",
            "thinking_mode": mode,
            "model": {"base_url": base_url, "model_id": model_id, **model_kw},
        }
    )


# --- profile matching ---


def test_match_profile_family_by_host() -> None:
    assert match_profile_family("https://api.openai.com", "anything") == "openai"
    assert match_profile_family("https://api.anthropic.com", "anything") == "anthropic"
    assert match_profile_family("https://dashscope.aliyuncs.com/v1", "m") == "qwen"
    assert match_profile_family("https://api.deepseek.com", "m") == "deepseek"


def test_match_profile_family_by_model_prefix() -> None:
    assert match_profile_family("http://proxy.local", "gpt-5-pro") == "openai"
    assert match_profile_family("http://proxy.local", "o3-mini") == "openai"
    assert match_profile_family("http://proxy.local", "claude-opus-4") == "anthropic"
    assert match_profile_family("http://proxy.local", "qwen3-32b") == "qwen"
    assert match_profile_family("http://proxy.local", "deepseek-r1") == "deepseek"


def test_match_profile_family_no_match() -> None:
    assert match_profile_family("http://127.0.0.1:11434", "llama3.1") is None


# --- resolution order ---


def test_endpoint_override_wins(tmp_path: Any) -> None:
    cache = ThinkingParamsCache(tmp_path / "cache.json")
    override = {"reasoning_effort": "low"}
    payload = _payload(
        "off", "https://api.openai.com", "gpt-5", thinking_off_params=override
    )
    assert resolve_thinking_params(payload, "off", cache) == override


def test_unsupported_cache_sends_nothing(tmp_path: Any) -> None:
    cache = ThinkingParamsCache(tmp_path / "cache.json")
    cache.mark_unsupported("https://api.openai.com", "gpt-5", reason="400")
    payload = _payload("off", "https://api.openai.com", "gpt-5")
    assert resolve_thinking_params(payload, "off", cache) is None


def test_known_profile_used(tmp_path: Any) -> None:
    cache = ThinkingParamsCache(tmp_path / "cache.json")
    off = resolve_thinking_params(
        _payload("off", "https://api.openai.com", "gpt-5"), "off", cache
    )
    assert off == THINKING_OFF_PROFILES["openai"]
    high = resolve_thinking_params(
        _payload("high", "https://api.openai.com", "gpt-5"), "high", cache
    )
    assert high == THINKING_HIGH_PROFILES["openai"]


def test_unmatched_model_gets_no_params(tmp_path: Any) -> None:
    cache = ThinkingParamsCache(tmp_path / "cache.json")
    assert (
        resolve_thinking_params(_payload("off", "http://local.test", "llama3"), "off", cache)
        is None
    )
    assert (
        resolve_thinking_params(_payload("high", "http://local.test", "llama3"), "high", cache)
        is None
    )


# --- retry ladder ---


def test_ladder_off_profile_then_minimal_then_plain(tmp_path: Any) -> None:
    cache = ThinkingParamsCache(tmp_path / "cache.json")
    ladder = thinking_attempt_ladder(_payload("off", "https://api.openai.com", "gpt-5"), cache)
    assert ladder == [
        THINKING_OFF_PROFILES["openai"],
        THINKING_OFF_FALLBACK,
        None,
    ]


def test_ladder_high_profile_then_plain(tmp_path: Any) -> None:
    cache = ThinkingParamsCache(tmp_path / "cache.json")
    ladder = thinking_attempt_ladder(_payload("high", "https://api.openai.com", "gpt-5"), cache)
    assert ladder == [THINKING_HIGH_PROFILES["openai"], None]


def test_ladder_unmatched_is_plain_only(tmp_path: Any) -> None:
    cache = ThinkingParamsCache(tmp_path / "cache.json")
    assert thinking_attempt_ladder(_payload("off", "http://local.test", "llama3"), cache) == [None]
    assert thinking_attempt_ladder(_payload("max", "https://api.openai.com", "gpt-5"), cache) == [
        None
    ]


def test_ladder_cached_unsupported_is_plain_only(tmp_path: Any) -> None:
    cache = ThinkingParamsCache(tmp_path / "cache.json")
    cache.mark_unsupported("https://api.openai.com", "gpt-5", reason="400")
    ladder = thinking_attempt_ladder(_payload("high", "https://api.openai.com", "gpt-5"), cache)
    assert ladder == [None]


# --- 400 param-error detection ---


class _FakeBadRequest(Exception):
    def __init__(self, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.status_code = status_code


def test_is_thinking_param_error_matches_offending_key() -> None:
    exc = _FakeBadRequest("Unrecognized request argument supplied: reasoning_effort")
    assert is_thinking_param_error(exc, {"reasoning_effort": "none"}) is True
    assert is_thinking_param_error(exc, {"reasoning_effort": "none"}) is True
    # A 400 about something else is not a thinking-param error.
    other = _FakeBadRequest("messages: field required")
    assert is_thinking_param_error(other, {"reasoning_effort": "none"}) is False


def test_is_thinking_param_error_nested_keys_and_chain() -> None:
    inner = _FakeBadRequest("unknown field enable_thinking")
    outer = RuntimeError("model request failed")
    outer.__cause__ = inner
    assert is_thinking_param_error(outer, {"extra_body": {"enable_thinking": False}}) is True


def test_is_thinking_param_error_ignores_non_4xx_and_none() -> None:
    assert (
        is_thinking_param_error(
            _FakeBadRequest("reasoning_effort bad", status_code=500),
            {"reasoning_effort": "none"},
        )
        is False
    )
    assert is_thinking_param_error(_FakeBadRequest("reasoning_effort"), None) is False


def test_next_thinking_attempt_advances_and_caches(tmp_path: Any) -> None:
    cache = ThinkingParamsCache(tmp_path / "cache.json")
    payload = _payload("off", "https://api.openai.com", "gpt-5")
    ladder = thinking_attempt_ladder(payload, cache)
    exc = _FakeBadRequest("Unrecognized request argument supplied: reasoning_effort")

    nxt = next_thinking_attempt(exc, ladder, 0, payload, cache)
    assert nxt == 1  # -> {"reasoning_effort": "minimal"}
    assert not cache.is_unsupported("https://api.openai.com", "gpt-5")

    nxt = next_thinking_attempt(exc, ladder, 1, payload, cache)
    assert nxt == 2  # -> plain request; endpoint is now cached unsupported
    assert cache.is_unsupported("https://api.openai.com", "gpt-5")

    # At the plain-request rung there is nowhere to fall back to.
    assert next_thinking_attempt(exc, ladder, 2, payload, cache) is None


def test_next_thinking_attempt_ignores_other_errors(tmp_path: Any) -> None:
    cache = ThinkingParamsCache(tmp_path / "cache.json")
    payload = _payload("high", "https://api.openai.com", "gpt-5")
    ladder = thinking_attempt_ladder(payload, cache)
    assert next_thinking_attempt(RuntimeError("boom"), ladder, 0, payload, cache) is None


def test_cache_persists_across_instances(tmp_path: Any) -> None:
    path = tmp_path / "thinking-params-cache.json"
    ThinkingParamsCache(path).mark_unsupported("http://h", "m", reason="r")
    reloaded = ThinkingParamsCache(path)
    assert reloaded.is_unsupported("http://h/", "m")  # trailing slash normalized
    data = json.loads(path.read_text(encoding="utf-8"))
    assert len(data["unsupported"]) == 1


# --- learn-on-400 end to end against a mock OpenAI server ---

_CHUNKS = ["Learned", " the", " ladder"]


class _ThinkingSSEHandler(BaseHTTPRequestHandler):
    """400s whenever the request carries reasoning_effort; SSE otherwise."""

    def do_POST(self) -> None:
        assert self.path == "/v1/chat/completions", self.path
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length) or b"{}")
        srv = self.server
        srv.requests.append(body)
        if "reasoning_effort" in body:
            payload = json.dumps(
                {
                    "error": {
                        "message": "Unrecognized request argument supplied: "
                        "reasoning_effort",
                        "type": "invalid_request_error",
                    }
                }
            ).encode()
            self.send_response(400)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        sse = b""
        for chunk in _CHUNKS:
            delta = json.dumps({"choices": [{"delta": {"content": chunk}}]}).encode()
            sse += b"data: " + delta + b"\n\n"
        sse += b"data: [DONE]\n\n"
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(sse)))
        self.end_headers()
        self.wfile.write(sse)

    def log_message(self, *args: Any) -> None:
        pass


@pytest.fixture()
def thinking_server() -> Any:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _ThinkingSSEHandler)
    server.requests = []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=15)


def _collect_run(runs: ChatRuns, events: list[Any]) -> list[dict[str, Any]]:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        types = [e.type for e in events]
        if "run.completed" in types or "run.failed" in types:
            return [e.model_dump() for e in events]
        time.sleep(0.05)
    raise AssertionError(f"run did not terminate: {[e.type for e in events]}")


@pytest.mark.usefixtures("_unload_strands_thinking")
def test_off_mode_learns_unsupported_and_completes_plain(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch, thinking_server: Any
) -> None:
    """off: profile 400 -> minimal 400 -> plain 200, cache records it."""
    import agentgpt_runtime.chat as chat_mod

    cache = ThinkingParamsCache(tmp_path / "cache.json")
    monkeypatch.setattr(chat_mod, "process_cache", lambda: cache)

    events: list[Any] = []
    runs = ChatRuns(events.append)
    base_url = f"http://127.0.0.1:{thinking_server.server_address[1]}"
    payload = _payload("off", base_url, "gpt-5-test")
    ack = runs.start(payload, "req-think-1")
    assert ack.type == "run.started"

    dumped = _collect_run(runs, events)
    types = [e["type"] for e in dumped]
    assert types[-1] == "run.completed", dumped[-1]

    # Three attempts: reasoning_effort "none" -> "minimal" -> plain.
    assert len(thinking_server.requests) == 3
    assert thinking_server.requests[0]["reasoning_effort"] == "none"
    assert thinking_server.requests[1]["reasoning_effort"] == "minimal"
    assert "reasoning_effort" not in thinking_server.requests[2]

    deltas = [e["payload"]["text"] for e in dumped if e["type"] == "run.text_delta"]
    assert "".join(deltas) == "".join(_CHUNKS)

    # The endpoint+model is cached as unsupported, and the notice was emitted.
    assert cache.is_unsupported(base_url, "gpt-5-test")
    notices = [
        e["payload"]["message"] for e in dumped if e["type"] == "run.activity"
    ]
    assert any("does not support disabling thinking" in m for m in notices)


@pytest.fixture()
def _unload_strands_thinking() -> Any:
    """Expunge strands/openai modules so this test imports them cleanly.

    Other tests monkeypatch httpx.AsyncClient/openai.AsyncOpenAI, and
    openai._base_client subclasses httpx.AsyncClient at import time; a prior
    import under that monkeypatch poisons the hierarchy process-wide.
    """
    import sys

    for name in [m for m in list(sys.modules) if m.split(".")[0] in {"strands", "openai"}]:
        del sys.modules[name]
    yield
    for name in [m for m in list(sys.modules) if m.split(".")[0] in {"strands", "openai"}]:
        del sys.modules[name]
