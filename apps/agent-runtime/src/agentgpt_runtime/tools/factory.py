"""Tool Manager meta-tool: a pure proposer the agent calls to emit a tool.

Registered ONLY when a run carries ``factory_mode=True``. It performs no side
effects — it returns the proposed manifest + code for the host/UI to surface
for human review. The UI's Save button does the actual file write via REST.
"""

from __future__ import annotations

from typing import Any

from strands import tool

FACTORY_SYSTEM_PROMPT = """\
You are the Tool Manager. Given the user's request, produce ONE new or revised
Strands tool by calling the save_tool function EXACTLY ONCE.

Rules for tool_code:
- It is a complete, self-contained Python 3.12+ module.
- Start with `from strands import tool`.
- Define exactly one function decorated with `@tool`. Its docstring becomes the
  Strands tool description shown to agents — write it clearly.
- End with `TOOL = <function_name>`.
- You may import the Python standard library. To share helpers, import from
  `tools/_lib` using the project's importlib pattern (see existing tools).
- Return a plain string (or JSON-serializable value) from the function.

Think briefly (1-3 sentences) about what the tool should do, then call save_tool
with every field filled in. Do not write files; save_tool returns the proposal
for a human to review.
"""


def _save_tool_body(
    id: str,
    name: str,
    description: str,
    version: str,
    risk: str,
    requires_approval: bool,
    network: str,
    timeout_seconds: int,
    trusted: bool,
    tool_code: str,
) -> dict[str, Any]:
    """Pure implementation — returns the proposal dict.

    Kept separate from the ``@tool``-decorated wrapper so tests can call it
    directly without Strands' tool-call dispatch machinery.
    """
    # Clamp to a non-negative int so the Rust backend's u32 deserialization
    # never rejects the proposal on a negative value from the model.
    timeout_seconds = max(0, int(timeout_seconds))
    return {
        "status": "proposed",
        "manifest": {
            "id": id,
            "name": name,
            "description": description,
            "version": version,
            "risk": risk,
            "requires_approval": requires_approval,
            "network": network,
            "timeout_seconds": timeout_seconds,
            "trusted": trusted,
        },
        "tool_code": tool_code,
    }


@tool
def save_tool(
    id: str,
    name: str,
    description: str,
    version: str,
    risk: str,
    requires_approval: bool,
    network: str,
    timeout_seconds: int,
    trusted: bool,
    tool_code: str,
) -> dict[str, Any]:
    """Propose a tool for the Tool Manager. Call EXACTLY ONCE per request.

    Returns the proposal for human review; nothing is written to disk.
    tool_code must be a complete module that exports TOOL.
    """
    return _save_tool_body(
        id=id,
        name=name,
        description=description,
        version=version,
        risk=risk,
        requires_approval=requires_approval,
        network=network,
        timeout_seconds=timeout_seconds,
        trusted=trusted,
        tool_code=tool_code,
    )
