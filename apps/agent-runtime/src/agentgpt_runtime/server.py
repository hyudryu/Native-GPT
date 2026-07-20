"""Message dispatch for the Phase 0 runtime message set.

- runtime.hello    -> runtime.hello.ok
- runtime.health   -> runtime.health.ok (status, uptime_seconds, rss_bytes)
- runtime.shutdown -> runtime.shutdown echoed with empty payload, then exit 0
- anything else    -> ignored (no response, not fatal)
"""

from __future__ import annotations

import logging
import time
from typing import Any

import psutil
from pydantic import ValidationError

from agentgpt_runtime import __version__
from agentgpt_runtime.chat import ChatRuns
from agentgpt_runtime.endpoints import EndpointError, list_models, test_endpoint
from agentgpt_runtime.protocol import (
    TYPE_ENDPOINT_TEST,
    TYPE_ENDPOINT_TEST_OK,
    TYPE_HEALTH,
    TYPE_HEALTH_OK,
    TYPE_HELLO,
    TYPE_HELLO_OK,
    TYPE_MODELS_LIST,
    TYPE_MODELS_LIST_OK,
    TYPE_RUN_CANCEL,
    TYPE_RUN_START,
    TYPE_SHUTDOWN,
    EndpointTestPayload,
    Envelope,
    HealthOkPayload,
    HelloOkPayload,
    HelloPayload,
    ModelsListPayload,
    RunCancelPayload,
    RunStartPayload,
    make_envelope,
    make_error,
)

logger = logging.getLogger(__name__)

RUNTIME_NAME = "agentgpt-runtime"
CAPABILITIES = ["chat", "tools"]

_STARTED_AT = time.monotonic()
_PROCESS = psutil.Process()
chat_runs: ChatRuns | None = None


def configure_chat_runs(runs: ChatRuns) -> None:
    global chat_runs
    chat_runs = runs


def uptime_seconds() -> float:
    return time.monotonic() - _STARTED_AT


def rss_bytes() -> int:
    return _PROCESS.memory_info().rss


def health_payload(status: str = "ok") -> HealthOkPayload:
    return HealthOkPayload(
        status=status,
        uptime_seconds=uptime_seconds(),
        rss_bytes=rss_bytes(),
    )


def dispatch(envelope: Envelope) -> Envelope | None:
    """Handle one request envelope.

    Returns the response envelope, or None if the message type is unknown
    (unknown types are ignored by design, not an error).
    """
    if envelope.type == TYPE_HELLO:
        try:
            hello = HelloPayload.model_validate(envelope.payload)
        except ValidationError as exc:
            return make_error(
                envelope.request_id, "bad_request", f"invalid runtime.hello payload: {exc}"
            )
        logger.info("hello from %s %s", hello.client, hello.client_version)
        return make_envelope(
            TYPE_HELLO_OK,
            envelope.request_id,
            HelloOkPayload(
                runtime=RUNTIME_NAME,
                runtime_version=__version__,
                capabilities=list(CAPABILITIES),
            ),
        )

    if envelope.type == TYPE_HEALTH:
        return make_envelope(TYPE_HEALTH_OK, envelope.request_id, health_payload())

    if envelope.type == TYPE_ENDPOINT_TEST:
        try:
            payload = EndpointTestPayload.model_validate(envelope.payload)
        except ValidationError as exc:
            return make_error(
                envelope.request_id, "bad_request", f"invalid endpoint.test payload: {exc}"
            )
        logger.info("endpoint.test %s", payload.base_url)
        return make_envelope(TYPE_ENDPOINT_TEST_OK, envelope.request_id, test_endpoint(payload))

    if envelope.type == TYPE_MODELS_LIST:
        try:
            payload = ModelsListPayload.model_validate(envelope.payload)
        except ValidationError as exc:
            return make_error(
                envelope.request_id, "bad_request", f"invalid models.list payload: {exc}"
            )
        logger.info("models.list %s", payload.base_url)
        try:
            result = list_models(payload)
        except EndpointError as exc:
            return make_error(envelope.request_id, exc.code, exc.message, exc.retryable)
        return make_envelope(TYPE_MODELS_LIST_OK, envelope.request_id, result)

    if envelope.type == TYPE_RUN_START:
        try:
            payload = RunStartPayload.model_validate(envelope.payload)
        except ValidationError as exc:
            return make_error(
                envelope.request_id, "bad_request", f"invalid run.start payload: {exc}"
            )
        if chat_runs is None:
            return make_error(
                envelope.request_id, "runtime_unavailable", "chat runner not configured"
            )
        return chat_runs.start(payload, envelope.request_id)

    if envelope.type == TYPE_RUN_CANCEL:
        try:
            payload = RunCancelPayload.model_validate(envelope.payload)
        except ValidationError as exc:
            return make_error(
                envelope.request_id, "bad_request", f"invalid run.cancel payload: {exc}"
            )
        if chat_runs is None:
            return make_error(
                envelope.request_id, "runtime_unavailable", "chat runner not configured"
            )
        return chat_runs.cancel(payload.run_id, envelope.request_id)

    if envelope.type == TYPE_SHUTDOWN:
        logger.info("shutdown requested (request_id=%s)", envelope.request_id)
        # Ack: echo the shutdown type with an empty payload; caller exits 0 after.
        return make_envelope(TYPE_SHUTDOWN, envelope.request_id, {})

    logger.debug("ignoring unknown message type %r", envelope.type)
    return None


def should_exit(envelope: Envelope) -> bool:
    return envelope.type == TYPE_SHUTDOWN


def load_agent_sdk() -> Any:
    """Lazily import the mandated agent SDK (strands-agents).

    Never called at startup; only when an actual agent run begins, to keep
    sidecar startup fast.
    """
    import strands  # noqa: PLC0415

    return strands
