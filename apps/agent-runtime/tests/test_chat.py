from __future__ import annotations

from types import SimpleNamespace

from agentgpt_runtime.chat import ChatRuns, openai_base_url, strands_messages


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
