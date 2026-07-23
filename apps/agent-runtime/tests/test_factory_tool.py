"""Tests for the save_tool factory meta-tool (pure proposer)."""

from agentgpt_runtime.tools.factory import (
    FACTORY_SYSTEM_PROMPT,
    _save_tool_body,
)


def test_save_tool_returns_proposed_payload() -> None:
    tool_code = (
        "from strands import tool\n\n"
        "@tool\n"
        "def clock() -> str:\n"
        '    "Shows the current time"\n'
        "    ...\n\n"
        "TOOL = clock\n"
    )
    result = _save_tool_body(
        id="clock",
        name="Clock",
        description="Shows the current time",
        version="1.0.0",
        risk="read",
        requires_approval=False,
        network="none",
        timeout_seconds=10,
        trusted=False,
        tool_code=tool_code,
    )
    assert result["status"] == "proposed"
    manifest = result["manifest"]
    assert manifest["id"] == "clock"
    assert manifest["name"] == "Clock"
    assert manifest["trusted"] is False
    assert manifest["requires_approval"] is False
    assert manifest["risk"] == "read"
    assert result["tool_code"] == tool_code
    assert "TOOL = clock" in result["tool_code"]


def test_factory_prompt_instructs_single_call() -> None:
    assert "EXACTLY ONCE" in FACTORY_SYSTEM_PROMPT
    assert "save_tool" in FACTORY_SYSTEM_PROMPT
    assert "TOOL = " in FACTORY_SYSTEM_PROMPT


def test_save_tool_clamps_negative_timeout() -> None:
    """A negative timeout from the model must not reach the Rust u32 field."""
    result = _save_tool_body(
        id="clock",
        name="Clock",
        description="x",
        version="1.0.0",
        risk="read",
        requires_approval=False,
        network="none",
        timeout_seconds=-5,
        trusted=False,
        tool_code="TOOL = None\n",
    )
    assert result["manifest"]["timeout_seconds"] == 0
