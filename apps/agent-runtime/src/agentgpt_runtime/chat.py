"""Streaming Strands chat runs for OpenAI-compatible providers."""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from agentgpt_runtime.protocol import Envelope, RunStartPayload, make_envelope, make_error
from agentgpt_runtime.tools import load_tools

logger = logging.getLogger(__name__)

Emit = Callable[[Envelope], None]


def openai_base_url(value: str) -> str:
    """Accept either a provider root or its OpenAI ``/v1`` API prefix."""

    value = value.rstrip("/")
    return value if value.endswith("/v1") else f"{value}/v1"


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
    agent: Any | None = None


class ChatRuns:
    """Own active runs so the stdin dispatcher remains responsive to cancel."""

    def __init__(self, emit: Emit) -> None:
        self._emit = emit
        self._runs: dict[str, ActiveRun] = {}
        self._lock = threading.Lock()

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
        if active.agent is not None:
            active.agent.cancel()
        return make_envelope("run.cancelled", request_id, {"run_id": run_id})

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
        from strands.models.openai import OpenAIModel  # noqa: PLC0415

        model = OpenAIModel(
            model_id=payload.model.model_id,
            client_args={
                "base_url": openai_base_url(payload.model.base_url),
                # The SDK requires a value even when a local server ignores auth.
                "api_key": payload.model.api_key or "local-no-key",
            },
            stream=True,
        )
        agent = Agent(
            model=model,
            messages=strands_messages(payload.history),
            system_prompt=payload.system_prompt,
            tools=load_tools(payload.enabled_tools),
            # Strands' default callback handler PRINTS streamed text to stdout,
            # which corrupts our NDJSON protocol channel (and crashes on
            # non-ASCII under Windows cp1252). Replace it with a no-op —
            # streaming is consumed from stream_async events below.
            callback_handler=lambda **_: None,
        )
        active.agent = agent
        sequence = 0
        result = None
        self._emit(
            make_envelope(
                "run.activity",
                request_id,
                {"run_id": payload.run_id, "message": "Thinking through the request"},
            ).model_copy(update={"sequence": sequence})
        )
        sequence += 1
        async for event in agent.stream_async(payload.prompt):
            if active.cancelled.is_set():
                agent.cancel()
                break
            text = event.get("data") if isinstance(event, dict) else None
            if isinstance(event, dict) and event.get("result") is not None:
                result = event["result"]
            activity = activity_from_event(event)
            if activity is not None:
                self._emit(
                    make_envelope(
                        "run.activity",
                        request_id,
                        {"run_id": payload.run_id, **activity},
                    ).model_copy(update={"sequence": sequence})
                )
                sequence += 1
            if isinstance(text, str) and text:
                self._emit(
                    make_envelope(
                        "run.text_delta",
                        request_id,
                        {"run_id": payload.run_id, "text": text},
                    ).model_copy(update={"sequence": sequence})
                )
                sequence += 1

        if active.cancelled.is_set():
            self._emit(
                make_envelope(
                    "run.failed",
                    request_id,
                    {
                        "run_id": payload.run_id,
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
                {"run_id": payload.run_id, "usage": usage_from_result(result)},
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
