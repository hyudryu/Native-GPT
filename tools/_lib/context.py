"""Current run context for scoped tools (run_id / conversation_id).

Loaded by file path from `tools/<id>/tool.py` (the runtime imports each
tool.py as a standalone module, so package imports across tool folders are
unavailable). Resolution order:

1. `agentgpt_runtime.run_context` context vars — set by chat.py when the
   tool runs inside the agent runtime.
2. `AGENTGPT_RUN_ID` / `AGENTGPT_CONVERSATION_ID` env vars — fallback for
   sidecar/CLI invocations.
3. Empty dict — standalone use (tests); tools then require explicit ids as
   arguments or operate unscoped.
"""

from __future__ import annotations

import os
from typing import Any


def get_run_context() -> dict[str, Any]:
    """Return {"run_id": ..., "conversation_id": ...} for the active run.

    Missing pieces are simply omitted; an empty dict means no run context
    is available (e.g. unit tests calling the tool function directly).
    """
    try:
        from agentgpt_runtime import run_context  # type: ignore

        context = run_context.get_run_context()
        if context:
            return dict(context)
    except Exception:  # noqa: BLE001 - runtime package may be absent
        pass

    context: dict[str, Any] = {}
    run_id = os.environ.get("AGENTGPT_RUN_ID", "").strip()
    conversation_id = os.environ.get("AGENTGPT_CONVERSATION_ID", "").strip()
    if run_id:
        context["run_id"] = run_id
    if conversation_id:
        context["conversation_id"] = conversation_id
    return context
