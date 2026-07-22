from __future__ import annotations

from types import SimpleNamespace

import pytest

from agentgpt_runtime.chat import (
    ChatRuns,
    activity_from_event,
    build_openai_model,
    openai_base_url,
    strands_messages,
    usage_from_result,
)
from agentgpt_runtime.protocol import RunStartPayload


def test_openai_base_url_accepts_root_or_v1() -> None:
    assert openai_base_url("http://127.0.0.1:11434") == "http://127.0.0.1:11434/v1"
    assert openai_base_url("http://127.0.0.1:11434/v1/") == "http://127.0.0.1:11434/v1"


def test_history_is_translated_without_unknown_roles() -> None:
    history = [
        SimpleNamespace(role="user", content="hello"),
        SimpleNamespace(role="assistant", content="hi"),
        SimpleNamespace(role="system", content="ignored"),
    ]
    assert strands_messages(history) == [
        {"role": "user", "content": [{"text": "hello"}]},
        {"role": "assistant", "content": [{"text": "hi"}]},
    ]


def test_cancel_unknown_run_returns_protocol_error() -> None:
    runs = ChatRuns(lambda _event: None)
    response = runs.cancel("missing", "request-1")
    assert response.type == "error"
    assert response.payload["code"] == "run_not_found"


def test_usage_is_normalized_from_strands_metrics() -> None:
    result = SimpleNamespace(
        metrics=SimpleNamespace(
            accumulated_usage={"inputTokens": 12, "outputTokens": 8, "totalTokens": 20},
            accumulated_metrics={"latencyMs": 2000},
        )
    )
    assert usage_from_result(result) == {
        "input_tokens": 12,
        "output_tokens": 8,
        "total_tokens": 20,
        "latency_ms": 2000.0,
        "tokens_per_second": 4.0,
    }


def test_tool_use_event_becomes_a_concise_activity_update() -> None:
    assert activity_from_event({"current_tool_use": {"name": "github_search"}}) == {
        "message": "Using github_search",
        "source": "github_search",
    }
    assert activity_from_event({"data": "answer text"}) is None


# --- build_openai_model: tls_verify ---


@pytest.fixture
def _unload_strands() -> None:
    """build_openai_model lazily imports strands; keep it out of sys.modules so
    test_protocol's "not imported at startup" assertion still holds."""
    yield
    import sys

    for name in [m for m in sys.modules if m == "strands" or m.startswith("strands.")]:
        del sys.modules[name]


def _run_payload(**kw: object) -> RunStartPayload:
    base: dict = {
        "run_id": "run-1",
        "conversation_id": "conv-1",
        "message_id": "msg-1",
        "prompt": "hello",
        "model": {"base_url": "https://selfsigned.local", "model_id": "model-1"},
    }
    return RunStartPayload.model_validate({**base, **kw})


@pytest.mark.usefixtures("_unload_strands")
def test_build_openai_model_tls_verify_false_injects_unverified_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, dict] = {}

    class FakeAsyncClient:
        def __init__(self, **kwargs: object) -> None:
            captured["httpx"] = kwargs

    class FakeAsyncOpenAI:
        def __init__(self, **kwargs: object) -> None:
            captured["openai"] = kwargs

    monkeypatch.setattr("httpx.AsyncClient", FakeAsyncClient)
    monkeypatch.setattr("openai.AsyncOpenAI", FakeAsyncOpenAI)

    model = build_openai_model(_run_payload(tls_verify=False))

    # An unverified httpx client backs a pre-configured OpenAI client, which
    # Strands reuses across requests instead of closing (see build_openai_model).
    assert captured["httpx"]["verify"] is False
    assert captured["openai"]["http_client"] is not None
    assert captured["openai"]["base_url"] == "https://selfsigned.local/v1"
    assert model._custom_client is not None


@pytest.mark.usefixtures("_unload_strands")
def test_build_openai_model_tls_verify_absent_keeps_secure_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_async_client(**kwargs: object) -> None:
        raise AssertionError("no custom http_client may be built when tls_verify is absent")

    monkeypatch.setattr("httpx.AsyncClient", fail_async_client)

    model = build_openai_model(_run_payload())

    assert model._custom_client is None
    assert "http_client" not in model.client_args
    assert model.client_args["base_url"] == "https://selfsigned.local/v1"
