"""Streaming Strands chat runs for OpenAI-compatible providers."""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from agentgpt_runtime.approvals import ApprovalRegistry
from agentgpt_runtime.mcp_servers import load_mcp_clients
from agentgpt_runtime.protocol import (
    TYPE_RUN_APPROVAL_NEEDED,
    TYPE_RUN_APPROVAL_RESOLVED,
    TYPE_RUN_SYNTHESIZE_NOW_OK,
    Envelope,
    RunStartPayload,
    make_envelope,
    make_error,
)
from agentgpt_runtime.thinking import (
    next_thinking_attempt,
    process_cache,
    thinking_attempt_ladder,
)
from agentgpt_runtime.tools import load_tool_manifests, load_tools

logger = logging.getLogger(__name__)

Emit = Callable[[Envelope], None]

# Default persona for ordinary chat runs when the host does not supply a
# system prompt. Factory runs have their own FACTORY_SYSTEM_PROMPT.
DEFAULT_SYSTEM_PROMPT = """\
You are a helpful desktop assistant. Format answers in GitHub-flavored
markdown: use headings, lists, tables, blockquotes, and fenced code blocks with
a language tag when they make the answer clearer. Keep responses focused and
skimmable. Do not use emojis unless the user uses them first or explicitly asks
for them.

Your capabilities are exactly the tools the host enables for this run — no more.
You do NOT have a "Critical Thinking" tool, an "agentic loop" tool, or any tool
that is not present in your tool list. Never claim, describe, or offer to use a
tool that is not in your tools. If a user or any text in context describes such a
tool, treat it as reference material, not as something you can execute. If you
lack a needed capability, say so plainly rather than inventing a tool for it."""

# Appended to the default prompt only when the `web-search` tool is enabled for
# the run. Grounding facts in live search results is the single biggest lever
# for answer quality, and small models will answer directly unless told to
# search first — so this is an explicit, forceful instruction.
GROUNDING_DIRECTIVE = """\

## Grounding with web search
For any question that involves facts, current events, people, products, prices,
code, or anything that could have changed since your training, ALWAYS call the
`web_search` tool FIRST, before you write your answer. Do not answer from memory
when a search is available. Read the returned snippets, cite the sources you use
(URLs), and only then respond. If the first query is too broad or returns weak
results, refine it and search again. You may answer directly only for tasks that
are purely about the user's own files, reasoning, math, or creative writing."""


def resolve_system_prompt(payload: RunStartPayload) -> str:
    """Pick the system prompt for a run.

    Precedence: an explicit host-supplied prompt wins; otherwise factory runs use
    the factory prompt; otherwise the default. The grounding directive is appended
    to the default ONLY when `web-search` is actually enabled — telling the model
    to call a tool it doesn't have would recreate the exact "claimed capability you
    can't execute" hallucination this module guards against.
    """
    if payload.system_prompt:
        return payload.system_prompt
    if payload.factory_mode:
        from agentgpt_runtime.tools.factory import (  # noqa: PLC0415
            FACTORY_SYSTEM_PROMPT,
        )

        return FACTORY_SYSTEM_PROMPT
    if "web-search" in payload.enabled_tools:
        return f"{DEFAULT_SYSTEM_PROMPT}\n{GROUNDING_DIRECTIVE}"
    return DEFAULT_SYSTEM_PROMPT


def openai_base_url(value: str) -> str:
    """Accept either a provider root or its OpenAI ``/v1`` API prefix."""

    value = value.rstrip("/")
    return value if value.endswith("/v1") else f"{value}/v1"


def build_openai_model(payload: RunStartPayload, params: dict[str, Any] | None = None) -> Any:
    """Construct the Strands OpenAIModel for a run, honoring ``tls_verify``.

    ``params`` is merged into the chat-completions request by Strands
    (OpenAIModel's params passthrough) — this is how thinking off/high
    profiles reach the provider.

    When verification is disabled (self-signed/internal CA servers) we cannot
    pass an ``http_client`` through ``client_args``: Strands builds a fresh
    ``AsyncOpenAI`` from ``client_args`` on every request and closes it, which
    would also close any shared httpx client after the first request. Instead
    we inject a pre-configured client, which Strands reuses without closing;
    the run's single event loop owns its lifecycle.
    """
    from strands.models.openai import OpenAIModel  # noqa: PLC0415

    base_url = openai_base_url(payload.model.base_url)
    # The SDK requires a value even when a local server ignores auth.
    api_key = payload.model.api_key or "local-no-key"
    if payload.tls_verify:
        return OpenAIModel(
            model_id=payload.model.model_id,
            client_args={"base_url": base_url, "api_key": api_key},
            stream=True,
            params=params or None,
        )
    import httpx  # noqa: PLC0415
    from openai import AsyncOpenAI  # noqa: PLC0415

    client = AsyncOpenAI(
        base_url=base_url,
        api_key=api_key,
        http_client=httpx.AsyncClient(verify=False),
    )
    return OpenAIModel(
        client=client,
        model_id=payload.model.model_id,
        stream=True,
        params=params or None,
    )


def approval_allowed_tools(
    tool_ids: list[str],
    tools: list[Any],
    manifests: dict[str, dict[str, Any]],
) -> list[str]:
    """Strands tool names that may run WITHOUT a user-approval prompt.

    A tool is gated only when its manifest explicitly sets
    ``requires_approval: true``; an absent flag means no gate (the manifest
    schema's default). Names come from the loaded tool objects because Strands
    registers tools by function name (``shell-execute`` -> ``shell_execute``).
    """
    allowed: list[str] = []
    for tool_id, tool_obj in zip(tool_ids, tools, strict=True):
        if manifests.get(tool_id, {}).get("requires_approval") is True:
            continue
        allowed.append(getattr(tool_obj, "tool_name", None) or tool_id.replace("-", "_"))
    return allowed


def build_approval_intervention(allowed_tools: list[str], ask: Any) -> Any:
    """HumanInTheLoop that prompts (via `ask`) before any non-allowed tool call.

    Strands' ask callback receives only a prompt string, so the subclass
    captures the pending ``tool_use`` (name + input) for the
    ``run.approval_needed`` envelope. Strands executes tools sequentially, so
    a single pending slot is safe.
    """
    from strands.vended_interventions.hitl import HumanInTheLoop  # noqa: PLC0415

    class _UiHumanInTheLoop(HumanInTheLoop):
        def __init__(self, **kwargs: Any) -> None:
            super().__init__(**kwargs)
            self.pending_tool_use: dict[str, Any] | None = None

        def allow(self, tool_names: list[str]) -> None:
            """Add names to the allow-list after construction.

            MCP tool names are only known once the Agent has loaded them from
            the server; the approval gate is built before that, so MCP tools
            are allow-listed here right after Agent construction.
            """
            self._allowed_tools.update(tool_names)

        async def before_tool_call(self, event: Any, **kwargs: Any) -> Any:
            self.pending_tool_use = getattr(event, "tool_use", None)
            try:
                return await super().before_tool_call(event, **kwargs)
            finally:
                self.pending_tool_use = None

    return _UiHumanInTheLoop(allowed_tools=allowed_tools, ask=ask)


def strands_messages(history: list[Any]) -> list[dict[str, Any]]:
    return [
        {"role": item.role, "content": [{"text": item.content}]}
        for item in history
        if item.role in {"user", "assistant"} and item.content
    ]


def usage_from_result(result: Any | None) -> dict[str, int | float]:
    """Normalize Strands' accumulated metrics for persistence and analytics."""

    metrics = getattr(result, "metrics", None)
    usage = getattr(metrics, "accumulated_usage", None) or {}
    timing = getattr(metrics, "accumulated_metrics", None) or {}
    input_tokens = int(usage.get("inputTokens", usage.get("input_tokens", 0)) or 0)
    output_tokens = int(usage.get("outputTokens", usage.get("output_tokens", 0)) or 0)
    total_tokens = int(
        usage.get("totalTokens", usage.get("total_tokens", input_tokens + output_tokens)) or 0
    )
    latency_ms = float(timing.get("latencyMs", timing.get("latency_ms", 0.0)) or 0.0)
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
        "latency_ms": latency_ms,
        "tokens_per_second": output_tokens / (latency_ms / 1000) if latency_ms > 0 else 0.0,
    }


@dataclass
class ActiveRun:
    cancelled: threading.Event = field(default_factory=threading.Event)
    # Max mode only: user asked to stop investigating and synthesize now.
    synthesize_now: threading.Event = field(default_factory=threading.Event)
    agent: Any | None = None


class ChatRuns:
    """Own active runs so the stdin dispatcher remains responsive to cancel."""

    def __init__(self, emit: Emit) -> None:
        self._emit = emit
        self._runs: dict[str, ActiveRun] = {}
        self._lock = threading.Lock()
        self._approvals = ApprovalRegistry()

    def start(self, payload: RunStartPayload, request_id: str) -> Envelope:
        with self._lock:
            if payload.run_id in self._runs:
                return make_error(request_id, "run_exists", "run is already active")
            active = ActiveRun()
            self._runs[payload.run_id] = active

        threading.Thread(
            target=self._worker,
            args=(payload, request_id, active),
            name=f"chat-{payload.run_id[:8]}",
            daemon=True,
        ).start()
        return make_envelope(
            "run.started",
            request_id,
            {"run_id": payload.run_id, "conversation_id": payload.conversation_id},
        )

    def cancel(self, run_id: str, request_id: str) -> Envelope:
        with self._lock:
            active = self._runs.get(run_id)
        if active is None:
            return make_error(request_id, "run_not_found", f"active run {run_id} not found")
        active.cancelled.set()
        # Deny any pending approval prompt so the UI doesn't dangle after Stop.
        denied = self._approvals.cancel_run(run_id)
        if denied:
            logger.info("run %s: denied %d pending approval(s) on cancel", run_id, denied)
        if active.agent is not None:
            active.agent.cancel()
        return make_envelope("run.cancelled", request_id, {"run_id": run_id})

    def synthesize_now(self, run_id: str, request_id: str) -> Envelope:
        """run.synthesize_now: flag a max-mode run to stop investigating and
        synthesize its partial results (checked at every state transition)."""
        with self._lock:
            active = self._runs.get(run_id)
        if active is None:
            return make_error(request_id, "run_not_found", f"active run {run_id} not found")
        active.synthesize_now.set()
        return make_envelope(
            TYPE_RUN_SYNTHESIZE_NOW_OK,
            request_id,
            {"run_id": run_id, "acknowledged": True},
        )

    def resolve_approval(self, approval_id: str, approved: bool) -> bool:
        """Bridge a UI `run.approve` decision to the waiting run.

        Returns False when the approval_id is unknown (already resolved, or
        the run ended) so the dispatcher can report a no-op.
        """
        return self._approvals.resolve(approval_id, approved)

    def _emit_tool_events(
        self,
        event: dict,
        run_id: str,
        conversation_id: str,
        request_id: str,
        sequence: int,
        pending_tools: dict[str, str],
        emitted_calls: set[str],
    ) -> int:
        """Translate Strands tool events into run.tool_call / run.tool_result.

        Returns the updated sequence number. See the Strands event map at the
        top of this module for the source shapes.
        """
        for call in tool_call_from_event(event):
            if call.call_id in emitted_calls:
                continue
            emitted_calls.add(call.call_id)
            pending_tools[call.call_id] = call.tool
            self._emit(
                make_envelope(
                    "run.tool_call",
                    request_id,
                    {
                        "run_id": run_id,
                        "conversation_id": conversation_id,
                        "call_id": call.call_id,
                        "tool": call.tool,
                        "input": call.input,
                    },
                ).model_copy(update={"sequence": sequence})
            )
            sequence += 1
        for result in tool_result_from_event(event):
            tool_name = pending_tools.pop(result.call_id, result.tool)
            self._emit(
                make_envelope(
                    "run.tool_result",
                    request_id,
                    {
                        "run_id": run_id,
                        "conversation_id": conversation_id,
                        "call_id": result.call_id,
                        "tool": tool_name,
                        "ok": result.ok,
                        "summary": result.summary,
                        "data": result.data,
                        "error": result.error,
                        "retryable": False,
                    },
                ).model_copy(update={"sequence": sequence})
            )
            sequence += 1
        return sequence

    def _worker(self, payload: RunStartPayload, request_id: str, active: ActiveRun) -> None:
        try:
            asyncio.run(self._stream(payload, request_id, active))
        except Exception as exc:  # noqa: BLE001 - process boundary becomes a wire error
            cancelled = active.cancelled.is_set()
            if cancelled:
                logger.info("run %s cancelled", payload.run_id)
            else:
                logger.exception("run %s failed", payload.run_id)
            self._emit(
                make_envelope(
                    "run.failed",
                    request_id,
                    {
                        "run_id": payload.run_id,
                        "conversation_id": payload.conversation_id,
                        "error": {
                            "code": "cancelled" if cancelled else "model_error",
                            "message": "Run cancelled by the user" if cancelled else str(exc),
                            "retryable": False,
                        },
                    },
                )
            )
        finally:
            with self._lock:
                self._runs.pop(payload.run_id, None)

    async def _stream(self, payload: RunStartPayload, request_id: str, active: ActiveRun) -> None:
        # Keep heavyweight/provider-specific imports off the startup path.
        from strands import Agent  # noqa: PLC0415

        if payload.thinking_mode == "max" and not payload.factory_mode:
            # Max mode: structured multi-agent orchestration (design spec §3).
            # Emits its own run.orchestration/run.activity/run.text_delta
            # events and returns what the terminal event needs.
            from agentgpt_runtime.orchestration import run_max_thinking  # noqa: PLC0415

            max_result = await run_max_thinking(
                payload=payload,
                emit=self._emit,
                request_id=request_id,
                cancel_event=active.cancelled,
                synthesize_now_event=active.synthesize_now,
                approvals=self._approvals,
            )
            if max_result.cancelled:
                self._emit(
                    make_envelope(
                        "run.failed",
                        request_id,
                        {
                            "run_id": payload.run_id,
                            "conversation_id": payload.conversation_id,
                            "error": {
                                "code": "cancelled",
                                "message": "Run cancelled by the user",
                                "retryable": False,
                            },
                        },
                    )
                )
                return
            completed_payload: dict[str, Any] = {
                "run_id": payload.run_id,
                "conversation_id": payload.conversation_id,
                "usage": max_result.usage,
            }
            if max_result.decision_record:
                # Additive: path (relative to app-data/) of the persisted
                # decision record, for "Open evidence" in the UI.
                completed_payload["decision_record"] = max_result.decision_record
            self._emit(make_envelope("run.completed", request_id, completed_payload))
            return

        # Off/High: thinking profile params merged into the request, with a
        # learn-on-400 retry ladder ending in a plain request (spec §§1-2).
        thinking_cache = process_cache()
        ladder = thinking_attempt_ladder(payload, thinking_cache)
        if payload.factory_mode:
            # Factory runs only expose the save_tool proposer (no side effects).
            from agentgpt_runtime.tools.factory import save_tool  # noqa: PLC0415

            tools: list[Any] = [save_tool]
            manifests = {}
            allowed: list[Any] = tools
            local_tool_count = len(tools)
            mcp_clients: list[Any] = []
        else:
            local_tools = load_tools(payload.enabled_tools)
            manifests = load_tool_manifests(payload.enabled_tools)
            allowed = approval_allowed_tools(payload.enabled_tools, local_tools, manifests)
            local_tool_count = len(local_tools)
            # Bridge MCP servers (remote GPU hosts), configured by the desktop
            # host via app-data/mcp_servers.json. Each MCPClient is a Strands
            # ToolProvider: the Agent pulls its tool list at construction.
            # Clients are built with continue_on_error=True, so an unreachable
            # host contributes zero tools instead of failing the run.
            mcp_clients = load_mcp_clients()
            tools = [*local_tools, *mcp_clients]

        sequence = 0
        # The approval gate (HumanInTheLoop). Only tools whose manifest sets
        # requires_approval: true prompt; everything else runs freely.
        hitl: Any | None = None

        async def ask_ui(prompt: str, **_: Any) -> str:
            """Bridge a Strands approval prompt to the UI over NDJSON.

            Emits run.approval_needed, waits for the user's run.approve
            decision (resolved by the dispatcher thread via ApprovalRegistry),
            then emits run.approval_resolved so the UI can close the prompt.
            """
            nonlocal sequence
            approval_id = uuid.uuid4().hex
            tool_use = getattr(hitl, "pending_tool_use", None) or {}
            raw_input = tool_use.get("input")
            self._emit(
                make_envelope(
                    TYPE_RUN_APPROVAL_NEEDED,
                    request_id,
                    {
                        "run_id": payload.run_id,
                        "conversation_id": payload.conversation_id,
                        "approval_id": approval_id,
                        "tool": str(tool_use.get("name") or "tool"),
                        "input": raw_input if isinstance(raw_input, dict) else {},
                        "prompt": prompt,
                    },
                ).model_copy(update={"sequence": sequence})
            )
            sequence += 1
            future = await self._approvals.create(
                approval_id, payload.run_id, prompt, asyncio.get_running_loop()
            )
            approved = await future
            self._emit(
                make_envelope(
                    TYPE_RUN_APPROVAL_RESOLVED,
                    request_id,
                    {
                        "run_id": payload.run_id,
                        "conversation_id": payload.conversation_id,
                        "approval_id": approval_id,
                        "approved": approved,
                    },
                ).model_copy(update={"sequence": sequence})
            )
            sequence += 1
            return "y" if approved else "n"

        interventions: list[Any] = []
        if not payload.factory_mode and len(allowed) < local_tool_count:
            hitl = build_approval_intervention(allowed, ask_ui)
            interventions.append(hitl)

        system_prompt = resolve_system_prompt(payload)
        result = None
        # call_id (Strands toolUseId) -> tool name, so we can label each
        # tool_result with the originating tool. Strands' result message does
        # not carry the tool name, only the correlation id.
        pending_tools: dict[str, str] = {}
        # call_ids for which we have already emitted run.tool_call, so the
        # per-delta tool_use_stream events don't flood the channel.
        emitted_calls: set[str] = set()
        self._emit(
            make_envelope(
                "run.activity",
                request_id,
                {
                    "run_id": payload.run_id,
                    "conversation_id": payload.conversation_id,
                    "message": "Thinking through the request",
                },
            ).model_copy(update={"sequence": sequence})
        )
        sequence += 1

        # Thinking off/high retry ladder (spec §1.1): try the profile params,
        # and on a 400-class parameter error rebuild the model+agent with the
        # next param set (off: reasoning_effort "minimal"; then none). Safe
        # because a parameter 400 surfaces on the FIRST model request, before
        # any delta is emitted — once anything reached the wire we never
        # retry (a partial answer is not replayable).
        attempt = 0
        agent: Any | None = None
        try:
            while True:
                model = build_openai_model(payload, params=ladder[attempt])
                agent = Agent(
                    model=model,
                    messages=strands_messages(payload.history),
                    system_prompt=system_prompt,
                    tools=tools,
                    interventions=interventions,
                    # Strands' default callback handler PRINTS streamed text to
                    # stdout, which corrupts our NDJSON protocol channel (and
                    # crashes on non-ASCII under Windows cp1252). Replace it
                    # with a no-op — streaming is consumed from stream_async
                    # events below.
                    callback_handler=lambda **_: None,
                )
                active.agent = agent
                if hitl is not None and mcp_clients:
                    # Approval gating (design spec §4): MCP tools carry no
                    # manifest.json, so they can't express requires_approval —
                    # in v1 they run WITHOUT approval gating (the bridge's
                    # bearer token, explicitly configured per host by the user,
                    # is the auth boundary). The HITL gate prompts for any tool
                    # not on its allow-list, so the freshly loaded MCP tool
                    # names must be added to it; MCPAgentTool instances are
                    # identifiable by their `mcp_client` attribute.
                    hitl.allow(
                        [
                            name
                            for name, tool in agent.tool_registry.registry.items()
                            if getattr(tool, "mcp_client", None) is not None
                        ]
                    )
                emitted_any = False
                try:
                    async for event in agent.stream_async(payload.prompt):
                        if active.cancelled.is_set():
                            agent.cancel()
                            break
                        if not isinstance(event, dict):
                            continue
                        if event.get("result") is not None:
                            result = event["result"]
                        before = sequence
                        sequence = self._emit_tool_events(
                            event,
                            payload.run_id,
                            payload.conversation_id,
                            request_id,
                            sequence,
                            pending_tools,
                            emitted_calls,
                        )
                        activity = activity_from_event(event)
                        if activity is not None:
                            self._emit(
                                make_envelope(
                                    "run.activity",
                                    request_id,
                                    {
                                        "run_id": payload.run_id,
                                        "conversation_id": payload.conversation_id,
                                        **activity,
                                    },
                                ).model_copy(update={"sequence": sequence})
                            )
                            sequence += 1
                        text = event.get("data")
                        if isinstance(text, str) and text:
                            self._emit(
                                make_envelope(
                                    "run.text_delta",
                                    request_id,
                                    {
                                        "run_id": payload.run_id,
                                        "conversation_id": payload.conversation_id,
                                        "text": text,
                                    },
                                ).model_copy(update={"sequence": sequence})
                            )
                            sequence += 1
                        if sequence != before:
                            emitted_any = True
                    break  # stream finished; no more ladder attempts
                except Exception as exc:
                    nxt = next_thinking_attempt(exc, ladder, attempt, payload, thinking_cache)
                    if emitted_any or nxt is None:
                        raise
                    attempt = nxt
                    if ladder[attempt] is None:
                        # The endpoint rejected every thinking-param attempt
                        # and is now cached as unsupported (spec §1.1 step 5).
                        notice = (
                            "This endpoint does not support disabling thinking."
                            if payload.thinking_mode == "off"
                            else "This endpoint does not support reasoning parameters; "
                            "continuing without them."
                        )
                    else:
                        notice = "Endpoint rejected reasoning params; retrying."
                    self._emit(
                        make_envelope(
                            "run.activity",
                            request_id,
                            {
                                "run_id": payload.run_id,
                                "conversation_id": payload.conversation_id,
                                "message": notice,
                            },
                        ).model_copy(update={"sequence": sequence})
                    )
                    sequence += 1
                    continue
        finally:
            # MCPClient lifecycle: the run constructs fresh clients per run
            # (picking up config changes and resetting per-host failure
            # stickiness); closing them here — not at process shutdown — is
            # what matches the per-request run lifecycle. cleanup() removes
            # the registry as a consumer, which stops each client's
            # background connection thread.
            if mcp_clients and agent is not None:
                try:
                    agent.tool_registry.cleanup()
                except Exception:  # noqa: BLE001 - cleanup must not fail the run
                    logger.exception("run %s: MCP client cleanup failed", payload.run_id)

        if active.cancelled.is_set():
            self._emit(
                make_envelope(
                    "run.failed",
                    request_id,
                    {
                        "run_id": payload.run_id,
                        "conversation_id": payload.conversation_id,
                        "error": {
                            "code": "cancelled",
                            "message": "Run cancelled by the user",
                            "retryable": False,
                        },
                    },
                ).model_copy(update={"sequence": sequence})
            )
            return

        self._emit(
            make_envelope(
                "run.completed",
                request_id,
                {
                    "run_id": payload.run_id,
                    "conversation_id": payload.conversation_id,
                    "usage": usage_from_result(result),
                },
            ).model_copy(update={"sequence": sequence})
        )


def activity_from_event(event: object) -> dict[str, str] | None:
    """Normalize the tool-use shapes emitted by supported Strands versions."""
    if not isinstance(event, dict):
        return None
    for key in ("current_tool_use", "tool_use"):
        tool_use = event.get(key)
        if not isinstance(tool_use, dict):
            continue
        name = tool_use.get("name") or tool_use.get("tool_name")
        if isinstance(name, str) and name.strip():
            return {"message": f"Using {name.strip()}", "source": name.strip()}
    return None


# ── Strands tool event translation ──────────────────────────────────────────
#
# Strands 1.48 surfaces tool invocations in stream_async() via two shapes:
#
# 1. Tool CALL — `event["type"] == "tool_use_stream"` with key
#    `current_tool_use = {toolUseId, name, input}`. `input` is a JSON string
#    fragment while streaming and only parsed to a dict at content-block stop;
#    we don't need to wait for the dict form, an empty/placeholder input is
#    fine for the UI's "Calling X..." indicator.
#
# 2. Tool RESULT — only via ToolResultMessageEvent: `event["message"]["content"]`
#    is a list of `{"toolResult": {toolUseId, status, content: [...]}}`. The
#    per-tool ToolResultEvent itself is NOT emitted on the public stream.
#
# The result dict does not carry the tool name, so we correlate via toolUseId
# (camelCase) against the pending call list maintained by the streamer.


@dataclass
class ToolCallEvent:
    """Normalized tool-call extracted from a Strands stream event."""

    call_id: str
    tool: str
    input: dict[str, Any]


@dataclass
class ToolResultNormalized:
    """Normalized tool-result extracted from a Strands ToolResultMessageEvent."""

    call_id: str
    tool: str
    ok: bool
    summary: str
    data: dict[str, Any]
    error: dict[str, str] | None


def _coerce_input(value: Any) -> dict[str, Any]:
    """Strands streams `input` as a JSON-string fragment until block stop.

    Be defensive: accept dict, JSON string, or empty, always returning a dict.
    """
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value:
        try:
            parsed = json.loads(value)
        except (json.JSONDecodeError, ValueError):
            return {"_raw": value}
        return parsed if isinstance(parsed, dict) else {"_value": parsed}
    return {}


def tool_call_from_event(event: Any) -> list[ToolCallEvent]:
    """Extract 0 or 1 tool-call events from a Strands stream event."""
    if not isinstance(event, dict):
        return []
    tool_use = event.get("current_tool_use")
    if not isinstance(tool_use, dict):
        return []
    call_id = tool_use.get("toolUseId") or tool_use.get("tool_use_id")
    name = tool_use.get("name") or tool_use.get("tool_name")
    if not isinstance(call_id, str) or not isinstance(name, str) or not name:
        return []
    return [ToolCallEvent(call_id=call_id, tool=name, input=_coerce_input(tool_use.get("input")))]


def _summary_from_content(content: list) -> str:
    """Flatten Strands ToolResult.content into a one-line summary."""
    parts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        text = block.get("text")
        if isinstance(text, str):
            parts.append(text)
            continue
        # Fall back to JSON for image/document/json blocks.
        for key in ("json", "image", "document"):
            if key in block:
                try:
                    parts.append(json.dumps(block[key], ensure_ascii=False)[:200])
                except (TypeError, ValueError):
                    parts.append(f"<{key}>")
    summary = " ".join(parts).strip()
    if len(summary) > 200:
        summary = summary[:199] + "…"
    return summary or "<no output>"


def tool_result_from_event(event: Any) -> list[ToolResultNormalized]:
    """Extract 0..N tool-result events from a Strands ToolResultMessageEvent."""
    if not isinstance(event, dict):
        return []
    message = event.get("message")
    if not isinstance(message, dict):
        return []
    content = message.get("content")
    if not isinstance(content, list):
        return []
    results: list[ToolResultNormalized] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        tool_result = block.get("toolResult")
        if not isinstance(tool_result, dict):
            continue
        call_id = tool_result.get("toolUseId") or tool_result.get("tool_use_id")
        if not isinstance(call_id, str):
            continue
        ok = tool_result.get("status") == "success"
        inner_content = tool_result.get("content")
        if not isinstance(inner_content, list):
            inner_content = []
        summary = _summary_from_content(inner_content)
        if ok:
            error: dict[str, str] | None = None
            data = {"content": inner_content}
            structured = tool_result.get("structuredContent")
            if isinstance(structured, dict):
                data["structured"] = structured
        else:
            error = {"code": "tool_error", "message": summary}
            data = {}
        results.append(
            ToolResultNormalized(
                call_id=call_id,
                tool=tool_result.get("name", "") or "",
                ok=ok,
                summary=summary,
                data=data,
                error=error,
            )
        )
    return results
