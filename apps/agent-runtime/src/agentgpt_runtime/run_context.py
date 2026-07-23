"""Per-run context for scoped tools.

Carries the active `run_id` / `conversation_id` through the async execution
path via `contextvars`, so Strands tools loaded by file path (which receive
no call-site context) can attribute the rows they write — plans, goal
contracts, memories, citations — to the run and conversation that produced
them.

Set once per run in `chat.py` before the Strands agent is invoked. Context
vars are task-local, so concurrent runs never see each other's ids.
"""

from __future__ import annotations

import contextvars
from typing import Any

_run_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "agentgpt_run_id", default=None
)
_conversation_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "agentgpt_conversation_id", default=None
)


def set_run_context(
    run_id: str | None = None, conversation_id: str | None = None
) -> None:
    """Record the run/conversation being executed on this async task."""
    _run_id.set(run_id)
    _conversation_id.set(conversation_id)


def get_run_context() -> dict[str, Any]:
    """Return the current run context; empty dict outside a run."""
    context: dict[str, Any] = {}
    run_id = _run_id.get()
    conversation_id = _conversation_id.get()
    if run_id:
        context["run_id"] = run_id
    if conversation_id:
        context["conversation_id"] = conversation_id
    return context


def clear_run_context() -> None:
    """Reset both context vars (mainly for tests)."""
    _run_id.set(None)
    _conversation_id.set(None)
