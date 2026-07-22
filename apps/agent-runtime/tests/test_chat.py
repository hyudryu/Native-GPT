from __future__ import annotations

from types import SimpleNamespace

from agentgpt_runtime.chat import (
    ChatRuns,
    activity_from_event,
    openai_base_url,
    strands_messages,
    usage_from_result,
)


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
