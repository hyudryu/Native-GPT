"""Unit tests for Strands → wire-event translation in chat.py.

Pure-function tests (no Strands runtime, no network). The end-to-end stream
shape is covered by test_chat_stream.py.
"""

from __future__ import annotations

import json
from typing import Any

from agentgpt_runtime.chat import (
    activity_from_event,
    tool_call_from_event,
    tool_result_from_event,
)


def test_activity_from_event_extracts_tool_name() -> None:
    event = {"current_tool_use": {"toolUseId": "t1", "name": "current_time"}}
    assert activity_from_event(event) == {"message": "Using current_time", "source": "current_time"}


def test_activity_from_event_ignores_non_tool_events() -> None:
    assert activity_from_event({"data": "hello"}) is None
    assert activity_from_event(None) is None


def test_tool_call_from_event_returns_empty_for_non_tool_events() -> None:
    assert tool_call_from_event({"data": "text"}) == []
    assert tool_call_from_event(None) == []


def test_tool_call_from_event_extracts_call_id_name_and_dict_input() -> None:
    event = {
        "type": "tool_use_stream",
        "current_tool_use": {
            "toolUseId": "call-1",
            "name": "calculate",
            "input": {"expression": "2 + 2"},
        },
    }
    calls = tool_call_from_event(event)
    assert len(calls) == 1
    assert calls[0].call_id == "call-1"
    assert calls[0].tool == "calculate"
    assert calls[0].input == {"expression": "2 + 2"}


def test_tool_call_from_event_parses_json_string_input_fragment() -> None:
    """While streaming, Strands sends input as a JSON-string fragment."""
    event = {
        "current_tool_use": {
            "toolUseId": "call-1",
            "name": "calculate",
            "input": '{"expression": "3 * 4"}',
        },
    }
    calls = tool_call_from_event(event)
    assert calls[0].input == {"expression": "3 * 4"}


def test_tool_call_from_event_handles_partial_json_input_fragment() -> None:
    """A partial fragment (still streaming) should not raise."""
    event = {
        "current_tool_use": {
            "toolUseId": "call-1",
            "name": "read_file",
            "input": '{"path": "/etc/pas',
        },
    }
    calls = tool_call_from_event(event)
    # Falls back to a _raw bucket instead of raising.
    assert calls[0].input == {"_raw": '{"path": "/etc/pas'}


def test_tool_call_from_event_handles_empty_input() -> None:
    event = {
        "current_tool_use": {
            "toolUseId": "call-1",
            "name": "list_files",
            "input": "",
        },
    }
    calls = tool_call_from_event(event)
    assert calls[0].input == {}


def test_tool_call_from_event_rejects_missing_call_id() -> None:
    event = {"current_tool_use": {"name": "calculate"}}
    assert tool_call_from_event(event) == []


def test_tool_result_from_event_extracts_success_result() -> None:
    event = {
        "message": {
            "role": "user",
            "content": [
                {
                    "toolResult": {
                        "toolUseId": "call-1",
                        "status": "success",
                        "content": [{"text": "4"}],
                    }
                }
            ],
        }
    }
    results = tool_result_from_event(event)
    assert len(results) == 1
    r = results[0]
    assert r.call_id == "call-1"
    assert r.ok is True
    assert r.summary == "4"
    assert r.error is None
    assert r.data == {"content": [{"text": "4"}]}


def test_tool_result_from_event_extracts_error_result() -> None:
    event = {
        "message": {
            "role": "user",
            "content": [
                {
                    "toolResult": {
                        "toolUseId": "call-2",
                        "status": "error",
                        "content": [{"text": "Error: division by zero"}],
                    }
                }
            ],
        }
    }
    results = tool_result_from_event(event)
    assert len(results) == 1
    r = results[0]
    assert r.ok is False
    assert r.error == {"code": "tool_error", "message": "Error: division by zero"}
    assert r.data == {}


def test_tool_result_from_event_handles_multiple_results_in_one_message() -> None:
    event = {
        "message": {
            "role": "user",
            "content": [
                {
                    "toolResult": {
                        "toolUseId": "call-a",
                        "status": "success",
                        "content": [{"text": "a"}],
                    }
                },
                {
                    "toolResult": {
                        "toolUseId": "call-b",
                        "status": "success",
                        "content": [{"text": "b"}],
                    }
                },
            ],
        }
    }
    results = tool_result_from_event(event)
    assert [r.call_id for r in results] == ["call-a", "call-b"]


def test_tool_result_from_event_truncates_long_summary() -> None:
    long_text = "x" * 500
    event = {
        "message": {
            "content": [
                {
                    "toolResult": {
                        "toolUseId": "call-x",
                        "status": "success",
                        "content": [{"text": long_text}],
                    }
                }
            ],
        }
    }
    results = tool_result_from_event(event)
    assert len(results[0].summary) == 200
    assert results[0].summary.endswith("…")


def test_tool_result_from_event_includes_structured_content_for_mcp() -> None:
    structured = {"items": [1, 2, 3]}
    event = {
        "message": {
            "content": [
                {
                    "toolResult": {
                        "toolUseId": "call-mcp",
                        "status": "success",
                        "content": [{"text": "ok"}],
                        "structuredContent": structured,
                    }
                }
            ],
        }
    }
    results = tool_result_from_event(event)
    assert results[0].data["structured"] == structured


def test_tool_result_from_event_ignores_non_tool_messages() -> None:
    event = {"message": {"role": "assistant", "content": [{"text": "hi"}]}}
    assert tool_result_from_event(event) == []
    assert tool_result_from_event(None) == []
    assert tool_result_from_event({"data": "text"}) == []


def test_summary_falls_back_for_json_blocks() -> None:
    event = {
        "message": {
            "content": [
                {
                    "toolResult": {
                        "toolUseId": "call-j",
                        "status": "success",
                        "content": [{"json": {"rows": 42}}],
                    }
                }
            ],
        }
    }
    results = tool_result_from_event(event)
    summary: str = results[0].summary
    # The serialized form is a valid JSON object containing rows=42.
    parsed = json.loads(summary)
    assert parsed == {"rows": 42}


def test_no_crash_on_malformed_event() -> None:
    weird: Any = {"message": {"content": "not a list"}}
    assert tool_result_from_event(weird) == []
    weird2: Any = {"current_tool_use": "not a dict"}
    assert tool_call_from_event(weird2) == []
