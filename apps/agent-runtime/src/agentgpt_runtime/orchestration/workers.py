"""Investigator worker construction and parallel execution (spec §3.3).

Workers are isolated Strands Agents: each receives only the problem framing,
its assigned subproblem, relevant constraints, its tool subset, and the
output schema. They run as asyncio tasks capped by a semaphore; they never
see each other's results. Worker tools resolve through the existing tool
registry intersected with the run's enabled_tools, and workers inherit the
per-call HITL approval intervention.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from agentgpt_runtime.orchestration.prompts import REASK_PROMPT, WORKER_SYSTEM_PROMPT
from agentgpt_runtime.orchestration.schemas import (
    JsonExtractionError,
    ProblemFraming,
    Subproblem,
    WorkerResult,
    WorkerStatus,
    extract_json,
    parse_tolerant,
)

logger = logging.getLogger(__name__)

# Tool ids workers may ever receive. Any future orchestration tool is
# structurally excluded here (recursion prevention, spec §3.6).
_FORBIDDEN_WORKER_TOOLS = {"critical-thinking", "critical_thinking", "max-thinking"}

AgentFactory = Callable[..., Any]
OnWorkerStatus = Callable[[str, WorkerStatus, dict[str, Any] | None], None]


@dataclass
class WorkerOutcome:
    subproblem: Subproblem
    status: WorkerStatus
    result: WorkerResult | None = None
    error: str | None = None
    attempts: int = 0
    output_path: str | None = None
    tools_used: list[str] = field(default_factory=list)


def normalize_tool_id(name: str) -> str:
    """Subproblem recommended_tools use either web_search or web-search."""
    return name.strip().lower().replace("_", "-")


def resolve_worker_tool_ids(subproblem: Subproblem, enabled_tools: list[str]) -> list[str]:
    """recommended_tools ∩ enabled_tools, minus orchestration tools."""
    enabled = {normalize_tool_id(t) for t in enabled_tools}
    ids: list[str] = []
    for name in subproblem.recommended_tools:
        tool_id = normalize_tool_id(name)
        if tool_id in _FORBIDDEN_WORKER_TOOLS:
            continue
        if tool_id in enabled and tool_id not in ids:
            ids.append(tool_id)
    return ids


def result_text(result: Any) -> str:
    """Concatenate the text blocks of a Strands AgentResult message."""
    message = getattr(result, "message", None)
    if isinstance(message, dict):
        content = message.get("content")
        if isinstance(content, list):
            parts = [
                block["text"]
                for block in content
                if isinstance(block, dict) and isinstance(block.get("text"), str)
            ]
            if parts:
                return "".join(parts)
    return str(result)


def merge_usage(a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]:
    merged = dict(a)
    for key in ("input_tokens", "output_tokens", "total_tokens"):
        merged[key] = int(a.get(key, 0) or 0) + int(b.get(key, 0) or 0)
    return merged


def worker_prompt(framing: ProblemFraming, subproblem: Subproblem) -> str:
    """The worker's entire world: framing + its subproblem + the schema.

    Isolation (spec §3.3): no other worker results, no conversation history,
    no source corpus — workers fetch what they need via their tools.
    """
    return (
        "PROBLEM FRAMING (JSON):\n"
        + json.dumps(framing.model_dump(), indent=2)
        + "\n\nYOUR ASSIGNED SUBPROBLEM (JSON):\n"
        + json.dumps(subproblem.model_dump(), indent=2)
        + "\n\nInvestigate ONLY this subproblem and return the JSON result."
    )


def default_agent_factory(
    model: Any,
    system_prompt: str,
    tools: list[Any],
    interventions: list[Any],
) -> Any:
    from strands import Agent  # noqa: PLC0415

    return Agent(
        model=model,
        system_prompt=system_prompt,
        tools=tools,
        interventions=interventions,
        # See chat.py: the default callback handler prints to stdout, which
        # corrupts the NDJSON protocol channel.
        callback_handler=lambda **_: None,
    )


async def run_investigations(
    *,
    framing: ProblemFraming,
    subproblems: list[Subproblem],
    model: Any,
    tools_for: Callable[[Subproblem], tuple[list[Any], list[str]]],
    intervention_for: Callable[[str], list[Any]],
    usage_of: Callable[[Any], dict[str, Any]],
    on_status: OnWorkerStatus,
    cancel_event: threading.Event,
    synthesize_now_event: threading.Event,
    max_parallel_workers: int = 4,
    agent_factory: AgentFactory = default_agent_factory,
    is_cancelled: Callable[[BaseException], bool] | None = None,
) -> list[WorkerOutcome]:
    """Run one worker per subproblem as asyncio tasks under a semaphore.

    ``tools_for`` returns (tool objects, allowed-without-approval names) for
    a subproblem; ``intervention_for`` returns the HITL interventions for one
    worker (built per worker so parallel approvals don't share state);
    ``usage_of`` normalizes a Strands AgentResult's metrics.
    """
    from agentgpt_runtime.chat import tool_call_from_event  # noqa: PLC0415

    semaphore = asyncio.Semaphore(max_parallel_workers)

    async def attempt_worker(
        subproblem: Subproblem, prompt: str
    ) -> tuple[WorkerResult, dict[str, Any], list[str]]:
        """One worker attempt; raises on failure (the caller retries once)."""
        tools, allowed_names = tools_for(subproblem)
        interventions = intervention_for(subproblem.id)
        agent = agent_factory(model, WORKER_SYSTEM_PROMPT, tools, interventions)
        result: Any | None = None
        tools_used: list[str] = []
        seen_calls: set[str] = set()
        async for event in agent.stream_async(prompt):
            if cancel_event.is_set() or synthesize_now_event.is_set():
                agent.cancel()
                raise _WorkerStopped("stopped by user control")
            if not isinstance(event, dict):
                continue
            if event.get("result") is not None:
                result = event["result"]
            for call in tool_call_from_event(event):
                if call.call_id in seen_calls:
                    continue
                seen_calls.add(call.call_id)
                if call.tool not in tools_used:
                    tools_used.append(call.tool)
                on_status(
                    subproblem.id,
                    WorkerStatus.WAITING_FOR_TOOL,
                    {"tools_used": list(tools_used)},
                )
            if isinstance(event.get("data"), str) and event["data"]:
                on_status(subproblem.id, WorkerStatus.RUNNING, None)
        if result is None:
            raise RuntimeError("worker produced no result")
        usage = usage_of(result)
        text = result_text(result)
        try:
            data = extract_json(text)
        except JsonExtractionError:
            # One re-ask on parse failure (spec §3.3).
            reasked = await agent.invoke_async(REASK_PROMPT)
            usage = merge_usage(usage, usage_of(reasked))
            data = extract_json(result_text(reasked))
        data["subproblem_id"] = subproblem.id
        return parse_tolerant(WorkerResult, data), usage, tools_used

    async def run_worker(subproblem: Subproblem) -> WorkerOutcome:
        async with semaphore:
            on_status(subproblem.id, WorkerStatus.QUEUED, None)
            outcome = WorkerOutcome(subproblem=subproblem, status=WorkerStatus.RUNNING)
            prompt = worker_prompt(framing, subproblem)
            on_status(subproblem.id, WorkerStatus.RUNNING, None)
            for attempt in (1, 2):
                outcome.attempts = attempt
                if cancel_event.is_set() or synthesize_now_event.is_set():
                    outcome.status = WorkerStatus.CANCELLED
                    on_status(subproblem.id, outcome.status, None)
                    return outcome
                try:
                    result, _usage, tools_used = await attempt_worker(subproblem, prompt)
                    outcome.result = result
                    outcome.tools_used = tools_used
                    outcome.status = WorkerStatus.COMPLETE
                    on_status(
                        subproblem.id,
                        WorkerStatus.COMPLETE,
                        {"tools_used": tools_used, "summary": result.summary[:200]},
                    )
                    return outcome
                except _WorkerStopped:
                    outcome.status = WorkerStatus.CANCELLED
                    outcome.error = "stopped"
                    on_status(subproblem.id, outcome.status, None)
                    return outcome
                except Exception as exc:  # noqa: BLE001 - per-worker isolation
                    if is_cancelled is not None and is_cancelled(exc):
                        outcome.status = WorkerStatus.CANCELLED
                        outcome.error = str(exc)
                        on_status(subproblem.id, outcome.status, None)
                        return outcome
                    logger.warning(
                        "worker %s attempt %d failed: %s", subproblem.id, attempt, exc
                    )
                    outcome.error = str(exc)
                    if attempt == 1:
                        # One retry with a corrected prompt (spec §3.7).
                        outcome.status = WorkerStatus.FAILED_RETRYABLE
                        on_status(
                            subproblem.id,
                            WorkerStatus.FAILED_RETRYABLE,
                            {"error": outcome.error[:200]},
                        )
                        prompt = (
                            worker_prompt(framing, subproblem)
                            + f"\n\nYour previous attempt failed with: {exc}. "
                            "Correct the problem and return ONLY the JSON result object."
                        )
                    else:
                        outcome.status = WorkerStatus.FAILED_FINAL
                        on_status(
                            subproblem.id,
                            WorkerStatus.FAILED_FINAL,
                            {"error": outcome.error[:200]},
                        )
            return outcome

    return await asyncio.gather(*(run_worker(sp) for sp in subproblems))


class _WorkerStopped(Exception):
    """Internal: worker aborted by cancel / synthesize-now."""
